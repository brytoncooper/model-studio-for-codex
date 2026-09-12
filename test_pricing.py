import io
import json
from pathlib import Path
import tempfile
import time
import unittest

import pricing


SAMPLE = {"data": [
    {"id": "deepseek/deepseek-v4.1-flash", "name": "DeepSeek: V4.1 Flash", "context_length": 1048576,
     "pricing": {"prompt": "0.00000015", "completion": "0.0000006", "input_cache_read": "0.000000003"},
     "architecture": {"input_modalities": ["text", "image"]}, "supported_parameters": ["tools", "reasoning"],
     "description": "Fast."},
    {"id": "some/free-model:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 32000},
    {"id": "weird/dynamic", "pricing": {"prompt": "-1", "completion": "abc"}},
    {"id": 5}, "junk",
]}


class PricingTests(unittest.TestCase):
    def test_parse_reduces_to_per_million_and_metadata(self):
        parsed = pricing.parse_pricing(SAMPLE)
        flash = parsed["deepseek/deepseek-v4.1-flash"]
        self.assertEqual((flash["input"], flash["output"], flash["cache_read"], flash["cache_write"]), (0.15, 0.6, 0.003, None))
        self.assertEqual(flash["context"], 1048576)
        self.assertEqual(flash["modalities"], ["text", "image"])
        self.assertTrue(flash["tools"] and flash["reasoning"])
        self.assertEqual(parsed["some/free-model:free"]["input"], 0)
        self.assertEqual(parsed["weird/dynamic"], dict(parsed["weird/dynamic"], input=None, output=None))
        self.assertNotIn(5, parsed)
        with self.assertRaises(ValueError):
            pricing.parse_pricing({"data": "no"})

    def test_price_line_and_variant_lookup(self):
        parsed = pricing.parse_pricing(SAMPLE)
        self.assertEqual(pricing.price_line(parsed["deepseek/deepseek-v4.1-flash"]), "in $0.15/M · out $0.6/M · cached $0.003/M · 1.04858M context")
        self.assertEqual(pricing.price_line(parsed["some/free-model:free"]), "in $0/M · out $0/M · 32k context")
        self.assertEqual(pricing.price_line(None), "")
        self.assertIs(pricing.pricing_for("deepseek/deepseek-v4.1-flash:nitro", parsed), parsed["deepseek/deepseek-v4.1-flash"])
        self.assertIsNone(pricing.pricing_for("nobody/nothing", parsed))

    def test_estimate_cost_uses_cache_rate(self):
        entry = {"input": 1.0, "output": 2.0, "cache_read": 0.1}
        self.assertAlmostEqual(pricing.estimate_cost(entry, {"input_tokens": 1_000_000, "cached_tokens": 500_000, "output_tokens": 100_000}),
                               0.5 + 0.05 + 0.2)
        self.assertIsNone(pricing.estimate_cost({"input": None, "output": 1}, {}))

    def test_cache_round_trip_and_staleness(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pricing-cache.json"
            self.assertEqual(pricing.load_cached(path), ({}, False))
            class Response(io.BytesIO):
                def __enter__(self): return self
                def __exit__(self, *args): return False
            calls = []
            def opener(request, timeout):
                calls.append(request.full_url)
                return Response(json.dumps(SAMPLE).encode())
            fetched = pricing.refresh(path, opener=opener)
            self.assertIn("deepseek/deepseek-v4.1-flash", fetched)
            self.assertEqual(calls, [pricing.PRICING_URL])
            cached, fresh = pricing.load_cached(path)
            self.assertTrue(fresh)
            self.assertEqual(cached, fetched)
            document = json.loads(path.read_text())
            document["fetched"] = time.time() - pricing.CACHE_TTL - 1
            path.write_text(json.dumps(document))
            self.assertEqual(pricing.load_cached(path)[1], False)
            self.assertEqual(pricing.load(path, refresh_if_stale=False), fetched)
            path.write_text("garbage")
            self.assertEqual(pricing.load_cached(path), ({}, False))


if __name__ == "__main__":
    unittest.main()
