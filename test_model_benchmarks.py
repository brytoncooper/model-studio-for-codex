"""Synthetic source-contract fixtures, not asserted real benchmark scores."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import urllib.request

import model_benchmarks as bm

CATALOG = {"data": [
    {"id": "lab/a", "canonical_slug": "lab/a-2026", "name": "A", "benchmarks": {
        "artificial_analysis": {"coding_index": 0, "intelligence_index": 61, "agentic_index": None},
        "design_arena": [{"arena": "models", "category": "website", "elo": 1200, "win_rate": 55, "rank": 2}],
        "future_test": {"score": 99}}},
    {"id": "lab/b", "canonical_slug": "lab/b-2026", "name": "A", "benchmarks": {
        "artificial_analysis": {"coding_index": 75},
        "design_arena": [{"arena": "models", "category": "website", "elo": 1250, "win_rate": 60, "rank": 1}]}},
    {"id": "lab/unscored", "name": "Unscored"}], "total_count": 3, "links": {"next": None}}
AA = {"data": [{"source": "artificial-analysis", "model_permaslug": "lab/a-2026", "coding_index": 10},
               {"source": "artificial-analysis", "model_permaslug": "A", "coding_index": 100}],
      "meta": {"as_of": "2026-09-10T00:00:00Z", "source_url": "https://artificialanalysis.ai/", "citation": "Synthetic citation", "version": "v1"}}


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 200000
        self.fail = set()
        self.requests = []
        self.store = bm.BenchmarkStore(Path(self.temp.name) / "cache.json", self.fetch, lambda: self.now)

    def fetch(self, url, headers):
        self.requests.append((url, headers))
        if url in self.fail:
            raise RuntimeError("secret-key-must-not-appear")
        return copy.deepcopy(CATALOG if url == bm.CATALOG_URL else AA if "artificial-analysis" in url else {"data": [], "meta": {}})

    def test_full_catalog_public_multitest_coverage_and_no_fuzzy_join(self):
        status = self.store.refresh()
        self.assertEqual(status["catalog_models"], 3)
        self.assertEqual(status["models_with_scores"], 2)
        self.assertEqual(status["models_without_scores"], 1)
        self.assertIn("future_test", status["feeds"][0]["unsupported_benchmark_keys"])
        profile = self.store.profile("lab/a")
        self.assertEqual(len(profile["scores"]), 5)
        self.assertTrue(all(row["identity_match"] == "exact_id" for row in profile["scores"]))
        self.assertTrue(all(row["as_of"] is None for row in profile["scores"]))
        self.assertEqual(self.store.profile("A")["scores"], [])
        self.assertEqual(self.store.profile("cursor/lab/a")["scores"], [])
        self.assertEqual(self.requests, [(bm.CATALOG_URL, {})])

    def test_nonfinite_bool_and_missing_are_not_zero(self):
        payload = copy.deepcopy(CATALOG)
        payload["data"][0]["benchmarks"]["artificial_analysis"] = {
            "intelligence_index": float("nan"), "coding_index": 0, "agentic_index": True}
        payload["data"][0]["benchmarks"]["design_arena"][0]["elo"] = float("inf")
        normalized = bm.normalize(payload, "catalog")
        rows = [row for row in normalized["scores"] if row["benchmark_model_id"] == "lab/a"]
        self.assertEqual(next(row["score"] for row in rows if row["metric"] == "coding_index"), 0)
        self.assertFalse(any(row["metric"] in ("intelligence_index", "agentic_index", "elo") for row in rows))
        json.dumps(normalized, allow_nan=False)

    def test_canonical_identity_and_unmatched_provenance(self):
        self.store.refresh({"Authorization": "Bearer fake-test-key"})
        row = next(row for row in self.store.profile("lab/a")["scores"] if row["metric"] == "coding_index")
        self.assertEqual(row["score"], 10)
        self.assertEqual(row["identity_match"], "catalog_canonical_slug")
        self.assertEqual(row["as_of"], AA["meta"]["as_of"])
        self.assertEqual(row["citation"], "Synthetic citation")
        self.assertEqual(self.store.status()["unmatched_identities"], 1)
        self.assertEqual(self.store.profile("lab/b")["scores"][0]["score"], 75)
        self.assertNotIn("fake-test-key", self.store.path.read_text())
        self.assertEqual(len(self.requests), 5)
        self.assertEqual(next(headers for url, headers in self.requests if url == bm.CATALOG_URL), {})

    def test_failed_source_retains_stale_data_without_secret_and_fresh_public_wins(self):
        self.store.refresh({"Authorization": "Bearer fake-test-key"})
        self.fail.add(bm.FEEDS["artificial-analysis"])
        self.now += 5
        result = self.store.refresh({"Authorization": "Bearer fake-test-key"})
        self.assertTrue(next(feed for feed in result["feeds"] if feed["feed"] == "artificial-analysis")["stale"])
        self.assertEqual(next(row for row in self.store.profile("lab/a")["scores"] if row["metric"] == "coding_index")["score"], 0)
        self.assertNotIn("secret-key", json.dumps(result))
        self.fail.add(bm.CATALOG_URL)
        self.now += bm.TTL
        self.store.refresh()
        self.assertTrue(all(row["stale"] for row in self.store.profile("lab/a")["scores"]))
        self.assertEqual(self.store.status()["catalog_models"], 3)
        loaded = bm.BenchmarkStore(self.store.path, self.fetch, lambda: self.now)
        self.assertEqual(loaded.status()["catalog_models"], 3)

    def test_rank_direction_pagination_and_missing_comparisons(self):
        self.store.refresh()
        self.assertEqual(self.store.rank("coding", limit=1)["models"][0]["model"], "lab/b")
        self.assertEqual(self.store.rank("coding", limit=1)["next_offset"], 1)
        self.assertEqual(self.store.rank("website", "design-arena", "rank")["models"][0]["model"], "lab/b")
        compared = self.store.compare(["lab/a", "lab/b", "lab/unscored"], task="coding")
        self.assertEqual(compared["comparisons"][0]["missing_models"], ["lab/unscored"])
        self.assertEqual(self.store.profile("lab/a", limit=1)["next_offset"], 1)

    def test_partial_shape_rejected_and_redirects_blocked(self):
        for payload in ({}, {"data": {}}, {"data": [], "total_count": 4}, {"data": [], "links": {"next": "/page2"}}):
            with self.assertRaises(bm.BenchmarkError):
                bm.normalize(payload, "catalog")
        with self.assertRaises(bm.BenchmarkError):
            bm.fetch_json("https://evil.example/benchmarks", {"Authorization": "Bearer fake"})
        with self.assertRaises(bm.BenchmarkError):
            bm._NoRedirect().redirect_request(urllib.request.Request(bm.BENCHMARK_URL), None, 302, "redirect", {}, "https://evil.example")

    def test_mixed_snapshots_are_ranked_separately(self):
        self.store.refresh({"Authorization": "Bearer fake-test-key"})
        ranked = self.store.rank("coding")
        self.assertEqual(ranked["models"], [])
        self.assertEqual(len(ranked["rankings_by_snapshot"]), 2)
        with self.assertRaises(bm.BenchmarkError):
            bm.normalize({"data": [{"unexpected": "shape"}]}, "artificial-analysis")

    def test_cache_ttl_and_bad_cache(self):
        self.store.ensure()
        self.store.ensure()
        self.assertEqual(len(self.requests), 1)
        self.now += bm.TTL
        self.store.ensure()
        self.assertEqual(len(self.requests), 2)
        self.store.path.write_text('[]')
        self.assertEqual(bm.BenchmarkStore(self.store.path).status()["catalog_models"], 0)


if __name__ == "__main__":
    unittest.main()
