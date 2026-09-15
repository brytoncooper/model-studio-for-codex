"""End-to-end tests of `HttpEvidenceSource`, the seam where the adapter's record
vocabulary meets the engine's (python/src/model_deck/adapters/evidence/source.py).

`test_price_source.py` tests the adapter alone and `test_evidence_cache.py` tests the
engine against hand-written bodies, so neither exercises the pair. This module drives the
real bridge with a fake transport serving the offline fixtures, refreshes the engine cache
through `RefreshEvidenceUseCase`, reads it back through the query use cases, and asserts a
concrete dollar figure — the one assertion that catches a unit mismatch between the two
sides. No test here touches the network.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from model_deck.adapters.evidence.benchmarks import (
    FEED_ARTIFICIAL_ANALYSIS,
    FEEDS,
    BenchmarkTransportError,
)
from model_deck.adapters.evidence.benchmarks import SOURCE_ID as BENCHMARK_SOURCE_ID
from model_deck.adapters.evidence.prices import PRICING_URL, PriceTransportError
from model_deck.adapters.evidence.prices import SOURCE_ID as PRICE_SOURCE_ID
from model_deck.adapters.evidence.source import (
    DEFAULT_BENCHMARK_FEED,
    HttpEvidenceSource,
)
from model_deck.engine.evidence import (
    BENCHMARKS_KIND,
    PRICES_KIND,
    REFRESH_FETCH_FAILED,
    EvidenceSnapshot,
    QueryBenchmarksUseCase,
    QueryPricesUseCase,
    RefreshEvidenceUseCase,
    estimate_cost,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "evidence"

# A fixed epoch so every fetched_at is exact rather than clock-dependent.
FETCH_EPOCH = 1_757_900_000.0
FETCHED_AT = "2025-09-15T01:33:20Z"
READ_AT = "2025-09-15T02:00:00Z"  # well inside the default 24h staleness window

BENCHMARK_FEED_URL = FEEDS[FEED_ARTIFICIAL_ANALYSIS]


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FixtureTransport:
    """The injected `Callable[[str], bytes]`, serving fixtures by URL. Requesting a URL
    this transport was not given is a failure, not an empty answer."""

    def __init__(self, bodies: dict[str, bytes]) -> None:
        self._bodies = bodies
        self.requested: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.requested.append(url)
        try:
            return self._bodies[url]
        except KeyError:
            raise AssertionError(f"transport asked for an unexpected url: {url!r}") from None


class BrokenTransport:
    def __call__(self, url: str) -> bytes:
        raise ConnectionError("simulated network failure")


class MemoryEvidenceCache:
    """The cache contract without storage: a failure never drops the last good rows."""

    def __init__(self) -> None:
        self.snapshots: dict[str, EvidenceSnapshot] = {}
        self.failures: list[tuple[str, str, str]] = []

    def get_snapshot(self, kind: str) -> EvidenceSnapshot | None:
        return self.snapshots.get(kind)

    def put_snapshot(self, kind: str, snapshot: EvidenceSnapshot) -> None:
        self.snapshots[kind] = snapshot

    def record_refresh_failure(
        self, kind: str, attempted_at: str, error_code: str
    ) -> None:
        self.failures.append((kind, attempted_at, error_code))
        existing = self.snapshots.get(kind) or EvidenceSnapshot(kind=kind)
        self.snapshots[kind] = replace(
            existing, last_refresh_error=error_code, attempted_at=attempted_at
        )


def _live_transport() -> FixtureTransport:
    return FixtureTransport(
        {
            PRICING_URL: _fixture_bytes("prices_openrouter_sample.json"),
            BENCHMARK_FEED_URL: _fixture_bytes(
                "benchmarks_artificial_analysis_feed.json"
            ),
        }
    )


def _source(transport) -> HttpEvidenceSource:
    return HttpEvidenceSource(transport=transport, clock=lambda: FETCH_EPOCH)


class HttpEvidenceSourceConstructionTests(unittest.TestCase):
    def test_default_feed_is_the_artificial_analysis_feed(self) -> None:
        self.assertEqual(DEFAULT_BENCHMARK_FEED, FEED_ARTIFICIAL_ANALYSIS)

    def test_a_non_callable_transport_is_rejected_at_construction(self) -> None:
        with self.assertRaises(TypeError):
            HttpEvidenceSource(transport="https://example.invalid")  # type: ignore[arg-type]

    def test_an_unknown_benchmark_feed_is_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            HttpEvidenceSource(transport=_live_transport(), benchmark_feed="no-such-feed")


class FetchedEvidenceShapeTests(unittest.TestCase):
    """What the bridge hands the engine: snapshot-level provenance, provenance-free
    bodies, and the documented URLs."""

    def setUp(self) -> None:
        self.transport = _live_transport()
        self.source = _source(self.transport)

    def test_prices_fetch_uses_the_documented_url_and_names_the_source(self) -> None:
        fetched = self.source.fetch_prices()
        self.assertEqual(self.transport.requested, [PRICING_URL])
        self.assertEqual(fetched.source_id, PRICE_SOURCE_ID)
        self.assertEqual(fetched.source_url, PRICING_URL)
        self.assertEqual(fetched.fetched_at, FETCHED_AT)

    def test_price_bodies_carry_no_provenance(self) -> None:
        """`FetchedEvidence` carries one provenance for the whole fetch; the engine
        attaches it on read and rejects a body that brought its own."""
        fetched = self.source.fetch_prices()
        self.assertTrue(fetched.records)
        for body in fetched.records:
            self.assertNotIn("provenance", body)
            self.assertIn("unit_prices", body)

    def test_benchmarks_fetch_reads_exactly_one_named_feed(self) -> None:
        fetched = self.source.fetch_benchmarks()
        self.assertEqual(self.transport.requested, [BENCHMARK_FEED_URL])
        self.assertEqual(fetched.source_id, BENCHMARK_SOURCE_ID)
        self.assertEqual(fetched.source_url, BENCHMARK_FEED_URL)

    def test_benchmark_bodies_keep_the_publisher_source_id_but_lose_provenance(self) -> None:
        """The row's own `source_id` field names the publisher that produced the score;
        the snapshot provenance names the endpoint this adapter called. They differ, and
        both survive the bridge."""
        fetched = self.source.fetch_benchmarks()
        self.assertTrue(fetched.records)
        for body in fetched.records:
            self.assertNotIn("provenance", body)
            self.assertEqual(body["source_id"], "ai.artificialanalysis")
        self.assertEqual(fetched.source_id, "ai.openrouter")

    def test_a_transport_failure_raises_instead_of_returning_a_partial_fetch(self) -> None:
        source = _source(BrokenTransport())
        with self.assertRaises(PriceTransportError):
            source.fetch_prices()
        with self.assertRaises(BenchmarkTransportError):
            source.fetch_benchmarks()


class RefreshThroughTheBridgeTests(unittest.TestCase):
    """The pair under test: bridge -> RefreshEvidenceUseCase -> cache -> query."""

    def setUp(self) -> None:
        self.repository = MemoryEvidenceCache()
        self.transport = _live_transport()
        self.refresh = RefreshEvidenceUseCase(
            source=_source(self.transport),
            repository=self.repository,
            clock=lambda: FETCHED_AT,
        )
        self.prices = QueryPricesUseCase(self.repository, clock=lambda: READ_AT)
        self.benchmarks = QueryBenchmarksUseCase(self.repository, clock=lambda: READ_AT)

    def test_price_refresh_accepts_the_adapter_bodies_and_caches_them(self) -> None:
        """A refresh that returns `refreshed=True` proves the engine validated every
        adapter body against the frozen `price_record` schema."""
        result = self.refresh.refresh(PRICES_KIND)
        self.assertTrue(result.refreshed)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.record_count, 2)

    def test_queried_price_record_keeps_the_adapter_shape_and_fresh_provenance(self) -> None:
        self.refresh.refresh(PRICES_KIND)
        answer = self.prices.query(provider_model_id="openai/gpt-5")
        self.assertEqual(len(answer.records), 1)
        record = answer.records[0]
        self.assertEqual(record.provider_model_id, "openai/gpt-5")
        self.assertEqual(record.currency, "USD")
        self.assertEqual(record.unit_prices.input_tokens, 0.000003)
        self.assertEqual(record.unit_prices.output_tokens, 0.000015)
        self.assertEqual(record.unit_prices.cached_tokens, 0.00000075)
        self.assertEqual(record.provenance.source_id, PRICE_SOURCE_ID)
        self.assertEqual(record.provenance.fetched_at, FETCHED_AT)
        self.assertFalse(record.provenance.stale)
        self.assertTrue(answer.to_wire()["cached"])

    def test_a_fetched_price_estimates_a_realistic_dollar_figure(self) -> None:
        """The whole point of the seam: the unit the adapter parses is the unit the
        engine spends. One million input tokens of a $3/M model costs about $3."""
        self.refresh.refresh(PRICES_KIND)
        record = self.prices.price_for(provider_model_id="openai/gpt-5")
        self.assertIsNotNone(record)
        self.assertAlmostEqual(
            estimate_cost(record, {"input_tokens": 1_000_000}), 3.00, places=6
        )
        self.assertAlmostEqual(
            estimate_cost(
                record, {"input_tokens": 1_000_000, "output_tokens": 200_000}
            ),
            6.00,
            places=6,
        )
        self.assertAlmostEqual(
            estimate_cost(record, {"input_tokens": 1_000, "output_tokens": 500}),
            0.0105,
            places=9,
        )

    def test_an_unpriced_model_estimates_unknown_rather_than_free(self) -> None:
        self.refresh.refresh(PRICES_KIND)
        record = self.prices.price_for(provider_model_id="some/unpriced-model")
        self.assertIsNotNone(record)
        self.assertIsNone(record.unit_prices.input_tokens)
        self.assertIsNone(estimate_cost(record, {"input_tokens": 1_000_000}))

    def test_benchmark_refresh_accepts_the_adapter_bodies_and_caches_them(self) -> None:
        result = self.refresh.refresh(BENCHMARKS_KIND)
        self.assertTrue(result.refreshed)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.record_count, 2)

    def test_queried_benchmark_rows_keep_their_scores_and_stay_unmapped(self) -> None:
        self.refresh.refresh(BENCHMARKS_KIND)
        answer = self.benchmarks.query(model_id="openai/gpt-5")
        scores = {record.metric: record.score for record in answer.benchmarks}
        self.assertEqual(scores, {"intelligence_index": 71.0, "coding_index": 65.0})
        for record in answer.benchmarks:
            self.assertEqual(record.source_model_ref, "openai/gpt-5")
            # Mapping a feed's model ref onto a registered model is an engine concern
            # the adapter deliberately leaves undone.
            self.assertIsNone(record.provider_model_id)
            self.assertEqual(record.source_id, "ai.artificialanalysis")
            self.assertEqual(record.provenance.source_id, BENCHMARK_SOURCE_ID)

    def test_a_transport_failure_keeps_the_last_good_snapshot(self) -> None:
        self.refresh.refresh(PRICES_KIND)
        broken = RefreshEvidenceUseCase(
            source=_source(BrokenTransport()),
            repository=self.repository,
            clock=lambda: READ_AT,
        )
        result = broken.refresh(PRICES_KIND)
        self.assertFalse(result.refreshed)
        self.assertEqual(result.error_code, REFRESH_FETCH_FAILED)
        answer = self.prices.query(provider_model_id="openai/gpt-5")
        self.assertEqual(len(answer.records), 1)
        self.assertEqual(answer.records[0].unit_prices.input_tokens, 0.000003)
        # The rows survive; only their provenance turns stale and names the failure.
        self.assertTrue(answer.records[0].provenance.stale)
        self.assertEqual(
            answer.records[0].provenance.last_refresh_error, REFRESH_FETCH_FAILED
        )

    def test_reading_never_fetches(self) -> None:
        """After one refresh the transport is never called again, however many reads
        happen: `prices.query.result.cached` is const true for a reason."""
        self.refresh.refresh(PRICES_KIND)
        calls_after_refresh = len(self.transport.requested)
        self.prices.query()
        self.prices.query(provider_model_id="openai/gpt-5")
        self.prices.price_for(provider_model_id="openai/gpt-5")
        self.assertEqual(len(self.transport.requested), calls_after_refresh)


class FixturesAreOfflineTests(unittest.TestCase):
    def test_price_fixture_publishes_per_token_prices(self) -> None:
        """Guards the expectations above: the fixture is OpenRouter's own per-token
        wire format, so 0.000003 in equals 0.000003 out."""
        payload = json.loads(_fixture_bytes("prices_openrouter_sample.json"))
        self.assertEqual(payload["data"][0]["pricing"]["prompt"], "0.000003")


if __name__ == "__main__":
    unittest.main()
