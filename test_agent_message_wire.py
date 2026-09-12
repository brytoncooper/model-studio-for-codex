"""Wire contracts for native agent delegation; no network or inference."""
import copy
import json
import unittest

from agent_message_wire import (AgentMessageError, GENERIC_NAMESPACE, mark_plaintext_agent_call,
                                prepare_request, reject_encrypted_agent_messages, restore_event)


def namespace(name="collaboration"):
    return {"type": "namespace", "name": name, "description": "Native collaboration tools.", "tools": [
        {"type": "function", "name": "spawn_agent", "parameters": {"type": "object", "properties": {
            "message": {"type": "string", "encrypted": True, "description": "Task"},
            "other": {"type": "string", "encrypted": True}}, "required": ["message"]}},
        {"type": "function", "name": "send_message", "parameters": {"type": "object", "properties": {
            "message": {"type": "string", "encrypted": True}}}},
        {"type": "function", "name": "followup_task", "parameters": {"type": "object", "properties": {
            "message": {"type": "string", "encrypted": True}}}},
        {"type": "function", "name": "wait_agent", "parameters": {"type": "object", "properties": {
            "message": {"type": "string", "encrypted": True}}}}]}


def call(name="spawn_agent", namespace_name=GENERIC_NAMESPACE, **overrides):
    item = {"type": "function_call", "id": "fc_openai", "call_id": "call-openai", "name": name,
            "namespace": namespace_name, "arguments": '{"message":"Read the source."}'}
    item.update(overrides)
    return item


