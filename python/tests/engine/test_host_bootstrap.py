from __future__ import annotations

import tempfile
import unittest
import plistlib
from pathlib import Path

from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import build_engine_server
from model_deck.integrations.hosts.codex.host_adapter import CodexHostAdapter
from model_deck_contracts.paths import repo_root


class _IdleProbe:
    def is_codex_running(self) -> bool:
        return False


class HostBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = [tempfile.TemporaryDirectory() for _ in range(5)]
        self.addCleanup(lambda: [item.cleanup() for item in reversed(self.temporary)])
        roots = [Path(item.name).resolve() for item in self.temporary]
        application = roots[4] / "ChatGPT.app"
        executable = application / "Contents/Resources/codex"
        executable.parent.mkdir(parents=True)
        executable.write_text("fixture, not executed", encoding="utf-8")
        executable.chmod(0o755)
        (application / "Contents/Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "com.openai.codex"})
        )
        host_integration = CodexHostAdapter(
            applications_dir=roots[4],
            observed_protocol_version="app-server.v1",
            supported_protocol_versions=frozenset({"app-server.v1"}),
            process_probe=_IdleProbe(),
            overrides=("-c", 'openai_base_url="http://127.0.0.1:1/v1"'),
        )
        self.runtime = build_engine_server(
            state_root=roots[0],
            artifact_root=roots[1],
            socket_root=roots[2],
            legacy_agents_dir=roots[3],
            default_connection_id="550e8400-e29b-41d4-a716-446655440002",
            source_root=repo_root(),
            host_integration=host_integration,
        )
        self.runtime.server.start()
        self.addCleanup(self.runtime.server.stop)

    def test_socket_composition_lists_and_prepares_host(self) -> None:
        descriptor = load_rendezvous_file(self.runtime.rendezvous_path)
        credential = self.runtime.enrollment.credential_path.read_text().strip()
        client = UnixSocketEngineClient(descriptor.socket_path, timeout_seconds=5)
        with client.session() as session:
            hello = session.call({
                "jsonrpc": "2.0",
                "id": "hello",
                "method": "engine.v1.hello",
                "params": {
                    "client_name": "host-bootstrap-fixture",
                    "offered_api": {"major": 1, "minor": 0},
                    "authentication": {
                        "engine_instance_id": descriptor.engine_instance_id,
                        "instance_nonce": descriptor.instance_nonce,
                        "credential": credential,
                    },
                },
            })
            self.assertTrue(hello["result"]["authenticated"])
            listed = session.call({
                "jsonrpc": "2.0", "id": "list",
                "method": "engine.v1.hosts.list", "params": {},
            })
            prepared = session.call({
                "jsonrpc": "2.0", "id": "prepare",
                "method": "engine.v1.hosts.prepare",
                "params": {"host_id": "com.openai.codex"},
            })
        self.assertEqual(listed["result"]["hosts"][0]["host_id"], "com.openai.codex")
        self.assertEqual(prepared["result"], {"prepared": True})


if __name__ == "__main__":
    unittest.main()
