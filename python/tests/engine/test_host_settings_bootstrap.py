from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import build_engine_server
from model_deck.engine.host_settings.ports import CallerContext, READ_GRANT, WRITE_GRANT
from model_deck.integrations.hosts.codex.settings_document import DocumentContext, FieldSpec, SectionSpec, sha256_hex
from model_deck.integrations.hosts.codex.settings_file import CodexSettingsFile, SettingsFileSpecs
from model_deck_contracts.paths import repo_root


BASE = b'# preserve\nmodel = "before"\nunknown = "untouched"\n'
AFTER = BASE.replace(b'"before"', b'"after"')
CALLER = CallerContext("fixture-local-operator", frozenset({READ_GRANT, WRITE_GRANT}))


class HostSettingsBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temps = []
        self.runtimes = []
        self.state = self.directory()
        self.artifacts = self.directory()
        self.sockets = self.directory()
        self.documents = self.directory()
        self.backups = self.directory()
        self.config = self.documents / "config.toml"
        self.config.write_bytes(BASE)
        self.adapter = CodexSettingsFile(config_file=self.config, backup_dir=self.backups, context_specs=self.specs)
        self.sequence = 0

    def tearDown(self):
        for runtime in reversed(self.runtimes):
            runtime.server.stop()
        for temporary in reversed(self.temps):
            temporary.cleanup()

    def directory(self):
        temporary = tempfile.TemporaryDirectory()
        self.temps.append(temporary)
        return Path(temporary.name).resolve()

    def specs(self, source, exists):
        return SettingsFileSpecs(
            DocumentContext(host_id="codex.cli", document_id="config.toml",
                            document_revision=sha256_hex(source) if exists else "absent", exists=exists,
                            target={"display_name": "Fixture", "display_path": "fixture/config.toml", "scope": "user", "writable": True},
                            schema_profile={"schema_id": "codex", "schema_revision": "r1", "host_version": "v1", "support_level": "supported"},
                            precedence=[], context_revision="fixture-context"),
            (SectionSpec("general", "General", ("model",)),),
            (FieldSpec("model", "Model", "string", ("model",)),),
        )

    def build(self, **extra):
        return build_engine_server(state_root=self.state, artifact_root=self.artifacts,
                                   socket_root=self.sockets, legacy_agents_dir=self.documents,
                                   default_connection_id="550e8400-e29b-41d4-a716-446655440002",
                                   source_root=repo_root(), **extra)

    def start(self, configured=True):
        runtime = self.build(**({"host_settings_document": self.adapter, "host_settings_caller": CALLER} if configured else {}))
        runtime.server.start()
        self.runtimes.append(runtime)
        return runtime

    def session(self, runtime):
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        return UnixSocketEngineClient(descriptor.socket_path, timeout_seconds=5).session()

    def authenticate(self, session, runtime):
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        response = self.call(session, "hello", {"client_name": "settings-fixture", "offered_api": {"major": 1, "minor": 0},
                                               "authentication": {"engine_instance_id": descriptor.engine_instance_id,
                                                                  "instance_nonce": descriptor.instance_nonce,
                                                                  "credential": runtime.enrollment.credential_path.read_text().strip()}})
        self.assertTrue(response["result"]["authenticated"])

    def call(self, session, operation, params):
        self.sequence += 1
        return session.call({"jsonrpc": "2.0", "id": self.sequence, "method": "engine.v1." + operation, "params": params})

    def preview_params(self, snapshot):
        return {"host_id": snapshot["host_id"], "document_id": snapshot["document_id"],
                "expected_content_hash": snapshot["document_revision"], "context_revision": snapshot["context_revision"],
                "draft": {"kind": "structured", "changes": [{"field_id": "model", "operation": "set", "value": "after"}]}}

    def save_params(self, base, preview):
        return {key: value for key, value in base.items() if key != "draft"} | {
            "preview_id": preview["preview"]["preview_id"], "candidate_content_hash": preview["candidate_content_hash"],
            "candidate_raw_toml": preview["candidate_raw_toml"], "idempotency_key": str(uuid4())}

    def test_socket_save_backup_and_exact_replay_after_restart(self):
        runtime = self.start()
        with self.session(runtime) as session:
            self.authenticate(session, runtime)
            snapshot = self.call(session, "hosts.settings.read", {"host_id": "codex.cli"})["result"]["snapshot"]
            params = self.preview_params(snapshot)
            preview = self.call(session, "hosts.settings.preview", params)["result"]
            self.assertEqual(UUID(preview["preview"]["preview_id"]).version, 4)
            save = self.save_params(params, preview)
            first = self.call(session, "hosts.settings.save", save)["result"]
            replay = self.call(session, "hosts.settings.save", save)["result"]
            self.assertEqual(first, replay)
        self.assertEqual(self.config.read_bytes(), AFTER)
        backup = Path(first["backup"]["display_path"])
        self.assertEqual(backup.read_bytes(), BASE)
        self.assertEqual(list(self.backups.iterdir()), [backup])
        inode = self.config.stat().st_ino
        runtime.server.stop()
        self.runtimes.remove(runtime)
        runtime = self.start()
        with self.session(runtime) as session:
            self.authenticate(session, runtime)
            replay = self.call(session, "hosts.settings.save", save)["result"]
            self.assertEqual(replay, first)
            stale = self.call(session, "hosts.settings.preview", params)
            self.assertEqual(stale["error"]["data"]["code"], "conflict")
        self.assertEqual(self.config.stat().st_ino, inode)
        self.assertEqual(self.config.read_bytes(), AFTER)
        self.assertEqual(list(self.backups.iterdir()), [backup])
        database = self.state / "engine" / "host-settings.sqlite3"
        with sqlite3.connect(database) as connection:
            rows = connection.execute("SELECT principal, settled FROM host_save_receipts").fetchall()
        self.assertEqual(rows, [(CALLER.principal, 1)])

    def test_preview_admission_survives_restart_before_save(self):
        runtime = self.start()
        with self.session(runtime) as session:
            self.authenticate(session, runtime)
            snapshot = self.call(session, "hosts.settings.read", {"host_id": "codex.cli"})["result"]["snapshot"]
            params = self.preview_params(snapshot)
            preview = self.call(session, "hosts.settings.preview", params)["result"]
            save = self.save_params(params, preview)
        runtime.server.stop()
        self.runtimes.remove(runtime)
        runtime = self.start()
        with self.session(runtime) as session:
            self.authenticate(session, runtime)
            response = self.call(session, "hosts.settings.save", save)
        self.assertTrue(response["result"]["saved"])
        self.assertEqual(self.config.read_bytes(), AFTER)
        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_external_change_after_preview_conflicts_without_write(self):
        runtime = self.start()
        with self.session(runtime) as session:
            self.authenticate(session, runtime)
            snapshot = self.call(session, "hosts.settings.read", {"host_id": "codex.cli"})["result"]["snapshot"]
            params = self.preview_params(snapshot)
            preview = self.call(session, "hosts.settings.preview", params)["result"]
            save = self.save_params(params, preview)
            self.config.write_bytes(b"external = true\n")
            response = self.call(session, "hosts.settings.save", save)
        self.assertEqual(response["error"]["data"]["code"], "conflict")
        self.assertEqual(self.config.read_bytes(), b"external = true\n")
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_authentication_required_and_caller_cannot_be_supplied_in_params(self):
        runtime = self.start()
        with self.session(runtime) as session:
            response = self.call(session, "hosts.settings.read", {"host_id": "codex.cli"})
            self.assertIn("error", response)
            self.authenticate(session, runtime)
            response = self.call(session, "hosts.settings.read", {"host_id": "codex.cli", "caller": {"principal": "forged"}})
            self.assertIn("error", response)
        self.assertEqual(self.config.read_bytes(), BASE)

    def test_default_does_not_advertise_or_create_settings_ledger(self):
        runtime = self.start(configured=False)
        with self.session(runtime) as session:
            self.authenticate(session, runtime)
            response = self.call(session, "operations.list", {})
            self.assertFalse(any("hosts.settings" in entry["operation_id"] for entry in response["result"]["operations"]))
        self.assertFalse((self.state / "engine" / "host-settings.sqlite3").exists())
        self.assertEqual(self.config.read_bytes(), BASE)

    def test_partial_or_unprivileged_configuration_rejects_before_state_creation(self):
        for args in ({"host_settings_document": self.adapter}, {"host_settings_caller": CALLER},
                     {"host_settings_document": self.adapter, "host_settings_caller": CallerContext("operator", frozenset({READ_GRANT}))},
                     {"host_settings_document": self.adapter, "host_settings_caller": CallerContext("", CALLER.grants)}):
            with self.subTest(args=tuple(args)), self.assertRaises(ValueError):
                self.build(**args)
        self.assertEqual(list(self.state.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
