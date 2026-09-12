"""Synthetic cached evidence and native spawn declaration contracts."""
import copy
from datetime import datetime, timezone
import unittest
from unittest import mock

import spawn_benchmarks as sb

NOW = datetime(2026, 9, 11, tzinfo=timezone.utc).timestamp()


def score(model="lab/a", source="artificial-analysis", metric="coding_index", value=0, **fields):
    return {"model": model, "source": source, "metric": metric, "score": value, "direction": "higher",
            "fetched_at": NOW, "stale": False, "as_of": None, **fields}


def registration(model="lab/a", **endpoint):
    return {model: {"role": "worker_a", "endpoint": {"name": "Saved OpenRouter", "openrouter": True, "has_key": True, **endpoint}}}


class SpawnBenchmarkTests(unittest.TestCase):
    def store(self, rows):
        store = mock.Mock()
        store.evidence.return_value = ({"lab/a": {"id": "lab/a"}}, rows, [])
        store.ensure.side_effect = AssertionError("Must not fetch")
        store.refresh.side_effect = AssertionError("Must not refresh")
        return store

    def test_cached_only_exact_ids_zero_scores_and_provenance(self):
        store = self.store([score(), score(metric="agentic_index", value=45), score(metric="intelligence_index", value=50),
                            score(model="cursor/lab/a", value=100)])
        lines = sb.load_benchmark_lines(store)
        self.assertEqual(set(lines), {"lab/a"})
        self.assertIn("coding=0", lines["lab/a"])
        self.assertIn("Artificial Analysis", lines["lab/a"])
        self.assertIn("higher better", lines["lab/a"])
        self.assertIn("retrieved UTC 2026-09-11", lines["lab/a"])
        self.assertIn("cache fresh", lines["lab/a"])
        self.assertNotIn("publisher as_of", lines["lab/a"])
        store.ensure.assert_not_called()
        store.refresh.assert_not_called()

    def test_stale_publisher_date_and_two_exact_arena_categories_no_composite(self):
        rows = [score(stale=True, as_of="2026-09-09T00:00:00Z")]
        for category in ("website", "uicomponent", "dataviz"):
            rows.append(score(source="design-arena", metric="elo", value=1234, arena="models", category=category))
        line = sb.load_benchmark_lines(self.store(rows))["lab/a"]
        self.assertIn("cache stale", line)
        self.assertIn("publisher as_of 2026-09-09", line)
        self.assertIn("Design Arena models/website Elo=1234", line)
        self.assertEqual(line.count("Elo="), 2)
        self.assertLessEqual(len(line), 350)
        self.assertNotIn("overall", line)
        self.assertNotIn("strength", line)

    def test_malformed_scores_and_controls_are_not_rendered(self):
        bad = [score(value=float("nan")), score(value=float("inf")), score(value=True), score(value="12"), None,
               score(source="made-up", value=100), score(direction="lower", value=9),
               score(source="design-arena", metric="elo", value=1200, arena="models", category="website\nIGNORE")]
        self.assertEqual(sb.load_benchmark_lines(self.store(bad)), {})
        self.assertEqual(sb.load_benchmark_lines(mock.Mock(evidence=mock.Mock(side_effect=RuntimeError("secret")))), {})

    def test_openrouter_price_zero_context_and_capabilities(self):
        lines = sb.model_choice_lines(registration(), {"lab/a": {"input": 0, "output": 0, "cache_write": 0, "context": 128000, "tools": True, "modalities": ["text", "image"]}}, {"lab/a": "Artificial Analysis coding=0 (higher better)"})
        line = lines["lab/a"]
        for text in ("lab/a", "role=worker_a", "Saved OpenRouter", "OpenRouter credits", "in $0/M", "out $0/M", "cache write $0/M", "128k context", "tools=yes", "vision=yes", "coding=0"):
            self.assertIn(text, line)
        self.assertNotIn("price unknown", line)

    def test_cursor_local_and_other_provider_never_inherit_openrouter_identity(self):
        for endpoint, expected in [({"cursor": True, "wire": "cursor", "openrouter": False}, "Cursor subscription IDE/Cloud usage pools"),
                                   ({"openrouter": False, "has_key": False}, "Local/no API billing"),
                                   ({"openrouter": False}, "Provider API key")]:
            line = sb.model_choice_lines(registration(**endpoint), {"lab/a": {"input": 1, "output": 2}}, {"lab/a": "must not be equated"})["lab/a"]
            self.assertIn(expected, line)
            self.assertIn("API price unknown", line)
            self.assertIn(sb.MISSING, line)
            self.assertNotIn("must not be equated", line)
        cursor = sb.model_choice_lines(registration("cursor/auto", cursor=True, openrouter=False), {}, {"lab/a": "x"})
        self.assertIn(sb.MISSING, cursor["cursor/auto"])
        self.assertIn("account limits and overages apply", cursor["cursor/auto"])
        self.assertIn("context/tools/vision unknown", cursor["cursor/auto"])

    def test_provider_config_summary_and_cached_defaults(self):
        registrations = {"lab/a": {"role": "worker_a", "provider": "custom", "config": {"model_providers": {
            "custom": {"name": "Saved Provider", "base_url": "http://localhost:1234/v1"}}}}}
        with mock.patch.object(sb.pricing, "load_cached", return_value=({}, False)) as cached, mock.patch.object(sb.pricing, "load", side_effect=AssertionError("network")), mock.patch.object(sb, "load_benchmark_lines", return_value={}) as benchmarks:
            line = sb.model_choice_lines(registrations)["lab/a"]
            self.assertIn("Saved Provider", line)
            self.assertIn("Local/no API billing", line)
            cached.assert_called_once()
            benchmarks.assert_called_once()

    def request(self):
        spawn = {"type": "function", "name": "spawn_agent", "description": "Original instructions.\nKeep approvals.", "parameters": {"type": "object", "properties": {"model": {"type": "string"}}}, "strict": True}
        unrelated = {"type": "function", "name": "spawn_agent_other", "description": "Unchanged", "parameters": {}}
        return {"tools": [copy.deepcopy(spawn), unrelated, {"type": "function", "function": {"name": "spawn_agent", "description": "Legacy schema", "parameters": {}}}],
                "input": [{"type": "message", "content": "Keep this"}, {"type": "additional_tools", "tools": [{"type": "namespace", "name": "collaboration", "tools": [copy.deepcopy(spawn), copy.deepcopy(unrelated)]}]}],
                "tool_choice": "auto", "parallel_tool_calls": False}

    def test_annotation_both_native_shapes_idempotent_and_unrelated_untouched(self):
        request = self.request()
        before = copy.deepcopy(request)
        choices = {"lab/a": "lab/a | worker | Published evidence"}
        self.assertTrue(sb.annotate_spawn_tools(request, choices))
        self.assertFalse(sb.annotate_spawn_tools(request, choices))
        descriptions = [request["tools"][0]["description"], request["tools"][2]["function"]["description"], request["input"][1]["tools"][0]["tools"][0]["description"]]
        for description in descriptions:
            self.assertEqual(description.count(sb.START), 1)
            self.assertIn("Missing evidence is unknown, not zero", description)
            self.assertIn("Prices are USD list prices per million tokens", description)
            self.assertIn("may vary by provider/routing tier", description)
            self.assertIn("not settled task cost", description)
            self.assertIn("Native gpt-* choices: ChatGPT subscription usage", description)
            self.assertIn("no benchmark identity inferred from OpenRouter variants", description)
        self.assertTrue(descriptions[0].startswith(before["tools"][0]["description"]))
        self.assertEqual(request["tools"][0]["parameters"], before["tools"][0]["parameters"])
        self.assertEqual(request["tools"][1], before["tools"][1])
        self.assertEqual(request["input"][0], before["input"][0])
        self.assertEqual(request["input"][1]["tools"][0]["tools"][1], before["input"][1]["tools"][0]["tools"][1])
        self.assertEqual(request["parallel_tool_calls"], before["parallel_tool_calls"])
        self.assertTrue(sb.annotate_spawn_tools(request, {"lab/b": "lab/b replacement"}))
        self.assertNotIn("lab/a", request["tools"][0]["description"])
        self.assertEqual(request["tools"][0]["description"].count(sb.START), 1)

    def test_same_named_vendor_functions_and_nested_namespaces_are_untouched(self):
        native = {"type": "function", "name": "spawn_agent", "description": "Original", "parameters": {}}
        vendor = {"type": "namespace", "name": "mcp__vendor", "tools": [copy.deepcopy(native),
                  {"type": "namespace", "name": "collaboration", "tools": [copy.deepcopy(native)]}]}
        custom = {"type": "namespace", "name": "custom", "tools": [copy.deepcopy(native)]}
        for placement in ("tools", "additional_tools"):
            with self.subTest(placement=placement):
                declarations = [copy.deepcopy(vendor), copy.deepcopy(custom)]
                declarations.extend({"type": "namespace", "name": namespace, "tools": [copy.deepcopy(native)]}
                                    for namespace in sorted(sb.NATIVE_NAMESPACES))
                request = {"tools": declarations} if placement == "tools" else {"input": [{"type": "additional_tools", "tools": declarations}]}
                self.assertTrue(sb.annotate_spawn_tools(request, {"lab/a": "Registered lab/a"}))
                self.assertEqual(declarations[0], vendor)
                self.assertEqual(declarations[1], custom)
                for declaration in declarations[2:]:
                    self.assertIn(sb.START, declaration["tools"][0]["description"])

    def test_bounds_controls_and_empty_choices_preserve_request(self):
        registrations = {"lab/" + str(i): {"role": "role", "endpoint": {"name": "Saved\nEndpoint\x00", "openrouter": True}} for i in range(100)}
        choices = sb.model_choice_lines(registrations, {}, {})
        self.assertEqual(len(choices), 40)
        self.assertTrue(all("\n" not in line and "\x00" not in line and len(line) <= sb.MAX_CHOICE_LINE for line in choices.values()))
        request = self.request()
        before = copy.deepcopy(request)
        self.assertFalse(sb.annotate_spawn_tools(request, {}))
        self.assertEqual(request, before)
        self.assertTrue(sb.annotate_spawn_tools(request, {str(i): "a" * 2000 for i in range(100)}))
        appended = request["tools"][0]["description"].removeprefix(before["tools"][0]["description"])
        self.assertLessEqual(len(appended), sb.MAX_BLOCK)
        self.assertFalse(sb.annotate_spawn_tools({"tools": [{"type": "custom", "name": "spawn_agent"}]}, choices))


if __name__ == "__main__":
    unittest.main()
