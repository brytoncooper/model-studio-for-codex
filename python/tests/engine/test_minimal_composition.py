"""A minimal engine must start and serve without loading a single vendor module.

The check runs in a subprocess: this test process has already imported host and
provider integrations for other suites, so only a fresh interpreter can say what
a minimal composition actually loads.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

VENDOR_PREFIXES = ("model_deck.integrations.", "model_deck.adapters.providers.")

_START_MINIMAL_ENGINE = '''
import json, sys
from pathlib import Path

from model_deck.bootstrap import build_engine_server
from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck_contracts.paths import repo_root

VENDOR_PREFIXES = ("model_deck.integrations.", "model_deck.adapters.providers.")

root = Path(sys.argv[1])
runtime = build_engine_server(
    state_root=root / "state",
    artifact_root=root / "artifact",
    socket_root=root / "socket",
    legacy_agents_dir=root / "agents",
    default_connection_id="550e8400-e29b-41d4-a716-446655440002",
    source_root=repo_root(),
)
runtime.server.start()
try:
    descriptor = load_rendezvous_file(runtime.rendezvous_path)
    with UnixSocketEngineClient(descriptor.socket_path).session() as session:
        hello = session.call({
            "jsonrpc": "2.0", "id": 1, "method": "engine.v1.hello",
            "params": {
                "client_name": "minimal-composition",
                "offered_api": {"major": 1, "minor": 0},
                "authentication": {
                    "engine_instance_id": descriptor.engine_instance_id,
                    "instance_nonce": descriptor.instance_nonce,
                    "credential": runtime.enrollment.credential_path.read_text().strip(),
                },
            },
        })
        models = session.call({
            "jsonrpc": "2.0", "id": 2, "method": "engine.v1.models.list",
            "params": {"collection": "registered"},
        })
        operations = session.call({
            "jsonrpc": "2.0", "id": 3, "method": "engine.v1.operations.list", "params": {},
        })
finally:
    runtime.server.stop()

print(json.dumps({
    "authenticated": hello["result"]["authenticated"],
    "models": models["result"],
    "operation_ids": sorted(row["operation_id"] for row in operations["result"]["operations"]),
    "engine_loaded": "model_deck.engine.dispatch" in sys.modules,
    "vendor_modules": sorted(
        name for name in sys.modules if name.startswith(VENDOR_PREFIXES)
    ),
}))
'''


class MinimalCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="minimal-composition-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def start_minimal_engine(self, name: str) -> dict:
        environment = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(entry for entry in sys.path if entry),
        }
        # Each run owns its roots: a reused state root carries an instance lock
        # and an enrollment credential from the previous engine.
        completed = subprocess.run(
            [sys.executable, "-c", _START_MINIMAL_ENGINE, str(self.root / name)],
            capture_output=True,
            text=True,
            timeout=180,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_vendor_prefixes_name_modules_that_really_exist(self):
        """Guard the guard: a typo in a prefix would make every assertion vacuous."""
        for module in (
            "model_deck.integrations.hosts.codex.legacy_models",
            "model_deck.adapters.providers.deterministic",
        ):
            with self.subTest(module=module):
                self.assertTrue(module.startswith(VENDOR_PREFIXES))
                self.assertIsNotNone(importlib.util.find_spec(module))

    def test_minimal_engine_serves_over_a_socket_without_a_vendor_module(self):
        report = self.start_minimal_engine("serving")
        self.assertTrue(report["engine_loaded"])
        self.assertTrue(report["authenticated"])
        self.assertEqual(report["models"], {"collection": "registered", "items": []})
        self.assertEqual(report["vendor_modules"], [])

    def test_minimal_engine_discovers_only_the_composed_core_operations(self):
        report = self.start_minimal_engine("discovery")
        self.assertEqual(
            report["operation_ids"],
            [
                "engine.v1.capabilities.get",
                "engine.v1.health",
                "engine.v1.hello",
                "engine.v1.models.list",
                "engine.v1.operations.list",
            ],
        )


if __name__ == "__main__":
    unittest.main()