class AgentMessageWireTests(unittest.TestCase):
    def test_rewrites_normal_and_nested_additional_tool_namespaces(self):
        request = {"tools": [namespace()], "input": [{"type": "additional_tools", "role": "system", "tools": [
            {"type": "namespace", "name": "outer", "tools": [namespace()]}]}]}
        raw, changed = prepare_request(json.dumps(request).encode())
        self.assertTrue(changed)
        rewritten = json.loads(raw)
        definitions = [rewritten["tools"][0], rewritten["input"][0]["tools"][0]["tools"][0]]
        for definition in definitions:
            self.assertEqual(definition["name"], GENERIC_NAMESPACE)
            for tool in definition["tools"][:3]:
                self.assertNotIn("encrypted", tool["parameters"]["properties"]["message"])
            self.assertTrue(definition["tools"][0]["parameters"]["properties"]["other"]["encrypted"])
            self.assertTrue(definition["tools"][3]["parameters"]["properties"]["message"]["encrypted"])
        self.assertEqual(rewritten["input"][0]["role"], "system")

    def test_rewrites_only_known_plaintext_history_leaving_ciphertext_and_reasoning(self):
        plaintext = call(namespace_name="collaboration", encrypted_function_args=[])
        opaque = call(namespace_name="collaboration", encrypted_function_args=None, arguments="opaque-history")
        encrypted = call(namespace_name="collaboration", encrypted_function_args=["opaque-marker"])
        reasoning = {"type": "reasoning", "id": "rs_native", "encrypted_content": "opaque-reasoning", "summary": []}
        message = {"type": "agent_message", "content": [{"type": "encrypted_content", "encrypted_content": "opaque-task"}]}
        request = {"input": [plaintext, opaque, encrypted, reasoning, message],
                   "include": ["reasoning.encrypted_content"]}
        raw, changed = prepare_request(json.dumps(request).encode())
        result = json.loads(raw)
        self.assertTrue(changed)
        self.assertEqual(result["input"][0]["namespace"], GENERIC_NAMESPACE)
        self.assertEqual(result["input"][1:], request["input"][1:])
        self.assertEqual(result["include"], request["include"])

    def test_unrelated_schema_objects_are_not_recursively_rewritten(self):
        unrelated = {"type": "function", "name": "schema_editor", "parameters": namespace()}
        raw = json.dumps({"tools": [unrelated, namespace("other")], "input": "Plain prompt"}, indent=2).encode()
        self.assertEqual(prepare_request(raw), (raw, False))

    def test_nonmessage_collaboration_history_keeps_the_upstream_alias(self):
        item = call(name="wait_agent", namespace_name="collaboration")
        raw, changed = prepare_request(json.dumps({"input": [item]}).encode())
        self.assertTrue(changed)
        self.assertEqual(json.loads(raw)["input"][0]["namespace"], GENERIC_NAMESPACE)

    def test_collision_in_additional_tools_is_rejected(self):
        request = {"tools": [namespace()], "input": [{"type": "additional_tools", "tools": [
            {"type": "namespace", "name": "outer", "tools": [namespace(GENERIC_NAMESPACE)]}]}]}
        with self.assertRaises(AgentMessageError):
            prepare_request(json.dumps(request).encode())

    def test_collision_without_native_namespace_is_also_rejected(self):
        with self.assertRaises(AgentMessageError):
            prepare_request(json.dumps({"tools": [namespace(GENERIC_NAMESPACE)]}).encode())

    def test_added_done_and_completed_restore_names_with_plaintext_marker(self):
        for name in ("spawn_agent", "send_message", "followup_task"):
            for kind in ("response.output_item.added", "response.output_item.done", "response.completed"):
                with self.subTest(name=name, kind=kind):
                    original = call(name=name)
                    event = {"type": kind, "item": original} if kind != "response.completed" else {
                        "type": kind, "response": {"id": "resp_native", "output": [original]}}
                    restored = restore_event(event)
                    item = restored.get("item") or restored["response"]["output"][0]
                    self.assertEqual(item["namespace"], "collaboration")
                    self.assertEqual(item["encrypted_function_args"], [])
                    self.assertEqual(item["id"], "fc_openai")
                    self.assertEqual(item["call_id"], "call-openai")
                    self.assertEqual(item["arguments"], '{"message":"Read the source."}')

    def test_other_native_tools_restore_without_plaintext_marker(self):
        event = {"type": "response.output_item.done", "item": call(name="wait_agent")}
        item = restore_event(event)["item"]
        self.assertEqual(item["namespace"], "collaboration")
        self.assertNotIn("encrypted_function_args", item)

    def test_native_encrypted_events_and_reasoning_are_unchanged(self):
        event = {"type": "response.completed", "response": {"id": "resp_native", "output": [
            call(namespace_name="collaboration", encrypted_function_args=["opaque"]),
            {"type": "reasoning", "id": "rs_native", "encrypted_content": "opaque-reasoning", "summary": []}]}}
        before = copy.deepcopy(event)
        self.assertEqual(restore_event(event), before)

    def test_foreign_plaintext_marking_is_limited_to_native_message_calls(self):
        item = call(namespace_name="collaboration")
        self.assertEqual(mark_plaintext_agent_call(item)["encrypted_function_args"], [])
        for other in [call(name="wait_agent", namespace_name="collaboration"), call(),
                      {"type": "reasoning", "namespace": "collaboration", "name": "spawn_agent"}]:
            self.assertNotIn("encrypted_function_args", mark_plaintext_agent_call(other))

    def test_foreign_guard_rejects_encrypted_assignment_without_exposing_it(self):
        request = {"input": [{"type": "agent_message", "author": "/root", "content": [
            {"type": "input_text", "text": "Payload:"},
            {"type": "encrypted_content", "encrypted_content": "never-print-this-ciphertext"}]}]}
        with self.assertRaises(AgentMessageError) as failure:
            reject_encrypted_agent_messages(request)
        self.assertNotIn("never-print-this-ciphertext", str(failure.exception))
        self.assertIn("Start a new delegation", str(failure.exception))

    def test_guard_preserves_encrypted_reasoning_and_plaintext_agent_message(self):
        request = {"input": [{"type": "reasoning", "encrypted_content": "opaque"},
                             {"type": "agent_message", "content": [{"type": "input_text", "text": "Task"}]}]}
        before = copy.deepcopy(request)
        self.assertIsNone(reject_encrypted_agent_messages(request))
        self.assertEqual(request, before)

    def test_malformed_json_and_nonobject_bodies_pass_through(self):
        for raw in (b"not json", b"[]", b"null", b"\xff"):
            self.assertEqual(prepare_request(raw), (raw, False))


if __name__ == "__main__":
    unittest.main()
