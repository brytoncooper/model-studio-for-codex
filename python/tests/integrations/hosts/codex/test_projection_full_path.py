"""End-to-end B09 Codex projection wiring through build_engine_server.

Covers create / rename / connection-save fanout / remove / restart recovery and
foreign-file conflict. The projection root is an injected temp directory; no
home discovery, no live path use. The expected managed-agent marker is the
exact bytes emitted by the renderer.
"""
from __future__ import annotations

import sqlite3
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from model_deck.adapters.platform.macos.isolated_roots import validate_isolated_roots
from model_deck.adapters.storage.sqlite_connection_repository import SQLiteConnectionRepository
from model_deck.adapters.storage.sqlite_model_repository import SQLiteModelRepository
from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import _combine_startup_recovery, build_engine_server
from model_deck.engine.connections.ports import SaveConnectionCommand
from model_deck.engine.model_library.ports import RegisterModelCommand
from model_deck.integrations.hosts.codex.agent_materializer.resolution import ResolvedConnection
from model_deck_contracts.paths import repo_root

CONSUMER_ID = "com.modeldeck.host.codex.managed-agent"
MANAGED_AGENT_MARKER = b"# Managed by OpenRouter Settings native-agent registration v1\n"
CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
SECOND_CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440004"
PROVIDER_ID = "com.example.provider"
PROVIDER_ID_ALT = "com.example.provider.alt"
MODEL_ID = "openrouter/test-model"
SECOND_MODEL_ID = "qwen/qwen3.8-27b"
FIXTURES = Path(__file__).resolve().parents[2] / "engine" / "fixtures"
PROJECT_ROOT = Path(__file__).resolve().parents[5]


def _resolve_endpoint(record: Any) -> ResolvedConnection:
    return ResolvedConnection(
        connection_id=record.connection_id,
        kind="endpoint",
        revision=record.revision,
        endpoint_name=record.provider_id,
        base_url=(
            "https://alternate.example/v1"
            if record.provider_id == PROVIDER_ID_ALT
            else "https://openrouter.ai/api/v1"
        ),
        credential_account_id="550e8400-e29b-41d4-a716-446655440003",
        billing_description="Billed by token usage.",
    )


def _authenticate(session: Any, descriptor: Any, credential: str) -> None:
    response = session.call(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "engine.v1.hello",
            "params": {
                "client_name": "projection-full-path",
                "offered_api": {"major": 1, "minor": 0},
                "authentication": {
                    "engine_instance_id": descriptor.engine_instance_id,
                    "instance_nonce": descriptor.instance_nonce,
                    "credential": credential,
                },
            },
        }
    )
    assert response["result"]["authenticated"] is True, response


def _fetch_outbox_rows(db_path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT outbox_id, aggregate_type, aggregate_id, aggregate_revision, event_kind, payload_json, state "
            "FROM projection_outbox ORDER BY outbox_id"
        ).fetchall()
    finally:
        conn.close()


def _list_agent_files(projection_root: Path) -> list[Path]:
    agents_dir = projection_root / "agents"
    if not agents_dir.is_dir():
        return []
    return sorted(p for p in agents_dir.iterdir() if p.is_file() and not p.name.startswith("."))


def _save_connection(session, *, connection_id: str, provider_id: str, expected_revision: int, idempotency_key: str) -> dict[str, Any]:
    return session.call(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "engine.v1.connections.save",
            "params": {
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
                "connection": {"connection_id": connection_id, "provider_id": provider_id},
            },
        }
    )


def _register_model(
    session, *, connection_id: str, provider_model_id: str, display_name: str,
    expected_revision: int, idempotency_key: str,
) -> dict[str, Any]:
    return session.call(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "engine.v1.models.register",
            "params": {
                "connection_id": connection_id,
                "provider_model_id": provider_model_id,
                "display_name": display_name,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
            },
        }
    )


