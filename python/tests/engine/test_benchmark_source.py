"""Offline tests for the benchmark evidence adapter
(python/src/model_deck/adapters/evidence/benchmarks.py).

No test here touches the network: `fetch_benchmarks` is always given a fake transport, and
`parse_benchmarks`/`(de)serialize_benchmark_snapshot` never accept one in the first place.
"""
import json
import unittest
from pathlib import Path

from model_deck.adapters.evidence.benchmarks import (
    FEED_ARTIFICIAL_ANALYSIS,
    FEED_CATALOG,
    FEEDS,
    SOURCE_ID,
    BenchmarkFeedSnapshot,
    BenchmarkFormatError,
    BenchmarkRecord,
    BenchmarkSourceError,
    BenchmarkTransportError,
    deserialize_benchmark_snapshot,
    fetch_benchmarks,
    is_stale,
    parse_benchmarks,
    serialize_benchmark_snapshot,
)
from model_deck.adapters.evidence.provenance import SourceProvenance
from model_deck_contracts.validator import validate_schema_ref

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "evidence"
NOW = 1_757_900_000.0  # arbitrary fixed epoch so expectations are exact, not clock-dependent


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ParseCatalogFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = parse_benchmarks(_load("benchmarks_catalog_sample.json"), FEED_CATALOG, NOW)

    def test_artificial_analysis_rows_split_present_null_and_absent_metrics(self) -> None:
        """intelligence_index is present with a value, coding_index is present but null
        (kept, as an explicit unknown), agentic_index is absent entirely (not invented)."""
        aa_records = [r for r in self.snapshot.records if r.source_id == "ai.artificialanalysis"]
        scores_by_metric = {r.metric: r.score for r in aa_records}
        self.assertEqual(scores_by_metric, {"intelligence_index": 70.5, "coding_index": None})
        self.assertNotIn("agentic_index", scores_by_metric)

    def test_design_arena_rows_are_split_by_arena_and_category_within_feed(self) -> None:
        """benchmark_record has no arena/category field, so the catalog-embedded arena and
        category are folded into `feed` -- otherwise the (models, coding) elo and the
        (builders, webapp) elo would collide under the same key."""
        da_records = [r for r in self.snapshot.records if r.source_id == "org.designarena"]
        feeds = {r.feed for r in da_records}
        self.assertEqual(
            feeds,
            {"catalog/design-arena/models/coding", "catalog/design-arena/builders/webapp"},
        )
        by_feed_metric = {(r.feed, r.metric): r.score for r in da_records}
        self.assertEqual(by_feed_metric[("catalog/design-arena/models/coding", "elo")], 1500)
        self.assertEqual(by_feed_metric[("catalog/design-arena/models/coding", "win_rate")], 0.6)
        self.assertEqual(by_feed_metric[("catalog/design-arena/builders/webapp", "elo")], 1400)
        # win_rate was never mentioned for the builders/webapp row: no invented record.
        self.assertNotIn(("catalog/design-arena/builders/webapp", "win_rate"), by_feed_metric)

    def test_model_with_no_scores_at_all_produces_no_records_and_is_not_an_error(self) -> None:
        """A legitimate source can list a model with zero scores; that must not raise and
        must not fabricate a record for it."""
        self.assertFalse(any(r.source_model_ref == "vendor/unscored-model" for r in self.snapshot.records))

    def test_non_dict_rows_are_skipped_without_raising(self) -> None:
        self.assertFalse(any(r.source_model_ref is None for r in self.snapshot.records))

    def test_provider_model_id_is_always_null_mapping_is_an_engine_concern(self) -> None:
        for record in self.snapshot.records:
            self.assertIsNone(record.provider_model_id)

    def test_every_record_and_the_snapshot_provenance_matches_the_frozen_schema(self) -> None:
        self.assertTrue(self.snapshot.records, "fixture must produce at least one record")
        for record in self.snapshot.records:
            validate_schema_ref(
                "engine.v1/vocabulary.schema.json#/definitions/benchmark_record", record.to_wire()
            )
        validate_schema_ref(
            "engine.v1/vocabulary.schema.json#/definitions/source_provenance",
            self.snapshot.provenance.to_wire(),
        )

    def test_per_row_provenance_carries_the_publisher_not_the_openrouter_endpoint(self) -> None:
        aa_record = next(r for r in self.snapshot.records if r.source_id == "ai.artificialanalysis")
        self.assertEqual(aa_record.provenance.source_id, "ai.artificialanalysis")
        self.assertEqual(aa_record.provenance.as_of, "2026-09-01")
        self.assertEqual(aa_record.provenance.citation, "OpenRouter public catalog")
        da_record = next(r for r in self.snapshot.records if r.source_id == "org.designarena")
        self.assertEqual(da_record.provenance.source_id, "org.designarena")

    def test_snapshot_level_provenance_identifies_the_openrouter_endpoint(self) -> None:
        self.assertEqual(self.snapshot.provenance.source_id, SOURCE_ID)
        self.assertEqual(self.snapshot.provenance.source_url, FEEDS[FEED_CATALOG])
        self.assertFalse(self.snapshot.provenance.stale)

    def test_catalog_with_no_valid_identities_is_rejected(self) -> None:
        with self.assertRaises(BenchmarkFormatError):
            parse_benchmarks(_load("benchmarks_catalog_no_identities.json"), FEED_CATALOG, NOW)


class ParseStandaloneFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = parse_benchmarks(
            _load("benchmarks_artificial_analysis_feed.json"), FEED_ARTIFICIAL_ANALYSIS, NOW
        )

    def test_only_rows_matching_the_feeds_source_are_kept(self) -> None:
        """A row claiming a different `source` than the feed being parsed, and a row with
        no model identity at all, are both dropped rather than mis-attributed."""
        self.assertEqual(len(self.snapshot.records), 2)  # intelligence_index + coding_index for gpt-5 only
        self.assertTrue(all(r.source_model_ref == "openai/gpt-5" for r in self.snapshot.records))

    def test_feed_level_meta_is_carried_into_every_records_provenance(self) -> None:
        for record in self.snapshot.records:
            self.assertEqual(record.provenance.as_of, "2026-09-10")
            self.assertEqual(record.provenance.source_url, "https://artificialanalysis.ai/models/gpt-5")
            self.assertEqual(record.feed, FEED_ARTIFICIAL_ANALYSIS)

    def test_all_rows_invalid_is_rejected_rather_than_returning_an_empty_snapshot(self) -> None:
        with self.assertRaises(BenchmarkFormatError):
            parse_benchmarks(_load("benchmarks_feed_all_rows_invalid.json"), FEED_ARTIFICIAL_ANALYSIS, NOW)

    def test_malformed_document_is_rejected(self) -> None:
        with self.assertRaises(BenchmarkFormatError):
            parse_benchmarks(_load("benchmarks_malformed.json"), FEED_ARTIFICIAL_ANALYSIS, NOW)

    def test_unknown_feed_name_is_rejected(self) -> None:
        with self.assertRaises(BenchmarkFormatError):
            parse_benchmarks({"data": []}, "not-a-real-feed", NOW)


class FetchBenchmarksTests(unittest.TestCase):
    def test_fetch_uses_the_injected_transport_and_the_documented_feed_url(self) -> None:
        seen_urls = []

        def fake_transport(url: str) -> bytes:
            seen_urls.append(url)
            return json.dumps(_load("benchmarks_artificial_analysis_feed.json")).encode("utf-8")

        snapshot = fetch_benchmarks(fake_transport, FEED_ARTIFICIAL_ANALYSIS, NOW)
        self.assertEqual(seen_urls, [FEEDS[FEED_ARTIFICIAL_ANALYSIS]])
        self.assertEqual(len(snapshot.records), 2)

    def test_transport_failure_surfaces_as_a_typed_error_with_no_partial_snapshot(self) -> None:
        def broken_transport(url: str) -> bytes:
            raise TimeoutError("simulated timeout")

        with self.assertRaises(BenchmarkTransportError) as ctx:
            fetch_benchmarks(broken_transport, FEED_CATALOG, NOW)
        self.assertIsInstance(ctx.exception, BenchmarkSourceError)

    def test_transport_returning_invalid_json_surfaces_as_a_typed_format_error(self) -> None:
        def garbage_transport(url: str) -> bytes:
            return b"{not-json"

        with self.assertRaises(BenchmarkFormatError):
            fetch_benchmarks(garbage_transport, FEED_CATALOG, NOW)

    def test_fetch_rejects_an_unknown_feed_before_calling_the_transport(self) -> None:
        calls = []

        def should_not_be_called(url: str) -> bytes:
            calls.append(url)
            return b"{}"

        with self.assertRaises(BenchmarkFormatError):
            fetch_benchmarks(should_not_be_called, "not-a-real-feed", NOW)
        self.assertEqual(calls, [])


class StalenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = parse_benchmarks(
            _load("benchmarks_artificial_analysis_feed.json"), FEED_ARTIFICIAL_ANALYSIS, NOW
        )

    def test_fresh_snapshot_is_not_stale(self) -> None:
        self.assertFalse(is_stale(self.snapshot, NOW, max_age=3600))

    def test_just_under_max_age_is_not_stale(self) -> None:
        self.assertFalse(is_stale(self.snapshot, NOW + 3599, max_age=3600))

    def test_at_max_age_boundary_is_stale(self) -> None:
        self.assertTrue(is_stale(self.snapshot, NOW + 3600, max_age=3600))


class SerializeRoundTripTests(unittest.TestCase):
    def test_round_trip_preserves_records_and_provenance(self) -> None:
        original = parse_benchmarks(_load("benchmarks_catalog_sample.json"), FEED_CATALOG, NOW)
        text = serialize_benchmark_snapshot(original)
        restored = deserialize_benchmark_snapshot(text)
        self.assertEqual(restored, original)
        self.assertEqual(
            [r.to_wire() for r in restored.records], [r.to_wire() for r in original.records]
        )

    def test_serialized_document_carries_a_top_level_fetched_at_and_provenance(self) -> None:
        snapshot = parse_benchmarks(_load("benchmarks_catalog_sample.json"), FEED_CATALOG, NOW)
        document = json.loads(serialize_benchmark_snapshot(snapshot))
        self.assertEqual(document["fetched_at"], snapshot.provenance.fetched_at)
        self.assertEqual(document["provenance"]["source_id"], SOURCE_ID)
        self.assertIn("benchmarks", document)

    def test_deserialize_rejects_a_document_missing_required_fields(self) -> None:
        with self.assertRaises(BenchmarkFormatError):
            deserialize_benchmark_snapshot(json.dumps({"provenance": {}}))

    def test_deserialize_rejects_invalid_json_text(self) -> None:
        with self.assertRaises(BenchmarkFormatError):
            deserialize_benchmark_snapshot("{not valid json")

    def test_deserialize_rejects_a_json_array_document(self) -> None:
        with self.assertRaises(BenchmarkFormatError):
            deserialize_benchmark_snapshot("[]")


class WireDataclassTests(unittest.TestCase):
    def test_benchmark_record_wire_round_trip_with_null_score(self) -> None:
        provenance = SourceProvenance(
            source_id="ai.artificialanalysis", fetched_at="2026-09-15T00:00:00Z", stale=False,
            source_url="https://artificialanalysis.ai/", as_of="2026-09-01",
        )
        record = BenchmarkRecord(
            source_model_ref="vendor/model",
            provider_model_id=None,
            source_id="ai.artificialanalysis",
            feed="artificial-analysis",
            metric="intelligence_index",
            score=None,
            provenance=provenance,
        )
        wire = record.to_wire()
        self.assertIsNone(wire["score"])
        self.assertIsNone(wire["provider_model_id"])
        self.assertEqual(BenchmarkRecord.from_wire(wire), record)
        validate_schema_ref("engine.v1/vocabulary.schema.json#/definitions/benchmark_record", wire)

    def test_benchmark_record_with_mapped_provider_model_id_still_validates(self) -> None:
        provenance = SourceProvenance(source_id="org.designarena", fetched_at="2026-09-15T00:00:00Z", stale=True)
        record = BenchmarkRecord(
            source_model_ref="vendor-slug/model",
            provider_model_id="vendor/model",
            source_id="org.designarena",
            feed="design-arena/models/coding",
            metric="elo",
            score=1500.0,
            provenance=provenance,
        )
        validate_schema_ref("engine.v1/vocabulary.schema.json#/definitions/benchmark_record", record.to_wire())

    def test_feed_snapshot_with_zero_records_still_validates_its_provenance(self) -> None:
        provenance = SourceProvenance(source_id=SOURCE_ID, fetched_at="2026-09-15T00:00:00Z", stale=True)
        snapshot = BenchmarkFeedSnapshot(feed=FEED_CATALOG, records=(), provenance=provenance)
        self.assertEqual(snapshot.to_wire()["benchmarks"], [])
        validate_schema_ref(
            "engine.v1/vocabulary.schema.json#/definitions/source_provenance",
            snapshot.to_wire()["provenance"],
        )


if __name__ == "__main__":
    unittest.main()
