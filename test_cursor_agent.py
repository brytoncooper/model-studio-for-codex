"""Cursor callback routing contract without credentials, SDK installation, or inference."""
import queue
import unittest

import context_compaction
from cursor_agent import CursorAgentManager, _prompt_message
from cursor_sdk_runtime import CursorRuntimeError


def request(**overrides):
    body = {"model": "cursor/composer-test", "instructions": "Follow the Codex contract.",
            "tools": [{"type": "namespace", "name": "functions", "tools": [
                {"type": "function", "name": "exec_command", "description": "Execute through Codex.",
                 "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}]}],
            "input": [{"type": "message", "role": "user", "content": "Inspect the project."}]}
    body.update(overrides)
    return body


METADATA = {"thread_id": "thread-one", "agent_name": "/root", "turn_id": "turn-one"}


class FakeProcess:
    def __init__(self, payload, initial=()):
        self.payload = payload
        self.events = queue.Queue()
        self.results = []
        self.closed = False
        for event in initial:
            self.events.put(event)

    def tool_result(self, call_id, output):
        self.results.append((call_id, output))
        self.events.put({"type": "text", "text": "The check passed."})
        self.events.put({"type": "done", "status": "finished", "agent_id": "agent-one", "usage": None})

    def close(self):
        self.closed = True


class CursorAgentTests(unittest.TestCase):
    def setUp(self):
        self.processes = []
        self.initial = [{"type": "tool_call", "call_id": "cursor-call-one", "name": "exec_command",
                         "arguments": {"cmd": "pwd"}}]

        def factory(payload):
            process = FakeProcess(payload, self.initial)
            self.processes.append(process)
            return process
        self.manager = CursorAgentManager(factory, heartbeat_seconds=0.01)

    def tearDown(self):
        self.manager.close()

    def stream(self, body=None, metadata=None, account="account-one"):
        return self.manager.stream(body or request(), metadata or METADATA, "test-secret", account)

    def continuation(self):
        return request(input=[{"type": "function_call_output", "call_id": "cursor-call-one", "output": "ok"}])

    def test_tool_boundary_retains_run_and_restores_namespace_then_resumes(self):
        events = list(self.stream())
        calls = [event["item"] for event in events if event["type"] == "response.output_item.done"
                 and event["item"]["type"] == "function_call"]
        # Codex's default functions namespace is implicit when absent on the wire.
        self.assertEqual(calls[0].get("namespace", "functions"), "functions")
        self.assertEqual(calls[0]["call_id"], "cursor-call-one")
        self.assertFalse(self.processes[0].closed)
        continuation = list(self.stream(self.continuation()))
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.processes[0].results, [("cursor-call-one", "ok")])
        self.assertTrue(self.processes[0].closed)
        completed = continuation[-1]["response"]
        self.assertEqual(completed["cursor_agent_id"], "agent-one")
        self.assertIsNone(completed["cost"])
        self.assertIsNone(completed["usage"])

    def test_wrong_thread_account_or_model_never_releases_callback(self):
        list(self.stream())
        for metadata, account, model in [
            (dict(METADATA, thread_id="other"), "account-one", "cursor/composer-test"),
            (METADATA, "other-account", "cursor/composer-test"),
            (METADATA, "account-one", "cursor/other-model"),
            (dict(METADATA, agent_name="child"), "account-one", "cursor/composer-test")]:
            with self.subTest(metadata=metadata, account=account, model=model):
                body = self.continuation()
                body["model"] = model
                with self.assertRaises(CursorRuntimeError):
                    list(self.stream(body, metadata, account))
        self.assertEqual(self.processes[0].results, [])

    def test_abandoned_stream_cancels_process_before_tool_result(self):
        stream = self.stream()
        next(stream)
        stream.close()
        self.assertTrue(self.processes[0].closed)

    def test_unoffered_tool_fails_closed(self):
        self.initial = [{"type": "tool_call", "call_id": "cursor-call-bad", "name": "shell", "arguments": {}}]
        with self.assertRaises(CursorRuntimeError):
            list(self.stream())
        self.assertTrue(self.processes[0].closed)

    def test_missing_thread_identity_rejected_before_process_creation(self):
        with self.assertRaises(CursorRuntimeError):
            list(self.stream(metadata={"agent_name": "root"}))
        self.assertEqual(self.processes, [])

    def test_expired_callback_is_not_replayed_as_new_inference(self):
        with self.assertRaises(CursorRuntimeError):
            list(self.stream(self.continuation()))
        self.assertEqual(self.processes, [])

    def test_new_user_turn_cancels_old_generation(self):
        list(self.stream())
        first = self.processes[0]
        self.initial = [{"type": "done", "status": "finished"}]
        body = self.continuation()
        body["input"].append({"type": "message", "role": "user", "content": "New task"})
        list(self.stream(body))
        self.assertEqual(len(self.processes), 2)
        self.assertTrue(first.closed)
        self.assertEqual(first.results, [])

    def test_close_releases_paused_callbacks(self):
        list(self.stream())
        self.manager.close()
        self.assertTrue(self.processes[0].closed)

    def test_interrupt_only_cancels_matching_turn(self):
        list(self.stream())
        self.manager.cancel("thread-one", "other-turn")
        self.assertFalse(self.processes[0].closed)
        self.manager.cancel("thread-one", "turn-one")
        self.assertTrue(self.processes[0].closed)
        with self.assertRaises(CursorRuntimeError):
            list(self.stream())

    def test_billed_cost_uses_charged_cents_and_preserves_raw_cost(self):
        self.initial = [{"type": "done", "status": "finished", "cursor_usage_cost": {
            "raw_cost_cents": 9.0, "charged_cents": 3.0}}]
        events = list(self.stream())
        self.assertEqual(events[-1]["response"]["cost"], 0.03)
        self.assertEqual(events[-1]["response"]["cursor_usage_cost"]["raw_cost_cents"], 9.0)

    def test_old_delivered_output_does_not_reattach_without_pending_result(self):
        list(self.stream())
        session = next(iter(self.manager._sessions))
        session.pending.clear()
        session.delivered.add("cursor-call-one")
        with self.assertRaises(CursorRuntimeError):
            list(self.stream(self.continuation()))
        self.assertEqual(self.processes[0].results, [])

    def test_terminal_error_is_not_completed(self):
        self.initial = [{"type": "done", "status": "cancelled"}]
        with self.assertRaises(CursorRuntimeError):
            list(self.stream())

    def test_tool_choice_none_has_no_callback_tools(self):
        self.initial = [{"type": "done", "status": "finished"}]
        list(self.stream(request(tool_choice="none")))
        self.assertEqual(self.processes[0].payload["tools"], [])

    def test_fast_choice_reaches_the_sdk_process(self):
        self.initial = [{"type": "done", "status": "finished"}]
        list(self.stream(request(service_tier="priority")))
        self.assertEqual(self.processes[0].payload["service_tier"], "priority")
        list(self.stream(request(tool_choice="none")))
        self.assertIsNone(self.processes[1].payload["service_tier"])

    def test_completed_run_without_tool_calls_is_a_completed_response(self):
        self.initial = [{"type": "text", "text": "All good."}, {"type": "done", "status": "finished", "agent_id": "agent-two"}]
        events = list(self.stream(request(tool_choice="none")))
        self.assertEqual(events[-1]["type"], "response.completed")
        self.assertEqual(events[-1]["response"]["status"], "completed")
        self.assertEqual(events[-2]["item"]["content"][0]["text"], "All good.")
        self.assertTrue(self.processes[0].closed)

    def test_prompt_replaces_compaction_items_with_their_summary(self):
        ours = context_compaction.compaction_item(context_compaction.encode("Half done; next: ship.", "cursor/composer-test"))
        body = request(input=[{"type": "message", "role": "user", "content": "Start"}, ours,
                              {"type": "compaction", "encrypted_content": "opaque-openai-blob"},
                              {"type": "compaction_trigger"},
                              {"type": "message", "role": "user", "content": "Continue"}])
        message = _prompt_message(body, METADATA)
        self.assertIn("Half done; next: ship.", message["text"])
        self.assertIn(context_compaction.SUMMARY_PREFIX[:40], message["text"])
        self.assertIn("cannot be read here", message["text"])
        self.assertNotIn("opaque-openai-blob", message["text"])
        self.assertNotIn("compaction_trigger", message["text"])
        self.assertNotIn(context_compaction.MARKER, message["text"])
        self.assertIn("not a request to summarize it", message["text"])
        summarizing = _prompt_message(context_compaction.summarization_request(body), METADATA)
        self.assertIn("context checkpoint summary", summarizing["text"])
        self.assertNotIn("not a request to summarize it", summarizing["text"])

    def test_prompt_preserves_roles_and_omits_encrypted_reasoning(self):
        body = request(input=[{"type": "message", "role": "developer", "content": "Scope rules"},
                              {"type": "reasoning", "encrypted_content": "secret-reasoning"},
                              {"type": "message", "role": "user", "content": [
                                  {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}])
        message = _prompt_message(body, dict(METADATA, cwd="/project"))
        self.assertIn('"role": "developer"', message["text"])
        self.assertIn("/project", message["text"])
        self.assertNotIn("secret-reasoning", message["text"])
        self.assertEqual(message["images"], [{"data": "AAAA", "mime_type": "image/png"}])


if __name__ == "__main__":
    unittest.main()