def _rename_model(
    session, *, registration_id: str, display_name: str,
    expected_revision: int, idempotency_key: str,
) -> dict[str, Any]:
    return session.call(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "engine.v1.models.rename",
            "params": {
                "registration_id": registration_id,
                "display_name": display_name,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
            },
        }
    )


def _remove_model(
    session, *, registration_id: str, expected_revision: int, idempotency_key: str,
) -> dict[str, Any]:
    return session.call(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "engine.v1.models.remove",
            "params": {
                "registration_id": registration_id,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
            },
        }
    )


def _projection_status(session, *, host_id: str) -> dict[str, Any]:
    return session.call(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "engine.v1.hosts.projection_status",
            "params": {"host_id": host_id},
        }
    )


def _mcp_tool_call(process: subprocess.Popen[str], request_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }) + "\n")
    process.stdin.flush()
    reply = process.stdout.readline()
    if not reply:
        stderr = process.stderr.read() if process.stderr is not None else ""
        raise AssertionError(f"MCP process ended before replying: {stderr}")
    return json.loads(reply)


class ProjectionFullPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temps: list[tempfile.TemporaryDirectory[str]] = []

    def tearDown(self) -> None:
        for td in self._temps:
            td.cleanup()

    def _temp_dir(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self._temps.append(td)
        return Path(td.name).resolve()

    def _start_runtime(
        self,
        *,
        enable_application_state: bool,
        projection_root: Path | None = None,
        projection_resolver: Any | None = None,
        state: Path | None = None,
        artifact: Path | None = None,
        socket_root: Path | None = None,
    ):
        root = repo_root()
        state = state or self._temp_dir()
        artifact = artifact or self._temp_dir()
        socket_root = socket_root or self._temp_dir()
        validate_isolated_roots(state, artifact, socket_root, source_root=root)
        kwargs: dict[str, Any] = dict(
            state_root=state,
            artifact_root=artifact,
            socket_root=socket_root,
            legacy_agents_dir=FIXTURES / "legacy_agent",
            default_connection_id=CONNECTION_ID,
            source_root=root,
            enable_application_state=enable_application_state,
        )
        if projection_root is not None:
            kwargs["projection_root"] = projection_root
            kwargs["projection_resolver"] = projection_resolver
            kwargs["projection_token_helper_path"] = Path("/usr/bin/printf")
        runtime = build_engine_server(**kwargs)
        runtime.server.start()
        self.addCleanup(runtime.server.stop)
        return runtime

    def _open_session(self, runtime):
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        return client, descriptor, credential

    def test_projection_root_allows_macos_system_temporary_alias(self) -> None:
        temporary_alias = Path("/tmp")
        if not temporary_alias.is_symlink():
            self.skipTest("/tmp is not a system symlink on this platform")
        projection_root = Path(tempfile.mkdtemp(prefix="model-deck-projection-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, projection_root, True)
        (projection_root / "agents").mkdir()

        runtime = self._start_runtime(
            enable_application_state=True,
            projection_root=projection_root,
            projection_resolver=_resolve_endpoint,
        )

        self.assertIsNotNone(runtime.application_database_path)

    def test_projection_recovery_runs_when_another_startup_recovery_fails(self) -> None:
        calls = []

        def failing_recovery() -> None:
            calls.append("base")
            raise RuntimeError("fixture recovery failure")

        class _ProjectionRecovery:
            def recover_then_reconcile(self, *, trigger: str) -> None:
                calls.append(trigger)

        callback = _combine_startup_recovery(failing_recovery, _ProjectionRecovery())

        with self.assertRaisesRegex(RuntimeError, "fixture recovery failure"):
            callback()
        self.assertEqual(calls, ["base", "startup"])

    # --- Test 1: create then status ready ---
    def test_create_then_status_ready(self) -> None:
        projection_root = self._temp_dir()
        (projection_root / "agents").mkdir(parents=True, exist_ok=True)
        state = self._temp_dir()
        artifact = self._temp_dir()
        socket_root = self._temp_dir()
        runtime = self._start_runtime(
            enable_application_state=True,
            projection_root=projection_root,
            projection_resolver=_resolve_endpoint,
            state=state,
            artifact=artifact,
            socket_root=socket_root,
        )
        assert runtime.application_database_path is not None
        client, descriptor, credential = self._open_session(runtime)
        with client.session() as session:
            _authenticate(session, descriptor, credential)
            _save_connection(
                session, connection_id=CONNECTION_ID, provider_id=PROVIDER_ID,
                expected_revision=0, idempotency_key="conn-create",
            )
            registered = _register_model(
                session, connection_id=CONNECTION_ID, provider_model_id=MODEL_ID,
                display_name="Test Model", expected_revision=0, idempotency_key="model-reg",
            )
            self.assertIn("result", registered)
            status = _projection_status(session, host_id=CONSUMER_ID)
            self.assertIn("result", status, status)
            self.assertEqual(status["result"]["status"], "ready")
        files = _list_agent_files(projection_root)
        self.assertEqual(len(files), 1, f"expected exactly one agent file, got {[f.name for f in files]}")
        data = files[0].read_bytes()
        self.assertTrue(data.startswith(MANAGED_AGENT_MARKER), data[:80])

    # --- Test 2: rename revises file ---
    def test_rename_revises_file(self) -> None:
        projection_root = self._temp_dir()
        (projection_root / "agents").mkdir(parents=True, exist_ok=True)
        runtime = self._start_runtime(
            enable_application_state=True,
            projection_root=projection_root,
            projection_resolver=_resolve_endpoint,
        )
        client, descriptor, credential = self._open_session(runtime)
        with client.session() as session:
            _authenticate(session, descriptor, credential)
            _save_connection(
                session, connection_id=CONNECTION_ID, provider_id=PROVIDER_ID,
                expected_revision=0, idempotency_key="conn-rename",
            )
            registered = _register_model(
                session, connection_id=CONNECTION_ID, provider_model_id=MODEL_ID,
                display_name="Original", expected_revision=0, idempotency_key="reg-rename",
            )
            registration_id = registered["result"]["model"]["registration_id"]
            revision = registered["result"]["model"]["revision"]
            renamed = _rename_model(
                session, registration_id=registration_id, display_name="Renamed",
                expected_revision=revision, idempotency_key="rename",
            )
            self.assertIn("result", renamed)
            status = _projection_status(session, host_id=CONSUMER_ID)
            self.assertEqual(status["result"]["status"], "ready")
        files = _list_agent_files(projection_root)
        self.assertEqual(len(files), 1)
        data = files[0].read_bytes()
        self.assertIn(b"Renamed", data)

    # --- Test 3: connection-save atomic fanout re-renders ---
    def test_connection_save_fanout_re_renders(self) -> None:
        projection_root = self._temp_dir()
        (projection_root / "agents").mkdir(parents=True, exist_ok=True)
        runtime = self._start_runtime(
            enable_application_state=True,
            projection_root=projection_root,
            projection_resolver=_resolve_endpoint,
        )
        assert runtime.application_database_path is not None
        client, descriptor, credential = self._open_session(runtime)
        with client.session() as session:
            _authenticate(session, descriptor, credential)
            _save_connection(
                session, connection_id=CONNECTION_ID, provider_id=PROVIDER_ID,
                expected_revision=0, idempotency_key="conn-fanout",
            )
            first_registered = _register_model(
                session, connection_id=CONNECTION_ID, provider_model_id=MODEL_ID,
                display_name="Test", expected_revision=0, idempotency_key="reg-fanout",
            )
            _save_connection(
                session, connection_id=SECOND_CONNECTION_ID, provider_id=PROVIDER_ID,
                expected_revision=0, idempotency_key="conn-unrelated",
            )
            second_registered = _register_model(
                session, connection_id=SECOND_CONNECTION_ID, provider_model_id=SECOND_MODEL_ID,
                display_name="Unrelated", expected_revision=0, idempotency_key="reg-unrelated",
            )
            files_before = _list_agent_files(projection_root)
            self.assertEqual(len(files_before), 2)
            unrelated_before = next(
                path.read_bytes() for path in files_before
                if SECOND_MODEL_ID.encode() in path.read_bytes()
            )
            affected_before = next(
                path.read_bytes() for path in files_before
                if MODEL_ID.encode() in path.read_bytes()
            )
            outbox_ids_before = [row[0] for row in _fetch_outbox_rows(runtime.application_database_path)]
            max_before = max(outbox_ids_before) if outbox_ids_before else 0
            _save_connection(
                session, connection_id=CONNECTION_ID, provider_id=PROVIDER_ID_ALT,
                expected_revision=1, idempotency_key="conn-fanout-2",
            )
            outbox_rows_after = _fetch_outbox_rows(runtime.application_database_path)
            new_rows = [r for r in outbox_rows_after if r[0] > max_before]
            self.assertTrue(len(new_rows) >= 1, f"expected fanout upsert, got {outbox_rows_after}")
            first_registration_id = first_registered["result"]["model"]["registration_id"]
            second_registration_id = second_registered["result"]["model"]["registration_id"]
            self.assertTrue(any(
                row[1] == "registered_model"
                and row[2] == first_registration_id
                and row[6] == "applied"
                for row in new_rows
            ))
            self.assertFalse(any(row[2] == second_registration_id for row in new_rows))
            status = _projection_status(session, host_id=CONSUMER_ID)
            self.assertEqual(status["result"]["status"], "ready")
            files_after = _list_agent_files(projection_root)
            self.assertEqual(len(files_after), 2)
            unrelated_after = next(
                path.read_bytes() for path in files_after
                if SECOND_MODEL_ID.encode() in path.read_bytes()
            )
            affected_after = next(
                path.read_bytes() for path in files_after
                if MODEL_ID.encode() in path.read_bytes()
            )
            self.assertEqual(unrelated_after, unrelated_before)
            self.assertNotEqual(affected_after, affected_before)
            self.assertIn(b"https://alternate.example/v1", affected_after)

    # --- Test 4: remove tombstones file ---
    def test_remove_tombstones_file(self) -> None:
        projection_root = self._temp_dir()
        (projection_root / "agents").mkdir(parents=True, exist_ok=True)
        runtime = self._start_runtime(
            enable_application_state=True,
            projection_root=projection_root,
            projection_resolver=_resolve_endpoint,
        )
        assert runtime.application_database_path is not None
        client, descriptor, credential = self._open_session(runtime)
        with client.session() as session:
            _authenticate(session, descriptor, credential)
            _save_connection(
                session, connection_id=CONNECTION_ID, provider_id=PROVIDER_ID,
                expected_revision=0, idempotency_key="conn-rm",
            )
            registered = _register_model(
                session, connection_id=CONNECTION_ID, provider_model_id=MODEL_ID,
                display_name="Test", expected_revision=0, idempotency_key="reg-rm",
            )
            registration_id = registered["result"]["model"]["registration_id"]
            revision = registered["result"]["model"]["revision"]
            self.assertEqual(len(_list_agent_files(projection_root)), 1)
            _remove_model(
                session, registration_id=registration_id,
                expected_revision=revision, idempotency_key="rm",
            )
            status = _projection_status(session, host_id=CONSUMER_ID)
            self.assertEqual(status["result"]["status"], "ready")
        self.assertEqual(_list_agent_files(projection_root), [])

    # --- Test 5: restart recovers pending ---
    def test_restart_recovers_pending(self) -> None:
        projection_root = self._temp_dir()
        (projection_root / "agents").mkdir(parents=True, exist_ok=True)
        state = self._temp_dir()
        artifact = self._temp_dir()
        socket_root = self._temp_dir()
        database_path = state / "engine" / "state.sqlite3"
        SQLiteConnectionRepository(database_path).save(
            SaveConnectionCommand(
                connection_id=CONNECTION_ID,
                provider_id=PROVIDER_ID,
                expected_revision=0,
                idempotency_key="conn-restart",
            )
        )
        SQLiteModelRepository(database_path).register(
            RegisterModelCommand(
                connection_id=CONNECTION_ID,
                provider_model_id=MODEL_ID,
                display_name="Restart",
                expected_revision=0,
                idempotency_key="reg-restart",
            )
        )
        self.assertEqual(_list_agent_files(projection_root), [])
        self.assertTrue(any(row[6] == "pending" for row in _fetch_outbox_rows(database_path)))

        runtime = self._start_runtime(
            enable_application_state=True,
            projection_root=projection_root,
            projection_resolver=_resolve_endpoint,
            state=state,
            artifact=artifact,
            socket_root=socket_root,
        )
        client, descriptor, credential = self._open_session(runtime)
        with client.session() as session:
            _authenticate(session, descriptor, credential)
            status = _projection_status(session, host_id=CONSUMER_ID)
            self.assertEqual(status["result"]["status"], "ready")
        self.assertEqual(len(_list_agent_files(projection_root)), 1)

    # --- Test 6: foreign-file conflict ---
    def test_foreign_file_conflict(self) -> None:
        # First, determine the deterministic filename the renderer will pick.
        scratch_root = self._temp_dir()
        (scratch_root / "agents").mkdir(parents=True, exist_ok=True)
        runtime_for_name = self._start_runtime(
            enable_application_state=True,
            projection_root=scratch_root,
            projection_resolver=_resolve_endpoint,
        )
        client_r, descriptor_r, credential_r = self._open_session(runtime_for_name)
        with client_r.session() as session:
            _authenticate(session, descriptor_r, credential_r)
            _save_connection(
                session, connection_id=CONNECTION_ID, provider_id=PROVIDER_ID,
                expected_revision=0, idempotency_key="conn-fname",
            )
            _register_model(
                session, connection_id=CONNECTION_ID, provider_model_id=MODEL_ID,
                display_name="Conflict", expected_revision=0, idempotency_key="reg-fname",
            )
        files = _list_agent_files(scratch_root)
        self.assertEqual(len(files), 1)
        target_filename = files[0].name

        # Now create the conflict scenario against a fresh projection root.
        projection_root = self._temp_dir()
        agents_dir = projection_root / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        agents_dir.joinpath(target_filename).write_bytes(b"# not a managed agent\n[foreign]\n")

        state = self._temp_dir()
        artifact = self._temp_dir()
        socket_root = self._temp_dir()
        runtime = self._start_runtime(
            enable_application_state=True,
            projection_root=projection_root,
            projection_resolver=_resolve_endpoint,
            state=state,
            artifact=artifact,
            socket_root=socket_root,
        )
        client, descriptor, credential = self._open_session(runtime)
        with client.session() as session:
            _authenticate(session, descriptor, credential)
            _save_connection(
                session, connection_id=CONNECTION_ID, provider_id=PROVIDER_ID,
                expected_revision=0, idempotency_key="conn-conflict",
            )
            registered = _register_model(
                session, connection_id=CONNECTION_ID, provider_model_id=MODEL_ID,
                display_name="Conflict", expected_revision=0, idempotency_key="reg-conflict",
            )
            self.assertIn("result", registered)
            status = _projection_status(session, host_id=CONSUMER_ID)
            self.assertEqual(status["result"]["status"], "failed")
            self.assertEqual(
                agents_dir.joinpath(target_filename).read_bytes(),
                b"# not a managed agent\n[foreign]\n",
            )
            model = registered["result"]["model"]
        runtime.server.stop()

        restarted = self._start_runtime(
            enable_application_state=True,
            projection_root=projection_root,
            projection_resolver=_resolve_endpoint,
            state=state,
            artifact=artifact,
            socket_root=socket_root,
        )
        client, descriptor, credential = self._open_session(restarted)
        with client.session() as session:
            _authenticate(session, descriptor, credential)
            persisted_status = _projection_status(session, host_id=CONSUMER_ID)
            self.assertEqual(persisted_status["result"]["status"], "failed")
            agents_dir.joinpath(target_filename).unlink()
            recovered = _rename_model(
                session,
                registration_id=model["registration_id"],
                display_name="Conflict resolved",
                expected_revision=model["revision"],
                idempotency_key="conflict-retry",
            )
            self.assertIn("result", recovered)
            recovered_status = _projection_status(session, host_id=CONSUMER_ID)
            self.assertEqual(recovered_status["result"]["status"], "ready")
        on_disk = agents_dir.joinpath(target_filename).read_bytes()
        self.assertTrue(on_disk.startswith(MANAGED_AGENT_MARKER))
        self.assertIn(b"Conflict resolved", on_disk)

    def test_staged_mcp_entrypoint_adds_renames_and_removes_projection(self) -> None:
        projection_root = self._temp_dir()
        (projection_root / "agents").mkdir(parents=True, exist_ok=True)
        runtime = self._start_runtime(
            enable_application_state=True,
            projection_root=projection_root,
            projection_resolver=_resolve_endpoint,
        )
        client, descriptor, credential = self._open_session(runtime)
        with client.session() as session:
            _authenticate(session, descriptor, credential)
            saved = _save_connection(
                session,
                connection_id=CONNECTION_ID,
                provider_id=PROVIDER_ID,
                expected_revision=0,
                idempotency_key="conn-mcp",
            )
            self.assertIn("result", saved)

        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["MODEL_DECK_ENGINE_RENDEZVOUS_PATH"] = str(runtime.rendezvous_path)
        environment["MODEL_DECK_ENGINE_CREDENTIAL_PATH"] = str(runtime.enrollment.credential_path)
        stage = self._temp_dir()
        for filename in (
            "model_deck_mcp.py",
            "codex_settings.py",
            "pricing.py",
            "model_benchmarks.py",
            "provider_connections.py",
            "routing_registry.py",
        ):
            shutil.copyfile(PROJECT_ROOT / filename, stage / filename)
        shutil.copytree(
            PROJECT_ROOT / "python" / "src",
            stage / "vendor",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        process = subprocess.Popen(
            [sys.executable, "-B", str(stage / "model_deck_mcp.py")],
            cwd=self._temp_dir(),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            added = _mcp_tool_call(
                process, 1, "add_model",
                {"model": MODEL_ID, "display_name": "MCP model"},
            )
            self.assertFalse(added["result"]["isError"], added)
            files = _list_agent_files(projection_root)
            self.assertEqual(len(files), 1)
            self.assertIn(b"MCP model", files[0].read_bytes())
            listed = _mcp_tool_call(process, 2, "list_added_models", {})
            listed_payload = json.loads(listed["result"]["content"][0]["text"])
            self.assertEqual(
                [row["id"] for row in listed_payload["models"]],
                [MODEL_ID],
            )

            renamed = _mcp_tool_call(
                process, 3, "set_display_name",
                {"model": MODEL_ID, "name": "MCP renamed"},
            )
            self.assertFalse(renamed["result"]["isError"], renamed)
            self.assertIn(b"MCP renamed", files[0].read_bytes())

            removed = _mcp_tool_call(process, 4, "remove_model", {"model": MODEL_ID})
            self.assertFalse(removed["result"]["isError"], removed)
            self.assertEqual(_list_agent_files(projection_root), [])
            empty = _mcp_tool_call(process, 5, "list_added_models", {})
            empty_payload = json.loads(empty["result"]["content"][0]["text"])
            self.assertEqual(empty_payload["models"], [])
        finally:
            process.terminate()
            process.communicate(timeout=5)
