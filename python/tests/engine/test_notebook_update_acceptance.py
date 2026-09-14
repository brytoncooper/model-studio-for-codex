"""Packaged Session Notebook update acceptance.

These tests intentionally exercise the public socket and lifecycle surfaces;
the example package is copied before making candidate archives.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from model_deck.bootstrap import build_engine_server
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.plugins.authoring import pack_project_archive
from model_deck_contracts.paths import repo_root


class NotebookUpdateAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temps: list[tempfile.TemporaryDirectory[str]] = []
        self._runtime = None

    def tearDown(self) -> None:
        if self._runtime is not None:
            self._runtime.server.stop()
        for item in reversed(self._temps):
            item.cleanup()

    def _temporary_directory(self) -> Path:
        item = tempfile.TemporaryDirectory(prefix="md-notebook-update-", dir="/tmp")
        self._temps.append(item)
        return Path(item.name)

    def _rpc_call(self, session, number: int, method: str, params: dict) -> dict:
        request = {"jsonrpc": "2.0", "id": number, "method": method, "params": params}
        return session.call(request)

    def _invoke_operation(
        self, session, number: int, operation: str, input_data: dict, key: str
    ) -> dict:
        response = self._rpc_call(
            session,
            number,
            "engine.v1.operations.invoke",
            {"operation": operation, "input": input_data, "idempotency_key": key},
        )
        return response["result"]["output"]

    def _extension_record(self, session, number: int) -> dict:
        return self._rpc_call(
            session,
            number,
            "engine.v1.extensions.get",
            {"extension_id": "org.example.notebook"},
        )["result"]

    def _start_engine(self, roots=None):
        if roots is None:
            roots = self._new_roots()
        self._runtime = build_engine_server(
            state_root=roots["state"],
            artifact_root=roots["artifacts"],
            socket_root=roots["sockets"],
            legacy_agents_dir=roots["legacy"],
            default_connection_id=str(uuid4()),
            source_root=repo_root(),
            enable_application_state=True,
            enable_external_extensions=True,
            extension_state_root=roots["extension_state"],
            extension_artifact_root=roots["extension_artifacts"],
        )
        self._runtime.server.start()
        descriptor = load_rendezvous_file(self._runtime.rendezvous_path)
        credential = self._runtime.enrollment.credential_path.read_text().strip()
        context = UnixSocketEngineClient(descriptor.socket_path).session()
        session = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        hello = self._rpc_call(
            session,
            1,
            "engine.v1.hello",
            {
                "client_name": "notebook-update-acceptance",
                "offered_api": {"major": 1, "minor": 0},
                "authentication": {
                    "engine_instance_id": descriptor.engine_instance_id,
                    "instance_nonce": descriptor.instance_nonce,
                    "credential": credential,
                },
            },
        )
        self.assertTrue(hello["result"]["authenticated"])
        return session

    def _new_roots(self) -> dict[str, Path]:
        return {
            "state": self._temporary_directory(),
            "artifacts": self._temporary_directory(),
            "sockets": self._temporary_directory(),
            "legacy": self._temporary_directory(),
            "extension_state": self._temporary_directory(),
            "extension_artifacts": self._temporary_directory(),
        }

    def _restart(self, roots: dict[str, Path]):
        self._runtime.server.stop()
        self._runtime = None
        restarted = dict(roots)
        restarted["sockets"] = self._temporary_directory()
        return self._start_engine(restarted)

    def _package_candidate(self, version: str, *, broken: bool = False) -> Path:
        source = Path(__file__).resolve().parents[3] / "examples" / "session-notebook"
        copy = self._temporary_directory() / version
        shutil.copytree(source, copy)
        manifest = json.loads((copy / "manifest.json").read_text())
        manifest["version"] = version
        (copy / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        plugin = copy / "plugin.py"
        worker_version = "0.0.0" if broken else version
        text = plugin.read_text().replace(
            'PLUGIN_VERSION = "1.0.0"',
            f'PLUGIN_VERSION = "{worker_version}"',
        )
        text = text.replace('"Session Notebook"', f'"Session Notebook {version}"', 1)
        text = text.replace('"Refresh notes"', f'"Refresh notes {version}"', 1)
        plugin.write_text(text)
        panel_schema = copy / "panels" / "notebook-list.json"
        panel_schema.write_text(
            panel_schema.read_text().replace("Refresh notes", f"Refresh notes {version}", 1)
        )
        archive = self._temporary_directory() / f"{version}.zip"
        pack_project_archive(copy, output_path=archive)
        return archive

    def test_update_preserves_notes_and_serves_new_panel_marker(self) -> None:
        roots = self._new_roots()
        session = self._start_engine(roots)
        a = self._package_candidate("1.0.0")
        b = self._package_candidate("1.1.0")
        install = self._rpc_call(session, 2, "engine.v1.extensions.install", {
            "archive_path": str(a), "idempotency_key": "i", "expected_revision": 0,
        })
        self.assertEqual(install["result"]["version"], "1.0.0")
        record = self._extension_record(session, 3)
        self._rpc_call(session, 4, "engine.v1.extensions.enable", {
            "extension_id": "org.example.notebook",
            "expected_revision": record["revision"],
            "idempotency_key": "e",
        })
        note = self._invoke_operation(session, 5, "org.example.notebook.notes.create", {
            "title": "Δ", "body": "before",
            "metadata": {"kind": "unicode-✓", "nested": [1, True]},
        }, "n")
        self._invoke_operation(session, 6, "org.example.notebook.notes.update", {
            "note_id": note["note_id"], "expected_revision": note["revision"],
            "title": "Δ", "body": "edited",
            "metadata": {"kind": "unicode-✓", "nested": [1, True]},
        }, "u")
        current = self._extension_record(session, 7)
        updated = self._rpc_call(session, 8, "engine.v1.extensions.update", {
            "extension_id": "org.example.notebook", "archive_path": str(b),
            "expected_revision": current["revision"], "idempotency_key": "update-b",
        })
        self.assertEqual(updated["result"]["version"], "1.1.0")
        panel = self._rpc_call(session, 9, "engine.v1.ui.panel.get", {
            "panel_id": "org.example.notebook.list",
        })["result"]["panel"]
        self.assertIn("1.1.0", json.dumps(panel))
        got = self._invoke_operation(session, 10, "org.example.notebook.notes.get", {
            "note_id": note["note_id"],
        }, "g")
        self.assertEqual(
            (got["body"], got["metadata"], got["revision"]),
            ("edited", {"kind": "unicode-✓", "nested": [1, True]}, 2),
        )
        extra = self._invoke_operation(session, 11, "org.example.notebook.notes.create", {
            "title": "After", "body": "post-update", "metadata": {"source": "B"},
        }, "after")
        self._invoke_operation(session, 12, "org.example.notebook.notes.update", {
            "note_id": extra["note_id"], "expected_revision": extra["revision"],
            "title": "After", "body": "post-edit", "metadata": {"source": "B"},
        }, "after-edit")
        export = self._rpc_call(session, 13, "engine.v1.operations.invoke", {
            "operation": "org.example.notebook.export.start", "input": {},
            "idempotency_key": "export",
        })["result"]
        for request_id in range(14, 80):
            snapshot = self._rpc_call(session, request_id, "engine.v1.jobs.get", {
                "job_id": export["job_id"],
            })["result"]
            if snapshot["state"] == "completed":
                self.assertIn("edited", snapshot["output"]["content"])
                self.assertIn("post-edit", snapshot["output"]["content"])
                break
            time.sleep(0.01)
        else:
            self.fail("export did not complete")
        session = self._restart(roots)
        self.assertEqual(self._extension_record(session, 80)["version"], "1.1.0")
        persisted = self._invoke_operation(session, 81, "org.example.notebook.notes.get", {
            "note_id": note["note_id"],
        }, "restart-get")
        self.assertEqual(persisted["body"], "edited")
        panel = self._rpc_call(session, 82, "engine.v1.ui.panel.get", {
            "panel_id": "org.example.notebook.list",
        })["result"]["panel"]
        self.assertEqual(panel["root"]["children"][1]["label"], "Refresh notes 1.1.0")

    def test_late_worker_failure_rolls_back_to_selected_version(self) -> None:
        roots = self._new_roots()
        session = self._start_engine(roots)
        a = self._package_candidate("1.0.0")
        b = self._package_candidate("1.1.0")
        c = self._package_candidate("2.0.0", broken=True)
        self._rpc_call(session, 2, "engine.v1.extensions.install", {
            "archive_path": str(a), "idempotency_key": "i", "expected_revision": 0,
        })
        record = self._extension_record(session, 3)
        self._rpc_call(session, 4, "engine.v1.extensions.enable", {
            "extension_id": "org.example.notebook",
            "expected_revision": record["revision"], "idempotency_key": "e",
        })
        current = self._extension_record(session, 5)
        self._rpc_call(session, 6, "engine.v1.extensions.update", {
            "extension_id": "org.example.notebook", "archive_path": str(b),
            "expected_revision": current["revision"], "idempotency_key": "update-b",
        })
        current = self._extension_record(session, 7)
        note = self._invoke_operation(session, 8, "org.example.notebook.notes.create", {
            "title": "Retained", "body": "safe", "metadata": {},
        }, "retained")
        failure = self._rpc_call(session, 9, "engine.v1.extensions.update", {
            "extension_id": "org.example.notebook", "archive_path": str(c),
            "expected_revision": current["revision"], "idempotency_key": "update-c",
        })
        self.assertEqual(failure["error"]["data"]["code"], "conflict")
        self.assertEqual(self._extension_record(session, 10)["version"], "1.1.0")
        retained = self._invoke_operation(session, 11, "org.example.notebook.notes.get", {
            "note_id": note["note_id"],
        }, "read")
        self.assertEqual(retained["body"], "safe")
        edited = self._invoke_operation(session, 12, "org.example.notebook.notes.update", {
            "note_id": note["note_id"], "expected_revision": retained["revision"],
            "title": "Retained", "body": "edited after failed update", "metadata": {},
        }, "edit-after-failure")
        self.assertEqual(edited["revision"], 2)
        session = self._restart(roots)
        self.assertEqual(self._extension_record(session, 13)["version"], "1.1.0")
        persisted = self._invoke_operation(session, 14, "org.example.notebook.notes.get", {
            "note_id": note["note_id"],
        }, "restart-read")
        self.assertEqual(persisted["body"], "edited after failed update")
