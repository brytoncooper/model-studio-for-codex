"""B14 no-network acceptance through the real package-owned broker process."""
from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from model_deck.integrations.providers.cursor.configuration import (
    CURSOR_SDK_VERSION,
    CredentialCommand,
    CursorProfile,
    CursorSdkProcess,
)
from model_deck.integrations.providers.cursor import sdk_runtime


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "fake_cursor_sdk"


class FakeSdkProcessAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.fake_sdk_root = self.root / "fake-sdk"
        shutil.copytree(FIXTURE_ROOT, self.fake_sdk_root)
        self.sdk_python = self.root / "fake-sdk-python"
        self.sdk_python.write_text(
            "#!/bin/sh\n"
            f"PYTHONPATH={shlex.quote(str(self.fake_sdk_root))} "
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        self.sdk_python.chmod(0o700)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def _profile(self, model: str) -> CursorProfile:
        return CursorProfile(
            schema_version=1,
            provider_id="com.modeldeck.provider.cursor",
            provider_name="Cursor",
            connection_id="550e8400-e29b-41d4-a716-446655440000",
            provider_model_id=f"cursor/{model}",
            display_name=model,
            endpoint_config_ref="ref:test.cursor.endpoint",
            credential_ref="ref:test.cursor.credential",
            capability_snapshot_ref="ref:test.cursor.serial-tools",
            credential_command=CredentialCommand("/usr/bin/printf", ("fake-account-key",), 5000),
            sdk_python=self.sdk_python,
            sdk_version=CURSOR_SDK_VERSION,
            workspace_path=self.workspace,
            state_root=self.root / "state",
            billing_description="Synthetic Cursor account.",
        )

    def _start(self, model: str, *, priority: bool = False) -> CursorSdkProcess:
        return CursorSdkProcess(
            self._profile(model),
            {
                "model": model,
                "api_key": "fake-account-key",
                "workspace": str(self.workspace),
                "state_root": str(self.root / "state"),
                "tools": [{
                    "name": "host.exec",
                    "description": "Synthetic host tool",
                    "parameters": {"type": "object"},
                }],
                "message": {"text": "Run the synthetic task."},
                "reasoning": {"effort": "high"},
                "service_tier": "priority" if priority else None,
            },
        )

    def _events_until_terminal(self, process: CursorSdkProcess):
        events = []
        while True:
            event = process.events.get(timeout=5)
            events.append(event)
            if event.get("type") == "tool_call":
                process.tool_result(event["call_id"], {"step": event["arguments"]["step"]})
            if event.get("type") in {"done", "error"}:
                return events

    def _trace(self):
        path = self.fake_sdk_root / "trace.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_account_parameters_tools_and_active_run_reuse(self) -> None:
        process = self._start("composer-test", priority=True)
        self.addCleanup(process.close)

        events = self._events_until_terminal(process)

        calls = [event for event in events if event["type"] == "tool_call"]
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["call_id"], calls[1]["call_id"])
        self.assertTrue(all(event["name"] == "host.exec" for event in calls))
        self.assertEqual(events[-1]["type"], "done")
        trace = self._trace()
        creation = next(item for item in trace if item["event"] == "agents.create")
        self.assertEqual(creation["account"], "fake-account-key")
        self.assertEqual(creation["model"], {
            "id": "composer-test",
            "params": [
                {"id": "effort", "value": "high"},
                {"id": "fast", "value": "true"},
            ],
        })
        self.assertEqual(creation["tools"], ["mcp"])
        self.assertEqual(creation["mcp_servers"], {})
        self.assertEqual(creation["agents"], {})
        self.assertEqual(creation["setting_sources"], [])
        self.assertEqual(creation["custom_tools"], ["host.exec"])
        self.assertEqual(len([item for item in trace if item["event"] == "agents.create"]), 1)
        self.assertEqual(
            [item["step"] for item in trace if item["event"] == "tool.result"],
            [1, 2],
        )

    def test_unsupported_fast_and_truncated_terminal_fail_closed(self) -> None:
        unsupported = self._start("no-fast", priority=True)
        self.addCleanup(unsupported.close)
        unsupported_events = self._events_until_terminal(unsupported)
        self.assertEqual(unsupported_events[-1], {"type": "error", "code": "fast_unavailable"})

        truncated = self._start("truncate")
        self.addCleanup(truncated.close)
        truncated_events = self._events_until_terminal(truncated)
        self.assertEqual(truncated_events[0]["type"], "started")
        self.assertEqual(truncated_events[-1]["type"], "error")
        self.assertFalse(any(event["type"] == "done" for event in truncated_events))

    def test_parallel_processes_are_isolated_and_close_owned_pids(self) -> None:
        first = self._start("composer-test")
        second = self._start("composer-test")
        self.assertNotEqual(first.pid, second.pid)
        results: list[list[dict]] = []

        def collect(process):
            results.append(self._events_until_terminal(process))

        threads = [threading.Thread(target=collect, args=(process,)) for process in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        first_pid, second_pid = first.pid, second.pid
        first.close()
        second.close()
        self.assertEqual(len(results), 2)
        for pid in (first_pid, second_pid):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_cancellation_closes_the_owned_process_during_a_tool_callback(self) -> None:
        process = self._start("composer-test")
        self.assertEqual(process.events.get(timeout=5)["type"], "started")
        pending = process.events.get(timeout=5)
        self.assertEqual(pending["type"], "tool_call")
        owned_pid = process.pid

        process.close()

        with self.assertRaises(ProcessLookupError):
            os.kill(owned_pid, 0)
        self.assertFalse(any(item["event"] == "tool.result" for item in self._trace()))


if __name__ == "__main__":
    unittest.main()
