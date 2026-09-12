"""Router checks with local fake upstreams. No network, no credentials, no inference."""
import copy
import gzip
import http.client
import http.server
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import context_compaction
import model_benchmarks

from local_router import (LocalRouter, RouterError, SseParser, encode_event, flatten_tools, heal_rejected_encrypted_item,
                          inject_catalog, injected_etag, local_item_id, openrouter_error_message, route_for_model,
                          sanitize_openai_input, translate_event, translate_request)


REGISTERED = {"deepseek/test": {"provider": "openrouter-settings", "role": "openrouter_deepseek_test",
                                "config": {"model_providers": {"openrouter-settings": {
                                    "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                                    "wire_api": "responses", "supports_websockets": False,
                                    "auth": {"command": "/nonexistent/helper", "args": ["--token", "u"],
                                             "timeout_ms": 5000, "refresh_interval_ms": 300000}}}}}}


class FakeRegistry:
    def load_models(self):
        return json.loads(json.dumps(REGISTERED))


class FakeKeys:
    def key_for(self, model):
        return "sk-or-test-key"


def codex_request(model, **overrides):
    tools = [
        {"type": "function", "name": "exec_command", "description": "Run", "strict": False,
         "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
        {"type": "namespace", "name": "multi_agent_v1", "description": "Agents", "tools": [
            {"type": "function", "name": "spawn_agent", "description": "Spawn", "strict": False,
             "parameters": {"type": "object", "properties": {"model": {"type": "string"}}}},
            {"type": "function", "name": "wait_agent", "description": "Wait", "strict": False,
             "parameters": {"type": "object", "properties": {}}}]},
        {"type": "namespace", "name": "mcp__github", "description": "GitHub", "tools": [
            {"type": "function", "name": "_fetch", "description": "Fetch", "strict": False, "parameters": {"type": "object"}}]},
        {"type": "web_search", "external_web_access": False}]
    body = {"model": model, "instructions": "You are a coding agent.", "tools": tools, "tool_choice": "auto",
            "parallel_tool_calls": True, "reasoning": {"effort": "low", "summary": "auto"}, "store": False,
            "stream": True, "include": ["reasoning.encrypted_content"], "prompt_cache_key": "cache",
            "client_metadata": {"session_id": "s"}, "text": {"verbosity": "low"},
            "input": [
                {"type": "message", "role": "developer", "id": "msg_1",
                 "internal_chat_message_metadata_passthrough": {"turn_id": "t"},
                 "content": [{"type": "input_text", "text": "Rules"}]},
                {"type": "message", "role": "user", "id": "msg_2", "content": [{"type": "input_text", "text": "Hi"}]},
                {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "secret"},
                {"type": "function_call", "id": "fc_1", "name": "spawn_agent", "namespace": "multi_agent_v1",
                 "arguments": "{}", "call_id": "call_1", "encrypted_function_args": ["x"]},
                {"type": "function_call_output", "call_id": "call_1",
                 "output": [{"type": "input_text", "text": "spawned"}, {"type": "input_text", "text": "ok"}]},
                {"type": "agent_message", "author": "/root", "recipient": "/root/child",
                 "content": [{"type": "input_text", "text": "Do the task"}]},
                {"type": "additional_tools", "role": "developer", "tools": []},
                {"type": "message", "role": "assistant", "id": "msg_3", "phase": "final_answer",
                 "content": [{"type": "output_text", "text": "Done"}]}]}
    body.update(overrides)
    return body


class TranslationTests(unittest.TestCase):
    def test_route_for_model(self):
        self.assertEqual(route_for_model("gpt-5.6-sol", REGISTERED), "openai")
        self.assertEqual(route_for_model(None, REGISTERED), "openai")
        self.assertEqual(route_for_model("deepseek/test", REGISTERED), "endpoint")
        self.assertEqual(route_for_model("local-model", {"local-model": {}}), "endpoint")
        self.assertEqual(route_for_model("local-model", {}), "openai")
        self.assertEqual(route_for_model("qwen/unknown", REGISTERED), "unregistered")

    def test_request_keeps_only_openrouter_compatible_fields(self):
        request, alias_map = translate_request(codex_request("deepseek/test"))
        self.assertEqual(set(request), {"model", "stream", "instructions", "tools", "tool_choice",
                                        "parallel_tool_calls", "reasoning", "input"})
        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertEqual([tool["name"] for tool in request["tools"]], ["exec_command", "spawn_agent", "wait_agent", "_fetch"])
        self.assertEqual(alias_map["_fetch"], ("mcp__github", "_fetch"))
        self.assertTrue(all(tool["type"] == "function" for tool in request["tools"]))
        self.assertEqual(alias_map["spawn_agent"], ("multi_agent_v1", "spawn_agent"))
        self.assertEqual(alias_map["exec_command"], (None, "exec_command"))
        kinds = [item["type"] for item in request["input"]]
        self.assertEqual(kinds, ["message", "message", "function_call", "function_call_output", "message", "message"])
        for item in request["input"]:
            self.assertNotIn("id", item)
            self.assertNotIn("internal_chat_message_metadata_passthrough", item)
            self.assertNotIn("phase", item)
            self.assertNotIn("encrypted_function_args", item)
        self.assertEqual(request["input"][2], {"type": "function_call", "name": "spawn_agent", "arguments": "{}", "call_id": "call_1"})
        self.assertEqual(request["input"][3]["output"], "spawned\nok")
        self.assertEqual(request["input"][4]["role"], "user")
        self.assertIn("Message from agent /root:\nDo the task", request["input"][4]["content"][0]["text"])
        self.assertEqual(request["input"][5]["content"], [{"type": "output_text", "text": "Done"}])

    def test_fast_and_flex_become_openrouter_routing_variants(self):
        request, _ = translate_request(codex_request("deepseek/test", service_tier="priority"))
        self.assertEqual(request["model"], "deepseek/test:nitro")
        request, _ = translate_request(codex_request("local-model", service_tier="priority"), openrouter=False)
        self.assertEqual(request["model"], "local-model")  # variants are an OpenRouter feature only
        request, _ = translate_request(codex_request("deepseek/test", service_tier="flex"))
        self.assertEqual(request["model"], "deepseek/test:floor")
        request, _ = translate_request(codex_request("deepseek/test", service_tier="default"))
        self.assertEqual(request["model"], "deepseek/test")
        request, _ = translate_request(codex_request("deepseek/test:free", service_tier="priority"))
        self.assertEqual(request["model"], "deepseek/test:free")
        self.assertNotIn("service_tier", request)

    def test_injected_catalog_offers_fast_and_flex_with_friendly_names(self):
        catalog = {"models": [{"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list", "priority": 5,
                               "supported_reasoning_levels": [], "shell_type": "unified_exec", "supported_in_api": True,
                               "truncation_policy": {"mode": "tokens", "limit": 2}, "experimental_supported_tools": [],
                               "availability_nux": None, "upgrade": None, "description": None, "support_verbosity": False,
                               "default_verbosity": None, "apply_patch_tool_type": None}]}
        registered = {"deepseek/deepseek-v4.1-flash": REGISTERED["deepseek/test"]}
        entry = json.loads(inject_catalog(json.dumps(catalog).encode("utf-8"), registered))["models"][-1]
        self.assertEqual(entry["display_name"], "DeepSeek V4.1 Flash")  # no custom map given: automatic name
        entry = json.loads(inject_catalog(json.dumps(catalog).encode("utf-8"), registered,
                                          {"deepseek/deepseek-v4.1-flash": "DeepSeek Flash"}))["models"][-1]
        self.assertEqual(entry["display_name"], "DeepSeek Flash")
        self.assertEqual([tier["id"] for tier in entry["service_tiers"]], ["priority", "flex"])
        self.assertEqual(entry["service_tiers"][0]["name"], "Fast")
        self.assertEqual(entry["additional_speed_tiers"], ["fast"])
        self.assertIsNone(entry["default_service_tier"])

    def test_effort_mapping_and_optional_fields(self):
        request, _ = translate_request(codex_request("deepseek/test", reasoning={"effort": "xhigh"}))
        self.assertEqual(request["reasoning"], {"effort": "high"})
        request, _ = translate_request(codex_request("deepseek/test", reasoning={"effort": "none"}))
        self.assertEqual(request["reasoning"], {"effort": "minimal"})
        request, _ = translate_request(codex_request("deepseek/test", reasoning=None, tools=[],
                                                     text={"format": {"type": "json_object"}, "verbosity": "high"}))
        self.assertNotIn("reasoning", request)
        self.assertNotIn("tools", request)
        self.assertEqual(request["text"], {"format": {"type": "json_object"}})

    def test_name_collisions_between_plain_and_namespaced_tools_are_mangled_and_mapped_back(self):
        tools = [{"type": "function", "name": "search", "parameters": {}},
                 {"type": "namespace", "name": "multi_agent_v1", "tools": [
                     {"type": "function", "name": "search", "parameters": {}}]}]
        flat, alias_map = flatten_tools(tools)
        names = [tool["name"] for tool in flat]
        self.assertEqual(names[0], "search")
        self.assertTrue(names[1].startswith("search__") and len(names[1]) <= 64)
        self.assertEqual(alias_map[names[1]], ("multi_agent_v1", "search"))
        event = translate_event({"type": "response.output_item.done", "item": {
            "type": "function_call", "name": names[1], "arguments": "{}", "call_id": "c"}}, alias_map)
        self.assertEqual(event["item"]["name"], "search")
        self.assertEqual(event["item"]["namespace"], "multi_agent_v1")
        request, _ = translate_request({"model": "m", "tools": tools, "input": [
            {"type": "function_call", "name": "search", "namespace": "multi_agent_v1", "arguments": "{}", "call_id": "c"},
            {"type": "function_call", "name": "search", "arguments": "{}", "call_id": "d"}]})
        self.assertEqual(request["input"][0]["name"], names[1])
        self.assertEqual(request["input"][1]["name"], "search")

    def test_openrouter_items_are_made_foreign_safe_before_codex_stores_them(self):
        added = {"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_foreign",
                                                                  "summary": [{"type": "summary_text", "text": "s"}],
                                                                  "content": [{"type": "reasoning_text", "text": "thinking"}],
                                                                  "encrypted_content": "blob"}}
        item = translate_event(added, {})["item"]
        self.assertEqual(item, {"type": "reasoning", "id": local_item_id("rs_foreign"),
                                "summary": [{"type": "summary_text", "text": "s"}, {"type": "summary_text", "text": "thinking"}]})
        self.assertNotIn("_", item["id"])
        completed = {"type": "response.completed", "response": {"id": "gen", "output": [
            {"type": "reasoning", "id": "rs_x", "encrypted_content": "blob", "content": []}, {"type": "message", "id": "msg_1", "content": []}]}}
        output = translate_event(completed, {})["response"]["output"]
        self.assertEqual(output[0], {"type": "reasoning", "id": local_item_id("rs_x"), "summary": []})
        self.assertEqual(output[1]["id"], local_item_id("msg_1"))
        call = {"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc_9", "name": "f", "call_id": "call_9",
                                                                "arguments": "{}", "encrypted_function_args": ["x"]}}
        translated = translate_event(call, {})["item"]
        self.assertNotIn("encrypted_function_args", translated)
        self.assertEqual(translated["call_id"], "call_9")
        delta = translate_event({"type": "response.function_call_arguments.delta", "item_id": "fc_9", "delta": "{"}, {})
        self.assertEqual(delta["item_id"], translated["id"])

    def test_mcp_collisions_restore_names_in_stream_and_completed_response(self):
        tools = [{"type": "namespace", "name": namespace, "tools": [
            {"type": "function", "name": "search", "parameters": {"type": "object"}}]}
            for namespace in ("mcp__first", "mcp__second", "mcp__third")]
        flat, aliases = flatten_tools(tools)
        self.assertEqual(len(set(tool["name"] for tool in flat)), 3)
        for tool in flat:
            expected_namespace, expected_name = aliases[tool["name"]]
            event = translate_event({"type": "response.completed", "response": {"output": [{
                "type": "function_call", "id": "foreign", "name": tool["name"],
                "arguments": "{}", "call_id": "call"}]}}, aliases)
            item = event["response"]["output"][0]
            self.assertEqual((item["namespace"], item["name"]), (expected_namespace, expected_name))

    def test_cursor_catalog_is_native_with_cursor_billing_and_a_fast_toggle(self):
        registration = {"config": {"model_providers": {"openrouter-settings": {
            "name": "Cursor", "base_url": "https://api.cursor.com", "wire_api": "responses",
            "supports_websockets": False, "auth": {"args": ["--token", "saved-account"]}}}}}
        catalog = {"models": [{"slug": "gpt-5.5", "visibility": "list", "priority": 1}]}
        document = json.loads(inject_catalog(json.dumps(catalog).encode(), {"cursor/composer-2.5": registration}))
        cursor = document["models"][-1]
        self.assertEqual(cursor["slug"], "cursor/composer-2.5")
        self.assertEqual(cursor["visibility"], "list")
        self.assertEqual(cursor["multi_agent_version"], "v2")
        self.assertEqual(cursor["tool_mode"], "direct")
        self.assertIn("Cursor", cursor["description"])
        self.assertNotIn("OpenRouter credits", cursor["description"])
        # No cached Cursor catalog yet: Fast is offered and the SDK broker validates it per model.
        self.assertEqual([(tier["id"], tier["name"]) for tier in cursor["service_tiers"]], [("priority", "Fast")])
        self.assertEqual(cursor["additional_speed_tiers"], ["fast"])
        self.assertIsNone(cursor["default_service_tier"])
        # A cached catalog decides per model.
        offered = json.loads(inject_catalog(json.dumps(catalog).encode(), {"cursor/composer-2.5": registration},
                                            cursor_fast_models={"cursor/composer-2.5"}))["models"][-1]
        self.assertEqual(offered["additional_speed_tiers"], ["fast"])
        withheld = json.loads(inject_catalog(json.dumps(catalog).encode(), {"cursor/composer-2.5": registration},
                                             cursor_fast_models={"cursor/grok-4.6"}))["models"][-1]
        self.assertEqual((withheld["service_tiers"], withheld["additional_speed_tiers"]), ([], []))

    def test_compaction_items_become_summary_messages_for_endpoint_models(self):
        ours = context_compaction.compaction_item(context_compaction.encode("Half done; next: tests.", "deepseek/test"))
        body = codex_request("deepseek/test", input=[
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
            ours,
            {"type": "compaction", "id": "cmp_openai", "encrypted_content": "opaque-openai-blob"},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Continue"}]}])
        request, _ = translate_request(body, preserve_continuation=True)
        roles = [(item["type"], item.get("role")) for item in request["input"]]
        self.assertEqual(roles, [("message", "user")] * 4)
        summary = request["input"][1]["content"][0]["text"]
        self.assertTrue(summary.startswith(context_compaction.SUMMARY_PREFIX))
        self.assertIn("Half done; next: tests.", summary)
        self.assertNotIn("id", request["input"][1])
        self.assertIn("cannot be read here", request["input"][2]["content"][0]["text"])
        self.assertNotIn("opaque-openai-blob", json.dumps(request))

    def test_openai_route_rewrites_router_compaction_items_into_plain_summaries(self):
        ours = context_compaction.compaction_item(context_compaction.encode("Half done.", "deepseek/test"))
        body = json.dumps({"model": "gpt-6-astra", "input": [
            {"type": "message", "role": "user", "content": []}, ours,
            {"type": "compaction", "id": "cmp_openai", "encrypted_content": "opaque-openai-blob"}]}).encode("utf-8")
        cleaned, changed = sanitize_openai_input(body)
        self.assertEqual(changed, 1)
        items = json.loads(cleaned)["input"]
        self.assertEqual([item["type"] for item in items], ["message", "message", "compaction"])
        self.assertIn("Half done.", items[1]["content"][0]["text"])
        self.assertEqual(items[2]["encrypted_content"], "opaque-openai-blob")  # OpenAI's own item is untouched
        self.assertNotIn(context_compaction.MARKER, cleaned.decode("utf-8"))

    def test_openai_route_drops_reasoning_items_openai_cannot_use(self):
        body = json.dumps({"model": "gpt-6-astra", "input": [
            {"type": "message", "role": "user", "content": []},
            {"type": "reasoning", "id": "rs_openai", "summary": [], "encrypted_content": "ok"},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "kimi"}]},
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "kimi"}], "encrypted_content": None}]}).encode("utf-8")
        cleaned, dropped = sanitize_openai_input(body)
        self.assertEqual(dropped, 2)
        self.assertEqual([item.get("id") for item in json.loads(cleaned)["input"]], [None, "rs_openai"])
        untouched = b'{"model": "gpt-6-astra", "input": [{"type": "message", "role": "user", "content": []}]}'
        self.assertEqual(sanitize_openai_input(untouched), (untouched, 0))
        self.assertEqual(sanitize_openai_input(b"not json \"reasoning\""), (b"not json \"reasoning\"", 0))

    def test_healing_removes_only_the_rejected_reasoning_item(self):
        body = json.dumps({"model": "gpt-5.6-sol", "input": [
            {"type": "message", "role": "user", "content": []},
            {"type": "reasoning", "id": "rs_openai", "encrypted_content": "ok"},
            {"type": "reasoning", "id": "rs_foreign-1", "encrypted_content": "bad"}]}).encode("utf-8")
        error = b'{"error": {"message": "The encrypted content for item rs_foreign-1 could not be verified. Reason: Encrypted content could not be decrypted or parsed."}}'
        healed = json.loads(heal_rejected_encrypted_item(body, error))
        self.assertEqual([item.get("id") for item in healed["input"]], [None, "rs_openai"])
        no_id = heal_rejected_encrypted_item(body, b'{"error": {"message": "Encrypted content could not be parsed."}}')
        self.assertEqual([item["type"] for item in json.loads(no_id)["input"]], ["message"])
        by_index = heal_rejected_encrypted_item(body, b'{"error": {"message": "Invalid \'input[2].content\': array too long. Expected an array with maximum length 0, but got an array with length 1 instead."}}')
        self.assertEqual([item.get("id") for item in json.loads(by_index)["input"]], [None, "rs_openai"])
        self.assertIsNone(heal_rejected_encrypted_item(body, b'{"error": {"message": "Invalid \'input[0].content\': bad message"}}'))
        self.assertIsNone(heal_rejected_encrypted_item(body, b'{"error": {"message": "context window exceeded"}}'))
        self.assertIsNone(heal_rejected_encrypted_item(b'{"model": "m", "input": []}', error))
        self.assertIsNone(heal_rejected_encrypted_item(b"not json", error))

    def test_translate_event_leaves_plain_calls_and_other_events_alone(self):
        plain = {"type": "response.output_item.done", "item": {"type": "function_call", "name": "exec_command", "call_id": "c", "arguments": "{}"}}
        self.assertEqual(translate_event(json.loads(json.dumps(plain)), {"exec_command": (None, "exec_command")}), plain)
        delta = {"type": "response.output_text.delta", "delta": "hi"}
        self.assertEqual(translate_event(dict(delta), {}), delta)
        self.assertIsNone(translate_event({"no": "type"}, {}))

    def test_sse_parser_handles_comments_done_and_crlf(self):
        parser = SseParser()
        received = list(parser.feed(b": OPENROUTER PROCESSING\r\n\r\nevent: response.created\r\ndata: {\"type\":\"response.created\"}\r\n\r\ndata: [DONE]\n\ndata: {\"type\":"))
        self.assertEqual(received[0], ("comment", b": OPENROUTER PROCESSING"))
        self.assertEqual(received[1], ("event", {"type": "response.created"}))
        self.assertEqual(len(received), 2)
        received = list(parser.feed(b"\"response.completed\"}\n\n"))
        self.assertEqual(received, [("event", {"type": "response.completed"})])
        self.assertEqual(list(parser.flush()), [])

    def test_catalog_injection_adds_registered_models_from_a_template(self):
        catalog = {"models": [
            {"slug": "gpt-6-astra", "display_name": "GPT-6", "visibility": "list", "priority": 1, "guardian": {"shell": "x"},
             "supported_reasoning_levels": [], "shell_type": "unified_exec", "supported_in_api": True,
             "truncation_policy": {"mode": "tokens", "limit": 1}, "experimental_supported_tools": [],
             "availability_nux": None, "upgrade": None, "description": None, "support_verbosity": True,
             "default_verbosity": "low", "apply_patch_tool_type": "freeform", "use_responses_lite": True,
             "minimal_client_version": "0.153.0", "available_in_plans": ["pro"]},
            {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list", "priority": 5,
             "supported_reasoning_levels": [], "shell_type": "unified_exec", "supported_in_api": True,
             "truncation_policy": {"mode": "tokens", "limit": 2}, "experimental_supported_tools": ["clock"],
             "availability_nux": None, "upgrade": None, "description": None, "support_verbosity": True,
             "default_verbosity": "low", "apply_patch_tool_type": "freeform", "use_responses_lite": False,
             "minimal_client_version": "0.150.0", "available_in_plans": ["pro", "plus"]}]}
        raw = json.dumps(catalog).encode("utf-8")
        injected = json.loads(inject_catalog(raw, REGISTERED))
        slugs = [entry["slug"] for entry in injected["models"]]
        self.assertEqual(slugs, ["gpt-6-astra", "gpt-5.5", "deepseek/test"])
        entry = injected["models"][-1]
        self.assertEqual(entry["display_name"], "Test")  # automatic name for the fixture id deepseek/test
        self.assertEqual(entry["truncation_policy"], {"mode": "tokens", "limit": 2})
        self.assertEqual(entry["minimal_client_version"], "0.150.0")
        self.assertEqual(entry["available_in_plans"], ["pro", "plus"])
        self.assertEqual(entry["visibility"], "list")
        self.assertFalse(entry["use_responses_lite"])
        self.assertEqual(entry["tool_mode"], "direct")
        self.assertEqual(entry["multi_agent_version"], "v2")
        self.assertIsNone(entry["apply_patch_tool_type"])
        self.assertFalse(entry["supports_search_tool"])
        self.assertNotIn("guardian", entry)
        self.assertEqual([level["effort"] for level in entry["supported_reasoning_levels"]], ["low", "medium", "high"])
        self.assertEqual(json.loads(inject_catalog(injected and json.dumps(injected).encode("utf-8"), REGISTERED)), injected)
        self.assertEqual(inject_catalog(raw, {}), raw)
        self.assertEqual(injected_etag('W/"abc"'), 'W/"abc-deck"')
        self.assertEqual(injected_etag("abc", "deck1234"), "abc-deck1234")
        self.assertIsNone(injected_etag(None))

    def test_openrouter_error_messages(self):
        message, code = openrouter_error_message(401, b'{"error": {"message": "bad key"}}')
        self.assertIn("API key", message)
        self.assertIn("bad key", message)
        self.assertEqual(code, "invalid_prompt")
        self.assertEqual(openrouter_error_message(402, b"")[1], "invalid_prompt")
        self.assertEqual(openrouter_error_message(429, b"not json")[1], "rate_limit_exceeded")
        self.assertEqual(openrouter_error_message(503, b"")[1], "server_error")


class FakeUpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.record(b"")
        if self.path.startswith("/backend-api/codex/models"):
            payload = json.dumps({"models": [{"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list", "priority": 5,
                                              "supported_reasoning_levels": [], "shell_type": "unified_exec", "supported_in_api": True,
                                              "truncation_policy": {"mode": "tokens", "limit": 2}, "experimental_supported_tools": [],
                                              "availability_nux": None, "upgrade": None, "description": None, "support_verbosity": False,
                                              "default_verbosity": None, "apply_patch_tool_type": None}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("ETag", 'W/"real"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        call = self.record(body)
        if self.path == "/api/v1/responses":
            if self.headers.get("Authorization") != "Bearer sk-or-test-key":
                self.send_response(401)
                payload = b'{"error": {"message": "bad key"}}'
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            request = json.loads(body)
            if not request.get("tools"):
                summary = {"type": "message", "id": "msg_summary", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": "Summary: the task is half done."}]}
                events = ["event: response.created\ndata: " + json.dumps({"type": "response.created", "response": {"id": "gen-sum"}}) + "\n\n",
                          "event: response.output_item.done\ndata: " + json.dumps({"type": "response.output_item.done", "item": summary}) + "\n\n",
                          "event: response.completed\ndata: " + json.dumps({"type": "response.completed", "response": {
                              "id": "gen-sum", "output": [summary], "usage": {"input_tokens": 40, "output_tokens": 8, "total_tokens": 48}}}) + "\n\n"]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for event in events:
                    data = event.encode("utf-8")
                    self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                return
            alias = next((tool["name"] for tool in request.get("tools", []) if tool["name"].startswith("spawn_agent")), "spawn_agent")
            events = [": OPENROUTER PROCESSING\r\n\r\n",
                      "event: response.created\ndata: " + json.dumps({"type": "response.created", "response": {"id": "gen-1"}}) + "\n\n",
                      "event: response.output_item.done\ndata: " + json.dumps({"type": "response.output_item.done", "item": {
                          "type": "function_call", "name": alias, "arguments": "{\"model\":\"x\"}", "call_id": "call_9", "id": "fc_9"}}) + "\n\n",
                      "event: response.completed\ndata: " + json.dumps({"type": "response.completed", "response": {
                          "id": "gen-1", "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}}) + "\n\n",
                      "data: [DONE]\n\n"]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for event in events:
                data = event.encode("utf-8")
                self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            return
        # chatgpt.com stand-in: echo the model and whether ChatGPT auth arrived, as SSE.
        request = json.loads(body)
        if any(tool.get("name") == "model_deck_agents" for tool in request.get("tools", [])):
            item = {"type": "function_call", "namespace": "model_deck_agents", "name": "spawn_agent",
                    "id": "fc_openai_original", "call_id": "call_plaintext", "arguments": '{"message":"Run the child task"}'}
            reasoning = {"type": "reasoning", "id": "rs_openai_original", "encrypted_content": "opaque-openai-state"}
            events = [{"type": "response.output_item.added", "item": dict(item)},
                      {"type": "response.output_item.done", "item": dict(item)},
                      {"type": "response.completed", "response": {"id": "resp_original", "output": [reasoning, item]}}]
            payload = b"".join(encode_event(event) for event in events)
            self.send_response(200)
            # The live ChatGPT backend omits Content-Type on this SSE response.
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        foreign = [item for item in request.get("input", []) if item.get("type") == "reasoning"
                   and str(item.get("id", "")).startswith("rs_foreign") and item.get("encrypted_content")]
        if foreign:
            payload = json.dumps({"error": {"message": f"The encrypted content for item {foreign[0]['id']} could not be verified. "
                                                       "Reason: Encrypted content could not be decrypted or parsed."}}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        text = f"ECHO model={request.get('model')} auth={'yes' if self.headers.get('Authorization') else 'no'}"
        payload = ("event: response.completed\ndata: " + json.dumps({"type": "response.completed", "response": {
            "id": "resp-echo", "output_text": text, "usage": {"total_tokens": 1}}}) + "\n\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("x-codex-primary-used-percent", "42")
        self.send_header("X-Models-Etag", 'W/"real"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def record(self, body):
        call = {"path": self.path, "headers": {name.lower(): value for name, value in self.headers.items()}, "body": body}
        FakeUpstreamHandler.calls.append(call)
        return call


class FakeChatOnlyHandler(http.server.BaseHTTPRequestHandler):
    """A local server like LM Studio: no Responses API, streaming chat completions, no key expected."""
    protocol_version = "HTTP/1.1"
    calls = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        FakeChatOnlyHandler.calls.append({"path": self.path, "headers": {name.lower(): value for name, value in self.headers.items()}, "body": body})
        if self.path != "/v1/chat/completions":
            payload = b'{"error": "Unexpected endpoint or method. (POST /v1/responses)"}'
            self.send_response(404)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "thinking"}}]},
            {"choices": [{"index": 0, "delta": {"content": "Hello "}}]},
            {"choices": [{"index": 0, "delta": {"content": "there"}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_abc", "type": "function",
                                                                  "function": {"name": "spawn_agent", "arguments": "{\"mo"}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "del\":\"x\"}"}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                                      "prompt_tokens_details": {"cached_tokens": 4}}},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in chunks + ["[DONE]"]:
            data = ("data: " + (chunk if isinstance(chunk, str) else json.dumps(chunk)) + "\n\n").encode("utf-8")
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")


class RouterIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstreamHandler)
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        cls.local = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeChatOnlyHandler)
        threading.Thread(target=cls.local.serve_forever, daemon=True).start()
        cls.temp = tempfile.TemporaryDirectory()
        upstream = ("127.0.0.1", cls.upstream.server_address[1], False)
        cls.router = LocalRouter(FakeRegistry(), FakeKeys(), chatgpt_upstream=upstream, openrouter_upstream=upstream,
                                 ledger_path=Path(cls.temp.name) / "ledger.jsonl", log_path=Path(cls.temp.name) / "router.log",
                                 benchmark_path=Path(cls.temp.name) / "benchmarks.json").start()

    @classmethod
    def tearDownClass(cls):
        cls.router.stop()
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.local.shutdown()
        cls.local.server_close()
        cls.temp.cleanup()

    def use_registry(self, models):
        self.router.registry = type("Registry", (), {"load_models": staticmethod(lambda: models)})()
        self.router._registered_cache = ({}, 0.0)
        self.addCleanup(self.reset_registry)

    def reset_registry(self):
        self.router.registry = FakeRegistry()
        self.router._registered_cache = ({}, 0.0)

    def test_spawn_choices_reach_openai_and_openrouter_with_prices_and_benchmarks(self):
        prices = {"deepseek/test": {"input": 0.15, "output": 0.6, "context": 128000,
                                    "tools": True, "modalities": ["text", "image"]}}
        evidence = {"deepseek/test": "Artificial Analysis coding=75 (higher better); retrieved UTC 2026-09-11; cache fresh"}
        for model in ("gpt-6-astra", "deepseek/test"):
            with self.subTest(model=model), mock.patch.object(self.router, "price_table", return_value=prices), \
                    mock.patch.object(self.router, "benchmark_lines", return_value=evidence):
                body = codex_request(model)
                response, _ = self.call("POST", "/backend-api/codex/responses", body)
                self.assertEqual(response.status, 200)
                sent = json.loads(FakeUpstreamHandler.calls[-1]["body"])
                spawn = sent["tools"][1]["tools"][0] if model.startswith("gpt-") else sent["tools"][1]
                for expected in ("deepseek/test", "role=openrouter_deepseek_test", "in $0.15/M", "out $0.6/M",
                                 "128k context", "tools=yes", "vision=yes", "Artificial Analysis coding=75"):
                    self.assertIn(expected, spawn["description"])
                self.assertEqual(spawn["parameters"], body["tools"][1]["tools"][0]["parameters"])

    def test_gzip_native_additional_tools_keeps_schema_and_plaintext_handoff(self):
        body = codex_request("gpt-6-astra", tools=[])
        native = {"type": "namespace", "name": "collaboration", "tools": [
            {"type": "function", "name": "spawn_agent", "description": "Keep ownership and approvals.",
             "parameters": {"type": "object", "properties": {
                 "model": {"type": "string"}, "message": {"type": "string", "encrypted": True}}}}]}
        vendor = {"type": "namespace", "name": "mcp__vendor", "tools": [
            {"type": "function", "name": "spawn_agent", "description": "Vendor spawn", "parameters": {}}]}
        body["input"] = [{"type": "additional_tools", "tools": [native, copy.deepcopy(vendor)]}]
        encoded = gzip.compress(json.dumps(body).encode())
        connection = http.client.HTTPConnection("127.0.0.1", self.router.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("POST", "/backend-api/codex/responses", body=encoded,
                           headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 200)
        call = FakeUpstreamHandler.calls[-1]
        self.assertNotIn("content-encoding", call["headers"])
        self.assertEqual(int(call["headers"]["content-length"]), len(call["body"]))
        sent = json.loads(call["body"])
        namespace, external = sent["input"][0]["tools"]
        self.assertEqual(namespace["name"], "model_deck_agents")
        spawn = namespace["tools"][0]
        self.assertTrue(spawn["description"].startswith("Keep ownership and approvals."))
        self.assertIn("[Model Deck choices]", spawn["description"])
        self.assertIn("No exact published benchmark match", spawn["description"])
        self.assertEqual(spawn["parameters"]["properties"], {"model": {"type": "string"}, "message": {"type": "string"}})
        self.assertEqual(external, vendor)

    def test_spawn_benchmark_cache_observes_refresh_and_staleness_without_fetching(self):
        path = Path(self.temp.name) / "refresh-evidence.json"
        now = time.time()
        def save(score):
            normalized = model_benchmarks.normalize({"data": [{"id": "deepseek/test", "benchmarks": {
                "artificial_analysis": {"coding_index": score}}}]}, "catalog")
            path.write_text(json.dumps({"version": 1, "feeds": {"catalog": {
                **normalized, "fetched_at": now, "error": None}}}))
        router = LocalRouter(FakeRegistry(), FakeKeys(), benchmark_path=path)
        with mock.patch.object(model_benchmarks.BenchmarkStore, "ensure", side_effect=AssertionError("network")), \
                mock.patch.object(model_benchmarks.BenchmarkStore, "refresh", side_effect=AssertionError("network")):
            self.assertIn("No exact published benchmark match", router.model_choices()["deepseek/test"])
            save(0)
            self.assertIn("coding=0", router.model_choices()["deepseek/test"])
            save(75)
            self.assertIn("coding=75", router.model_choices()["deepseek/test"])
            with mock.patch("time.time", return_value=now + model_benchmarks.TTL + 61):
                self.assertIn("cache stale", router.model_choices()["deepseek/test"])

    def test_keyless_local_endpoint_falls_back_to_chat_completions(self):
        FakeChatOnlyHandler.calls.clear()
        base_url = f"http://127.0.0.1:{self.local.server_address[1]}/v1"
        self.use_registry({"local-coder": {"provider": "openrouter-settings", "role": "openrouter_local_coder", "config": {
            "model_providers": {"openrouter-settings": {"name": "LM Studio", "base_url": base_url,
                                                        "wire_api": "responses", "supports_websockets": False}}}}})
        self.router.wire_overrides.clear()
        response, data = self.call("POST", "/backend-api/codex/responses", codex_request("local-coder", service_tier="priority"),
                                   {"Authorization": "Bearer chatgpt-token", "chatgpt-account-id": "acct"})
        self.assertEqual(response.status, 200)
        paths = [call["path"] for call in FakeChatOnlyHandler.calls]
        self.assertEqual(paths, ["/v1/responses", "/v1/chat/completions"])
        chat_call = FakeChatOnlyHandler.calls[-1]
        self.assertNotIn("authorization", chat_call["headers"])
        self.assertNotIn("chatgpt-account-id", chat_call["headers"])
        sent = json.loads(chat_call["body"])
        self.assertEqual(sent["model"], "local-coder")
        self.assertEqual(sent["messages"][0], {"role": "system", "content": "You are a coding agent."})
        self.assertEqual([message["role"] for message in sent["messages"]],
                         ["system", "system", "user", "assistant", "tool", "user", "assistant"])
        self.assertEqual(sent["messages"][3]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(sent["messages"][4], {"role": "tool", "tool_call_id": "call_1", "content": "spawned\nok"})
        self.assertEqual([tool["function"]["name"] for tool in sent["tools"]], ["exec_command", "spawn_agent", "wait_agent", "_fetch"])
        self.assertTrue(sent["stream"])
        events = self.events(data)
        self.assertEqual([event["type"] for event in events], [
            "response.created", "response.output_item.added", "response.reasoning_summary_text.delta",
            "response.output_item.added", "response.output_text.delta", "response.output_text.delta",
            "response.output_item.added", "response.function_call_arguments.delta",
            "response.function_call_arguments.delta", "response.reasoning_summary_text.done",
            "response.output_item.done", "response.output_text.done", "response.output_item.done",
            "response.function_call_arguments.done", "response.output_item.done", "response.completed"])
        call_item = events[14]["item"]
        self.assertEqual((call_item["name"], call_item["namespace"], call_item["call_id"], call_item["arguments"]),
                         ("spawn_agent", "multi_agent_v1", "call_abc", '{"model":"x"}'))
        self.assertEqual(events[12]["item"]["content"][0]["text"], "Hello there")
        self.assertEqual(events[10]["item"]["summary"][0]["text"], "thinking")
        self.assertEqual(events[-1]["response"]["usage"]["total_tokens"], 15)
        self.assertEqual(events[-1]["response"]["usage"]["input_tokens_details"]["cached_tokens"], 4)
        ledger = [json.loads(line) for line in (Path(self.temp.name) / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual((ledger[-1]["route"], ledger[-1]["wire"], ledger[-1]["endpoint"]), ("endpoint", "chat", "LM Studio"))
        self.assertEqual(self.router.wire_overrides[base_url], "chat")
        # The second request goes straight to chat completions.
        FakeChatOnlyHandler.calls.clear()
        self.call("POST", "/backend-api/codex/responses", codex_request("local-coder"))
        self.assertEqual([call["path"] for call in FakeChatOnlyHandler.calls], ["/v1/chat/completions"])

    def setUp(self):
        FakeUpstreamHandler.calls.clear()

    def call(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.router.port, timeout=10)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    @staticmethod
    def events(data):
        return [json.loads(line[5:]) for line in data.decode("utf-8").split("\n") if line.startswith("data:")]

    def test_openai_model_passes_through_with_chatgpt_credentials(self):
        body = codex_request("gpt-5.6-sol")
        response, data = self.call("POST", "/backend-api/codex/responses", body,
                                   {"Authorization": "Bearer chatgpt-token", "chatgpt-account-id": "acct", "x-codex-turn-metadata": json.dumps({"thread_id": "t1", "agent_name": "/root"})})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("x-codex-primary-used-percent"), "42")
        self.assertIn("ECHO model=gpt-5.6-sol auth=yes", data.decode("utf-8"))
        call = FakeUpstreamHandler.calls[-1]
        self.assertEqual(call["path"], "/backend-api/codex/responses")
        self.assertEqual(call["headers"]["authorization"], "Bearer chatgpt-token")
        self.assertEqual(call["headers"]["chatgpt-account-id"], "acct")
        forwarded = json.loads(call["body"])
        spawn = forwarded["tools"][1]["tools"][0]
        original_description = body["tools"][1]["tools"][0]["description"]
        self.assertTrue(spawn["description"].startswith(original_description))
        self.assertIn("[Model Deck choices]", spawn["description"])
        spawn["description"] = original_description
        self.assertEqual(forwarded, body)
        ledger = [json.loads(line) for line in (Path(self.temp.name) / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual(ledger[-1]["route"], "openai")
        self.assertEqual(ledger[-1]["thread_id"], "t1")
        self.assertEqual(ledger[-1]["primary_used_percent"], "42")

    def test_openai_collaboration_stream_without_content_type_restores_plaintext_marker(self):
        body = codex_request("gpt-6-astra")
        body["tools"] = [{"type": "namespace", "name": "collaboration", "tools": [
            {"type": "function", "name": "spawn_agent", "parameters": {"type": "object", "properties": {
                "message": {"type": "string", "encrypted": True}}}}]}]
        response, data = self.call("POST", "/backend-api/codex/responses", body,
                                   {"Authorization": "Bearer chatgpt-token"})
        self.assertEqual(response.status, 200)
        sent = json.loads(FakeUpstreamHandler.calls[-1]["body"])
        self.assertEqual(sent["tools"][0]["name"], "model_deck_agents")
        self.assertNotIn("encrypted", sent["tools"][0]["tools"][0]["parameters"]["properties"]["message"])
        events = self.events(data)
        for item in [events[0]["item"], events[1]["item"], events[2]["response"]["output"][1]]:
            self.assertEqual(item["namespace"], "collaboration")
            self.assertEqual(item["encrypted_function_args"], [])
            self.assertEqual(item["id"], "fc_openai_original")
        self.assertEqual(events[2]["response"]["output"][0], {
            "type": "reasoning", "id": "rs_openai_original", "encrypted_content": "opaque-openai-state"})

    def test_encrypted_agent_task_rejected_before_external_request(self):
        body = codex_request("deepseek/test")
        body["input"] = [{"type": "agent_message", "content": [
            {"type": "encrypted_content", "encrypted_content": "opaque-assignment"}]}]
        response, data = self.call("POST", "/backend-api/codex/responses", body)
        self.assertEqual(FakeUpstreamHandler.calls, [])
        self.assertEqual(self.events(data)[0]["type"], "response.failed")
        self.assertNotIn("opaque-assignment", data.decode())

    def test_poisoned_openai_task_heals_by_dropping_the_rejected_item(self):
        body = codex_request("gpt-5.6-sol")
        body["input"].insert(3, {"type": "reasoning", "id": "rs_foreign-9", "summary": [], "encrypted_content": "from-openrouter"})
        response, data = self.call("POST", "/backend-api/codex/responses", body, {"Authorization": "Bearer chatgpt-token"})
        self.assertEqual(response.status, 200)
        self.assertIn("ECHO model=gpt-5.6-sol auth=yes", data.decode("utf-8"))
        calls = [call for call in FakeUpstreamHandler.calls if call["path"] == "/backend-api/codex/responses"]
        self.assertEqual(len(calls), 2)
        first, second = (json.loads(call["body"]) for call in calls)
        self.assertEqual(len(second["input"]), len(first["input"]) - 1)
        self.assertFalse(any(item.get("id") == "rs_foreign-9" for item in second["input"]))
        self.assertTrue(any(item.get("id") == "rs_1" for item in second["input"]))
        self.assertEqual(calls[1]["headers"]["authorization"], "Bearer chatgpt-token")

    def test_foreign_reasoning_without_encrypted_content_is_dropped_before_openai_sees_it(self):
        body = codex_request("gpt-6-astra")
        body["input"].append({"type": "reasoning", "id": "rs_foreign-k", "summary": [{"type": "summary_text", "text": "kimi"}]})
        response, data = self.call("POST", "/backend-api/codex/responses", body, {"Authorization": "Bearer chatgpt-token"})
        self.assertEqual(response.status, 200)
        calls = [call for call in FakeUpstreamHandler.calls if call["path"] == "/backend-api/codex/responses"]
        self.assertEqual(len(calls), 1)
        sent = json.loads(calls[0]["body"])
        self.assertFalse(any(item.get("id") == "rs_foreign-k" for item in sent["input"]))
        self.assertTrue(any(item.get("id") == "rs_1" for item in sent["input"]))

    def test_other_openai_errors_are_replayed_unchanged(self):
        body = codex_request("gpt-5.6-sol")
        body["model"] = "gpt-5.6-sol"
        body["input"].append({"type": "reasoning", "id": "rs_foreign-x", "encrypted_content": "blob"})
        # Three heals are the limit; the fake keeps rejecting because the fixture also carries rs_foreign-x twice.
        body["input"].append({"type": "reasoning", "id": "rs_foreign-y", "encrypted_content": "blob"})
        body["input"].append({"type": "reasoning", "id": "rs_foreign-z", "encrypted_content": "blob"})
        body["input"].append({"type": "reasoning", "id": "rs_foreign-w", "encrypted_content": "blob"})
        response, data = self.call("POST", "/backend-api/codex/responses", body, {"Authorization": "Bearer chatgpt-token"})
        self.assertEqual(response.status, 400)
        self.assertIn("could not be verified", data.decode("utf-8"))

    def test_catalog_etag_and_turn_header_agree_and_follow_the_registry(self):
        catalog, _ = self.call("GET", "/backend-api/codex/models?client_version=0.153.4", headers={"Authorization": "Bearer chatgpt-token"})
        turn, _ = self.call("POST", "/backend-api/codex/responses", codex_request("gpt-5.6-sol"), {"Authorization": "Bearer chatgpt-token"})
        self.assertEqual(catalog.getheader("ETag"), turn.getheader("X-Models-Etag"))
        self.assertTrue(catalog.getheader("ETag").startswith('W/"real-deck'))
        before = catalog.getheader("ETag")
        self.router.registry = type("Registry", (), {"load_models": staticmethod(lambda: {**REGISTERED, "qwen/new": REGISTERED["deepseek/test"]})})()
        self.router._registered_cache = ({}, 0.0)
        try:
            renamed, _ = self.call("GET", "/backend-api/codex/models?client_version=0.153.4", headers={"Authorization": "Bearer chatgpt-token"})
        finally:
            self.router.registry = FakeRegistry()
            self.router._registered_cache = ({}, 0.0)
        self.assertNotEqual(renamed.getheader("ETag"), before)

    def test_catalog_request_is_injected_and_retagged(self):
        response, data = self.call("GET", "/backend-api/codex/models?client_version=0.153.4", headers={"Authorization": "Bearer chatgpt-token"})
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("ETag").startswith('W/"real-deck'))
        models = json.loads(data)["models"]
        self.assertEqual([entry["slug"] for entry in models], ["gpt-5.5", "deepseek/test"])
        self.assertEqual(models[-1]["display_name"], "Test")  # automatic name when the registry has no custom one
        self.assertEqual(FakeUpstreamHandler.calls[-1]["headers"]["accept-encoding"], "identity")
        self.assertTrue(self.router.wait_for_catalog(1))

    def test_wait_for_catalog_gives_up_quietly_without_a_fetch(self):
        fresh = LocalRouter(FakeRegistry(), FakeKeys(), ledger_path=Path(self.temp.name) / "unused.jsonl",
                            log_path=Path(self.temp.name) / "unused.log")
        self.assertFalse(fresh.wait_for_catalog(0.2))

    def test_registered_model_goes_to_openrouter_with_its_own_key_only(self):
        response, data = self.call("POST", "/backend-api/codex/responses", codex_request("deepseek/test"),
                                   {"Authorization": "Bearer chatgpt-token", "chatgpt-account-id": "acct",
                                    "x-openai-subagent": "collab_spawn", "x-codex-turn-metadata": json.dumps({"thread_id": "t2", "agent_name": "/root/child"})})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        call = FakeUpstreamHandler.calls[-1]
        self.assertEqual(call["path"], "/api/v1/responses")
        self.assertEqual(call["headers"]["authorization"], "Bearer sk-or-test-key")
        self.assertNotIn("chatgpt-account-id", call["headers"])
        self.assertNotIn("x-codex-turn-metadata", call["headers"])
        sent = json.loads(call["body"])
        self.assertNotIn("include", sent)
        self.assertNotIn("prompt_cache_key", sent)
        self.assertNotIn("client_metadata", sent)
        self.assertNotIn("store", sent)
        self.assertEqual(sent["reasoning"], {"effort": "low"})
        events = self.events(data)
        self.assertEqual([event["type"] for event in events], ["response.created", "response.output_item.done", "response.completed"])
        self.assertEqual(events[1]["item"]["name"], "spawn_agent")
        self.assertEqual(events[1]["item"]["namespace"], "multi_agent_v1")
        self.assertIn(": OPENROUTER PROCESSING", data.decode("utf-8"))
        self.assertNotIn("[DONE]", data.decode("utf-8"))
        ledger = [json.loads(line) for line in (Path(self.temp.name) / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual(ledger[-1]["route"], "openrouter")
        self.assertEqual(ledger[-1]["agent_name"], "/root/child")
        self.assertEqual(ledger[-1]["usage"]["total_tokens"], 15)
        self.assertEqual(ledger[-1]["generation_id"], "gen-1")
        self.assertEqual(ledger[-1]["openrouter_model"], "deepseek/test")

    def test_cursor_uses_sdk_without_sending_chatgpt_headers_to_an_upstream(self):
        class CursorTransport:
            def __init__(self):
                self.requests = []

            def stream(self, request, metadata, api_key, account):
                self.requests.append((request, metadata, api_key, account))
                yield {"type": "response.created", "response": {"id": "cursor-response", "output": []}}
                yield None
                yield {"type": "response.output_item.done", "output_index": 0, "item": {
                    "type": "function_call", "id": "mdk-cursor-call", "call_id": "cursor-call",
                    "name": "_fetch", "namespace": "mcp__github", "arguments": "{}"}}
                yield {"type": "response.completed", "response": {
                    "id": "cursor-response", "status": "completed", "output": [],
                    "usage": None, "cost": None, "cursor_agent_id": "sdk-agent"}}

            def close(self):
                pass

        transport = CursorTransport()
        previous = self.router._cursor_manager
        self.router._cursor_manager = transport
        self.addCleanup(setattr, self.router, "_cursor_manager", previous)
        self.use_registry({"cursor/composer-2.5": {"endpoint": {
            "name": "Cursor", "base_url": "https://api.cursor.com", "wire": "cursor",
            "cursor": True, "openrouter": False, "has_key": True, "account": "saved-cursor-key"}}})
        response, raw = self.call("POST", "/backend-api/codex/responses", codex_request("cursor/composer-2.5"), {
            "Authorization": "Bearer chatgpt-private", "chatgpt-account-id": "private-account",
            "x-codex-turn-metadata": json.dumps({"thread_id": "thread", "agent_name": "/root/composer", "turn_id": "turn"})})
        self.assertEqual(response.status, 200)
        self.assertEqual(FakeUpstreamHandler.calls, [])
        body, metadata, key, account = transport.requests[0]
        self.assertEqual(body["model"], "cursor/composer-2.5")
        spawn_description = body["tools"][1]["tools"][0]["description"]
        self.assertIn("[Model Deck choices]", spawn_description)
        self.assertIn("Cursor subscription IDE/Cloud usage pools", spawn_description)
        self.assertIn("API price unknown", spawn_description)
        self.assertEqual(metadata, {"thread_id": "thread", "agent_name": "/root/composer", "turn_id": "turn"})
        self.assertEqual((key, account), ("sk-or-test-key", "saved-cursor-key"))
        self.assertNotIn("chatgpt-private", json.dumps(transport.requests))
        self.assertEqual(self.events(raw)[1]["item"]["namespace"], "mcp__github")
        ledger = [json.loads(line) for line in (Path(self.temp.name) / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual(ledger[-1]["route"], "cursor")
        self.assertEqual(ledger[-1]["billing"], "Cursor subscription")
        self.assertIsNone(ledger[-1]["usage"])
        self.assertIsNone(ledger[-1]["cost"])
        self.assertNotIn("sk-or-test-key", json.dumps(ledger[-1]))

    def test_cursor_compaction_runs_a_tool_less_summary_turn_on_the_same_model(self):
        class CursorTransport:
            def __init__(self):
                self.requests = []

            def stream(self, request, metadata, api_key, account):
                self.requests.append((request, metadata))
                yield {"type": "response.created", "response": {"id": "cursor-sum"}}
                yield None
                yield {"type": "response.output_text.delta", "item_id": "msg-1", "output_index": 0, "delta": "Half "}
                yield {"type": "response.output_item.done", "output_index": 0, "item": {
                    "type": "message", "id": "msg-1", "role": "assistant",
                    "content": [{"type": "output_text", "text": "Half done; next: ship."}]}}
                yield {"type": "response.completed", "response": {
                    "id": "cursor-sum", "status": "completed", "output": [], "usage": {"total_tokens": 9},
                    "cost": None, "cursor_agent_id": "sdk-agent"}}

            def close(self):
                pass

        transport = CursorTransport()
        previous = self.router._cursor_manager
        self.router._cursor_manager = transport
        self.addCleanup(setattr, self.router, "_cursor_manager", previous)
        self.use_registry({"cursor/composer-2.5": {"endpoint": {
            "name": "Cursor", "base_url": "https://api.cursor.com", "wire": "cursor",
            "cursor": True, "openrouter": False, "has_key": True, "account": "saved-cursor-key"}}})
        body = codex_request("cursor/composer-2.5")
        body["input"].append({"type": "compaction_trigger"})
        response, raw = self.call("POST", "/backend-api/codex/responses", body,
                                  {"x-codex-turn-metadata": json.dumps({"thread_id": "thread", "agent_name": "/root", "turn_id": "turn"})})
        self.assertEqual(response.status, 200)
        self.assertEqual(FakeUpstreamHandler.calls, [])
        events = self.events(raw)
        self.assertEqual([event["type"] for event in events], ["response.created", "response.output_item.added",
                                                               "response.output_item.done", "response.completed"])
        item = events[2]["item"]
        self.assertEqual(item["type"], "compaction")
        self.assertEqual(context_compaction.decode(item["encrypted_content"])["summary"], "Half done; next: ship.")
        self.assertEqual(events[3]["response"]["output"], [item])
        self.assertEqual(events[3]["response"]["usage"], {"total_tokens": 9})
        self.assertIn(": cursor agent active", raw.decode("utf-8"))
        sent, metadata = transport.requests[0]
        self.assertNotIn("tools", sent)
        self.assertNotIn("tool_choice", sent)
        self.assertNotIn("compaction_trigger", json.dumps(sent))
        self.assertEqual(sent["input"][-1]["content"][0]["text"], context_compaction.SUMMARIZATION_PROMPT)
        self.assertTrue(metadata["compaction"])
        ledger = [json.loads(line) for line in (Path(self.temp.name) / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual((ledger[-1]["route"], ledger[-1]["compaction"], ledger[-1]["status"]), ("cursor", True, 200))
        # Codex's older unary endpoint gets JSON back.
        response, raw = self.call("POST", "/backend-api/codex/responses/compact", codex_request("cursor/composer-2.5"),
                                  {"x-codex-turn-metadata": json.dumps({"thread_id": "thread", "agent_name": "/root", "turn_id": "turn2"})})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/json")
        output = json.loads(raw)["output"]
        self.assertEqual([entry["type"] for entry in output], ["compaction"])
        self.assertEqual(context_compaction.decode(output[0]["encrypted_content"])["model"], "cursor/composer-2.5")
        # A Cursor model that offers no Fast mode is refused explicitly, never silently slowed down.
        class FastRefusal(CursorTransport):
            def stream(self, request, metadata, api_key, account):
                from cursor_sdk_runtime import CursorRuntimeError
                raise CursorRuntimeError("This Cursor model has no Fast mode. Turn Fast off in Codex's model picker.")
                yield  # pragma: no cover
        self.router._cursor_manager = FastRefusal()
        _, raw = self.call("POST", "/backend-api/codex/responses", codex_request("cursor/composer-2.5", service_tier="priority"),
                           {"x-codex-turn-metadata": json.dumps({"thread_id": "thread", "agent_name": "/root", "turn_id": "turn3"})})
        self.assertIn("no Fast mode", self.events(raw)[0]["response"]["error"]["message"])

    def test_unregistered_model_fails_clearly_without_contacting_anyone(self):
        response, data = self.call("POST", "/backend-api/codex/responses", codex_request("qwen/unknown"))
        self.assertEqual(response.status, 200)
        events = self.events(data)
        self.assertEqual(events[0]["type"], "response.failed")
        self.assertIn("not registered", events[0]["response"]["error"]["message"])
        self.assertEqual(FakeUpstreamHandler.calls, [])

    def test_openrouter_auth_failure_is_reported_as_a_failed_response_not_an_http_401(self):
        self.router.key_provider = type("Keys", (), {"key_for": staticmethod(lambda model: "sk-wrong")})()
        try:
            response, data = self.call("POST", "/backend-api/codex/responses", codex_request("deepseek/test"))
        finally:
            self.router.key_provider = FakeKeys()
        self.assertEqual(response.status, 200)
        event = self.events(data)[0]
        self.assertEqual(event["type"], "response.failed")
        self.assertEqual(event["response"]["error"]["code"], "invalid_prompt")
        self.assertIn("API key", event["response"]["error"]["message"])

    def test_compaction_trigger_runs_a_summary_turn_on_the_endpoint_model(self):
        body = codex_request("deepseek/test")
        body["input"].append({"type": "compaction_trigger"})
        response, data = self.call("POST", "/backend-api/codex/responses", body)
        self.assertEqual(response.status, 200)
        events = self.events(data)
        self.assertEqual([event["type"] for event in events], ["response.created", "response.output_item.added",
                                                               "response.output_item.done", "response.completed"])
        item = events[2]["item"]
        self.assertEqual(item["type"], "compaction")
        self.assertNotIn("_", item["id"])
        record = context_compaction.decode(item["encrypted_content"])
        self.assertEqual((record["summary"], record["model"]), ("Summary: the task is half done.", "deepseek/test"))
        self.assertEqual(events[3]["response"]["usage"]["total_tokens"], 48)
        sent = json.loads(FakeUpstreamHandler.calls[-1]["body"])
        self.assertNotIn("tools", sent)
        self.assertNotIn("tool_choice", sent)
        self.assertNotIn("compaction_trigger", json.dumps(sent))
        self.assertEqual(sent["input"][-1], {"type": "message", "role": "user",
                                             "content": [{"type": "input_text", "text": context_compaction.SUMMARIZATION_PROMPT}]})
        self.assertEqual(sent["input"][0]["content"][0]["text"], "Rules")  # the whole history is summarized
        ledger = [json.loads(line) for line in (Path(self.temp.name) / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual((ledger[-1]["route"], ledger[-1]["compaction"], ledger[-1]["status"]), ("openrouter", True, 200))
        # The next turn carries the compaction item; the model sees the summary in its place.
        follow_up = codex_request("deepseek/test", input=[
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hi"}]}, item,
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Continue"}]}])
        self.call("POST", "/backend-api/codex/responses", follow_up)
        sent = json.loads(FakeUpstreamHandler.calls[-1]["body"])
        self.assertEqual([entry["type"] for entry in sent["input"]], ["message", "message", "message"])
        self.assertTrue(sent["input"][1]["content"][0]["text"].startswith(context_compaction.SUMMARY_PREFIX))
        self.assertIn("Summary: the task is half done.", sent["input"][1]["content"][0]["text"])
        self.assertNotIn(context_compaction.MARKER, json.dumps(sent))

    def test_unary_compact_endpoint_answers_with_json_for_endpoint_models(self):
        response, data = self.call("POST", "/backend-api/codex/responses/compact", codex_request("deepseek/test"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/json")
        output = json.loads(data)["output"]
        self.assertEqual([entry["type"] for entry in output], ["compaction"])
        self.assertEqual(context_compaction.decode(output[0]["encrypted_content"])["summary"], "Summary: the task is half done.")
        sent = json.loads(FakeUpstreamHandler.calls[-1]["body"])
        self.assertNotIn("tools", sent)
        self.assertEqual(sent["input"][-1]["content"][0]["text"], context_compaction.SUMMARIZATION_PROMPT)
        # Errors on the unary endpoint are JSON too, never an event stream.
        response, data = self.call("POST", "/backend-api/codex/responses/compact", codex_request("qwen/unknown"))
        self.assertEqual(response.status, 502)
        self.assertIn("not registered", json.loads(data)["error"]["message"])

    def test_websocket_upgrade_is_refused_so_codex_falls_back_to_http(self):
        response, _ = self.call("GET", "/backend-api/codex/responses", headers={"Upgrade": "websocket", "Connection": "Upgrade"})
        self.assertEqual(response.status, 426)

    def test_keychain_failure_surfaces_as_a_clear_error(self):
        from local_router import KeychainKeyProvider
        with self.assertRaises(RouterError):
            KeychainKeyProvider(FakeRegistry()).key_for("deepseek/test")


if __name__ == "__main__":
    unittest.main()
