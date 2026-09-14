from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.adapters.platform.macos.extension_lease import ExtensionEngineLease
from model_deck.adapters.platform.macos.instance_lock import FileInstanceLock
from model_deck.adapters.storage.sqlite_extension_lifecycle import SQLiteExtensionLifecycleRepository
from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.adapters.storage.sqlite_versioned_plugin_data import SQLiteVersionedPluginDataStore
from model_deck.bootstrap import build_engine_server
from model_deck.engine.server import EngineServer
from model_deck.engine.jobs import GetJobCommand, JobState
from model_deck.plugins.authoring import pack_project_archive
from model_deck.plugins.external_host import ExternalExtensionHost, HostDependencies
from model_deck_contracts.paths import repo_root


class ExternalExtensionTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temps: list[tempfile.TemporaryDirectory[str]] = []
        self._runtime = None

    def tearDown(self) -> None:
        if self._runtime is not None:
            self._runtime.server.stop()
        for temporary in reversed(self._temps):
            temporary.cleanup()

    def _directory(self) -> Path:
        temporary = tempfile.TemporaryDirectory(prefix="mdx-", dir="/tmp")
        self._temps.append(temporary)
        return Path(temporary.name).resolve()

    def _call(self, session, request_id: int, method: str, params: dict) -> dict:
        return session.call(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )

    def _authenticated_session(self):
        descriptor = load_rendezvous_file(self._runtime.rendezvous_path)
        credential = self._runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        session_context = client.session()
        session = session_context.__enter__()
        self.addCleanup(session_context.__exit__, None, None, None)
        response = self._call(
            session,
            1,
            "engine.v1.hello",
            {
                "client_name": "extension-transport-test",
                "offered_api": {"major": 1, "minor": 0},
                "authentication": {
                    "engine_instance_id": descriptor.engine_instance_id,
                    "instance_nonce": descriptor.instance_nonce,
                    "credential": credential,
                },
            },
        )
        self.assertTrue(response["result"]["authenticated"])
        return session

    def test_packed_notebook_lifecycle_and_generic_invocation_over_socket(self) -> None:
        state = self._directory()
        artifacts = self._directory()
        sockets = self._directory()
        legacy = self._directory()
        extension_state = self._directory()
        extension_artifacts = self._directory()
        archive = self._directory() / "notebook.zip"
        project_root = Path(__file__).resolve().parents[3]
        pack_project_archive(project_root / "examples" / "session-notebook", output_path=archive)

        self._runtime = build_engine_server(
            state_root=state,
            artifact_root=artifacts,
            socket_root=sockets,
            legacy_agents_dir=legacy,
            default_connection_id=str(uuid4()),
            source_root=repo_root(),
            enable_application_state=True,
            enable_external_extensions=True,
            extension_state_root=extension_state,
            extension_artifact_root=extension_artifacts,
        )
        self._runtime.server.start()
        session = self._authenticated_session()

        installed = self._call(session, 2, "engine.v1.extensions.install", {
            "archive_path": str(archive), "idempotency_key": "install", "expected_revision": 0,
        })["result"]
        self.assertEqual(installed, {"extension_id": "org.example.notebook", "version": "1.0.0"})
        record = self._call(session, 3, "engine.v1.extensions.get", {
            "extension_id": "org.example.notebook",
        })["result"]
        listed = self._call(session, 30, "engine.v1.extensions.list", {})["result"]
        self.assertEqual(listed["extensions"], [{
            "extension_id": "org.example.notebook", "status": "installed",
        }])
        forged_principal = self._call(session, 31, "engine.v1.extensions.get", {
            "extension_id": "org.example.notebook", "principal": "attacker-selected",
        })
        self.assertEqual(forged_principal["error"]["code"], -32602)
        enabled = self._call(session, 4, "engine.v1.extensions.enable", {
            "extension_id": "org.example.notebook", "expected_revision": record["revision"],
            "idempotency_key": "enable",
        })
        self.assertEqual(enabled["result"], {"enabled": True})

        operations = self._call(session, 5, "engine.v1.operations.list", {})["result"]["operations"]
        self.assertIn("org.example.notebook.notes.create", [item["operation_id"] for item in operations])
        filtered_operations = self._call(session, 51, "engine.v1.operations.list", {
            "plugin_id": "org.example.notebook",
        })["result"]["operations"]
        filtered_ids = {item["operation_id"] for item in filtered_operations}
        self.assertTrue({
            "org.example.notebook.notes.create",
            "org.example.notebook.notes.list",
            "org.example.notebook.notes.get",
            "org.example.notebook.notes.update",
            "org.example.notebook.notes.delete",
            "org.example.notebook.export.start",
        }.issubset(filtered_ids))
        self.assertTrue(all(
            item["operation_id"].startswith("org.example.notebook.")
            for item in filtered_operations
        ))
        direct = self._call(session, 50, "org.example.notebook.notes.create", {
            "title": "Bypass", "body": "Not allowed", "metadata": {},
        })
        self.assertEqual(direct["error"]["data"]["code"], "unsupported_capability")
        panels = self._call(session, 6, "engine.v1.ui.contributions.list", {
            "extension_id": "org.example.notebook",
        })["result"]["panels"]
        self.assertEqual({panel["panel_id"] for panel in panels}, {
            "org.example.notebook.list", "org.example.notebook.editor",
        })
        panel = self._call(session, 7, "engine.v1.ui.panel.get", {
            "panel_id": "org.example.notebook.list",
        })["result"]["panel"]
        self.assertEqual(panel["panel_id"], "org.example.notebook.list")
        created_result = self._call(session, 8, "engine.v1.operations.invoke", {
            "operation": "org.example.notebook.notes.create",
            "input": {"title": "First", "body": "Retained", "metadata": {}},
            "idempotency_key": "create",
        })["result"]
        created = created_result["output"]
        self.assertEqual(created["body"], "Retained")
        self.assertEqual(created_result["panel"]["panel_id"], "org.example.notebook.editor")
        first_update = self._call(session, 81, "engine.v1.operations.invoke", {
            "operation": "org.example.notebook.notes.update",
            "input": {
                "note_id": created["note_id"], "expected_revision": created["revision"],
                "title": "First", "body": "Edited once", "metadata": {},
            },
            "idempotency_key": "update-once",
        })["result"]
        second_update = self._call(session, 82, "engine.v1.operations.invoke", {
            "operation": "org.example.notebook.notes.update",
            "input": {
                "note_id": created["note_id"],
                "expected_revision": first_update["output"]["revision"],
                "title": "First", "body": "Edited twice", "metadata": {},
            },
            "idempotency_key": "update-twice",
        })["result"]
        self.assertEqual(second_update["output"]["revision"], 3)
        self.assertEqual(
            second_update["panel"]["root"]["children"][2]["params"]["expected_revision"],
            3,
        )
        stale = self._call(session, 83, "engine.v1.operations.invoke", {
            "operation": "org.example.notebook.notes.update",
            "input": {
                "note_id": created["note_id"],
                "expected_revision": first_update["output"]["revision"],
                "title": "Stale", "body": "Must not win", "metadata": {},
            },
            "idempotency_key": "update-stale",
        })
        self.assertEqual(stale["error"]["data"]["code"], "conflict")
        after_conflict = self._call(session, 84, "engine.v1.operations.invoke", {
            "operation": "org.example.notebook.notes.get",
            "input": {"note_id": created["note_id"]},
            "idempotency_key": "get-after-conflict",
        })["result"]["output"]
        self.assertEqual(after_conflict["body"], "Edited twice")

        for index in range(40):
            self._call(session, 100 + index, "engine.v1.operations.invoke", {
                "operation": "org.example.notebook.notes.create",
                "input": {
                    "title": f"Export note {index}",
                    "body": f"Durable export body {index}",
                    "metadata": {},
                },
                "idempotency_key": f"export-note-{index}",
            })["result"]

        cancelled_export = self._call(
            session,
            200,
            "engine.v1.operations.invoke",
            {
                "operation": "org.example.notebook.export.start",
                "input": {},
                "idempotency_key": "export-cancelled",
            },
        )["result"]
        cancelled_job_id = cancelled_export["job_id"]
        self.assertEqual(cancelled_export["output"]["job_id"], cancelled_job_id)
        cancel_acknowledgement = self._call(
            session,
            201,
            "engine.v1.jobs.cancel",
            {
                "job_id": cancelled_job_id,
                "idempotency_key": "cancel-export",
            },
        )["result"]
        self.assertTrue(cancel_acknowledgement["accepted"])
        cancelled_snapshot = self._wait_for_job_terminal(
            session, cancelled_job_id, request_id_start=210
        )
        self.assertEqual(cancelled_snapshot["state"], "cancelled")
        self.assertNotIn("output", cancelled_snapshot)

        completed_export = self._call(
            session,
            300,
            "engine.v1.operations.invoke",
            {
                "operation": "org.example.notebook.export.start",
                "input": {},
                "idempotency_key": "export-completed",
            },
        )["result"]
        completed_snapshot = self._wait_for_job_terminal(
            session, completed_export["job_id"], request_id_start=310
        )
        self.assertEqual(completed_snapshot["state"], "completed")
        self.assertEqual(completed_snapshot["progress"], 1.0)
        self.assertEqual(completed_snapshot["output"]["media_type"], "text/markdown")
        self.assertEqual(
            completed_snapshot["output"]["suggested_filename"],
            "session-notebook.md",
        )
        markdown = completed_snapshot["output"]["content"]
        self.assertIn("# Session Notebook", markdown)
        self.assertIn("## First", markdown)
        self.assertIn("Edited twice", markdown)
        self.assertIn("## Export note 39", markdown)

        interrupted_export = self._call(
            session,
            400,
            "engine.v1.operations.invoke",
            {
                "operation": "org.example.notebook.export.start",
                "input": {},
                "idempotency_key": "export-interrupted",
            },
        )["result"]
        interrupted_job_id = interrupted_export["job_id"]

        current = self._call(session, 9, "engine.v1.extensions.get", {
            "extension_id": "org.example.notebook",
        })["result"]
        self.assertEqual(self._call(session, 10, "engine.v1.extensions.disable", {
            "extension_id": "org.example.notebook", "expected_revision": current["revision"],
            "idempotency_key": "disable",
        })["result"], {"enabled": False})
        denied = self._call(session, 11, "engine.v1.operations.invoke", {
            "operation": "org.example.notebook.notes.get", "input": {"note_id": created["note_id"]},
            "idempotency_key": "disabled-get",
        })
        self.assertEqual(denied["error"]["data"]["code"], "plugin_unavailable")
        self.assertNotIn(created["note_id"], str(denied))
        after_disable = self._call(session, 12, "engine.v1.operations.list", {})["result"]["operations"]
        self.assertNotIn("org.example.notebook.notes.create", [item["operation_id"] for item in after_disable])

        interrupted_snapshot = self._call(
            session,
            401,
            "engine.v1.jobs.get",
            {"job_id": interrupted_job_id},
        )["result"]
        self.assertEqual(interrupted_snapshot["state"], "interrupted")
        self.assertNotIn("output", interrupted_snapshot)

        self._runtime.server.stop()
        reopened = ExternalExtensionHost(extension_state, artifact_root=extension_artifacts,
            dependencies=HostDependencies(SQLiteExtensionLifecycleRepository,
                lambda path: SQLitePluginJobRepository(path, checkpoint_validator=lambda _s, _v: None),
                SQLiteVersionedPluginDataStore, FileInstanceLock, ExtensionEngineLease))
        reopened.close()
        persisted_jobs = SQLitePluginJobRepository(
            extension_state / "host.sqlite3",
            checkpoint_validator=lambda _schema, _value: None,
        )
        self.assertEqual(
            persisted_jobs.get(GetJobCommand(interrupted_job_id)).state,
            JobState.INTERRUPTED,
        )

    def _wait_for_job_terminal(
        self,
        session,
        job_id: str,
        *,
        request_id_start: int,
    ) -> dict:
        deadline = time.monotonic() + 5.0
        request_id = request_id_start
        while time.monotonic() < deadline:
            snapshot = self._call(
                session,
                request_id,
                "engine.v1.jobs.get",
                {"job_id": job_id},
            )["result"]
            if snapshot["state"] in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            }:
                return snapshot
            request_id += 1
            time.sleep(0.01)
        self.fail(f"job {job_id} did not reach a terminal state")

    def test_external_extensions_are_opt_in_and_require_distinct_application_state(self) -> None:
        arguments = dict(
            state_root=self._directory(), artifact_root=self._directory(),
            socket_root=self._directory(), legacy_agents_dir=self._directory(),
            default_connection_id=str(uuid4()), source_root=repo_root(),
        )
        with self.assertRaisesRegex(ValueError, "application state"):
            build_engine_server(
                **arguments,
                enable_external_extensions=True,
                extension_state_root=self._directory(),
                extension_artifact_root=self._directory(),
            )

    def test_shutdown_callback_runs_after_listener_stop_and_before_lock_release(self) -> None:
        events: list[str] = []

        class Lock:
            def acquire(self, timeout):
                events.append("lock")
                return True

            def release(self):
                events.append("release")

        class Listener:
            def start(self):
                events.append("listen")

            def stop(self):
                events.append("stop-listener")

        server = EngineServer(
            Lock(),
            Listener(),
            lambda: {},
            lambda _: events.append("publish"),
            startup_callback=lambda: events.append("recover"),
            shutdown_callback=lambda: events.append("close-extensions"),
        )
        server.start()
        server.stop()
        server.stop()
        self.assertEqual(events, [
            "lock", "recover", "listen", "publish",
            "stop-listener", "close-extensions", "release",
        ])


if __name__ == "__main__":
    unittest.main()
