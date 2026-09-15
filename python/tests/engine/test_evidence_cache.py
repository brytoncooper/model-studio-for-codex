from __future__ import annotations

import unittest
from dataclasses import replace

from model_deck.engine.evidence import (
    BENCHMARKS_KIND,
    DEFAULT_EVIDENCE_MAX_AGE_SECONDS,
    MAX_CACHED_PRICE_RECORDS,
    MAX_PRICE_RECORDS,
    NEVER_FETCHED_AT,
    NEVER_REFRESHED_ERROR,
    PRICES_KIND,
    REFRESH_FETCH_FAILED,
    REFRESH_INVALID_PAYLOAD,
    REFRESH_TOO_MANY_RECORDS,
    EvidenceCacheRepository,
    EvidenceKindError,
    EvidenceResourceExhaustedError,
    EvidenceSnapshot,
    EvidenceSourceUnavailableError,
    FetchedEvidence,
    QueryBenchmarksUseCase,
    QueryPricesUseCase,
    RefreshEvidenceUseCase,
    estimate_cost,
)
from model_deck_contracts.validator import validate_schema_ref

PRICES_QUERY_RESULT_REF = "contracts/engine.v1/methods/prices.query.result.schema.json"
BENCHMARKS_QUERY_RESULT_REF = (
    "contracts/engine.v1/methods/benchmarks.query.result.schema.json"
)

FETCHED_AT = "2026-09-15T00:00:00Z"
ONE_HOUR_LATER = "2026-09-15T01:00:00Z"


def price_body(**overrides: object) -> dict:
    body: dict = {
        "provider_model_id": "openai/gpt-x",
        "currency": "USD",
        "unit_prices": {
            "input_tokens": 0.001,
            "output_tokens": 0.002,
            "cached_tokens": None,
        },
    }
    body.update(overrides)
    return body


def benchmark_body(**overrides: object) -> dict:
    body: dict = {
        "source_model_ref": "gpt-x-2026",
        "provider_model_id": "openai/gpt-x",
        "source_id": "com.example.bench",
        "feed": "arena",
        "metric": "elo",
        "score": 1337.0,
    }
    body.update(overrides)
    return body


class MemoryEvidenceCache:
    """The cache contract without storage: failures never drop the last good rows."""

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


class RecordingSource:
    def __init__(self, *, prices=(), benchmarks=(), fetched_at: str = FETCHED_AT) -> None:
        self.price_calls = 0
        self.benchmark_calls = 0
        self.failure: Exception | None = None
        self._prices = tuple(prices)
        self._benchmarks = tuple(benchmarks)
        self._fetched_at = fetched_at

    def fetch_prices(self) -> FetchedEvidence:
        self.price_calls += 1
        if self.failure is not None:
            raise self.failure
        return FetchedEvidence(
            source_id="com.example.prices",
            fetched_at=self._fetched_at,
            records=self._prices,
            source_url="https://example.com/prices",
            citation="Example price list",
            as_of="2026-09-14",
        )

    def fetch_benchmarks(self) -> FetchedEvidence:
        self.benchmark_calls += 1
        if self.failure is not None:
            raise self.failure
        return FetchedEvidence(
            source_id="com.example.bench",
            fetched_at=self._fetched_at,
            records=self._benchmarks,
        )


class PricesOnlySource:
    def fetch_prices(self) -> FetchedEvidence:
        return FetchedEvidence(source_id="com.example.prices", fetched_at=FETCHED_AT)


class EvidenceCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MemoryEvidenceCache()
        self.now = ONE_HOUR_LATER
        self.source = RecordingSource(
            prices=(price_body(),), benchmarks=(benchmark_body(),)
        )
        self.refresh = RefreshEvidenceUseCase(
            source=self.source, repository=self.repository, clock=self._clock
        )

    def _clock(self) -> str:
        return self.now

    def _prices(self, max_age_seconds: float = 7200) -> QueryPricesUseCase:
        return QueryPricesUseCase(
            self.repository, max_age_seconds=max_age_seconds, clock=self._clock
        )

    def _benchmarks(self) -> QueryBenchmarksUseCase:
        return QueryBenchmarksUseCase(
            self.repository, max_age_seconds=7200, clock=self._clock
        )

    def test_memory_cache_satisfies_the_repository_port(self) -> None:
        self.assertIsInstance(self.repository, EvidenceCacheRepository)

    def test_query_never_reaches_the_source(self) -> None:
        self.refresh.refresh(PRICES_KIND)
        self.refresh.refresh(BENCHMARKS_KIND)
        calls = (self.source.price_calls, self.source.benchmark_calls)
        for _ in range(3):
            self.assertTrue(self._prices().query().to_wire()["cached"])
            self.assertTrue(self._benchmarks().query().to_wire()["cached"])
        self.assertEqual((self.source.price_calls, self.source.benchmark_calls), calls)

    def test_empty_cache_still_reports_provenance(self) -> None:
        result = self._prices().query()
        validate_schema_ref(PRICES_QUERY_RESULT_REF, result.to_wire())
        self.assertEqual(result.records, ())
        self.assertTrue(result.snapshot.stale)
        self.assertEqual(result.snapshot.fetched_at, NEVER_FETCHED_AT)
        self.assertEqual(result.snapshot.last_refresh_error, NEVER_REFRESHED_ERROR)
        self.assertEqual(self.source.price_calls, 0)

    def test_refresh_success_replaces_the_snapshot(self) -> None:
        self.assertEqual(self.refresh.refresh(PRICES_KIND).record_count, 1)
        self.source._prices = (price_body(provider_model_id="openai/gpt-y"),)
        second = self.refresh.refresh(PRICES_KIND)
        self.assertTrue(second.refreshed)
        self.assertIsNone(second.error_code)
        result = self._prices().query()
        validate_schema_ref(PRICES_QUERY_RESULT_REF, result.to_wire())
        self.assertEqual(
            [record.provider_model_id for record in result.records], ["openai/gpt-y"]
        )
        self.assertFalse(result.snapshot.stale)
        self.assertEqual(result.snapshot.source_id, "com.example.prices")
        self.assertEqual(result.snapshot.citation, "Example price list")
        self.assertEqual(result.records[0].provenance.fetched_at, FETCHED_AT)

    def test_refresh_failure_keeps_last_good_snapshot_and_reports_the_code(self) -> None:
        self.refresh.refresh(PRICES_KIND)
        self.source.failure = RuntimeError("bearer token 12345 rejected by feed")
        failed = self.refresh.refresh(PRICES_KIND)
        self.assertFalse(failed.refreshed)
        self.assertEqual(failed.error_code, REFRESH_FETCH_FAILED)
        self.assertEqual(self.repository.failures, [(PRICES_KIND, self.now, REFRESH_FETCH_FAILED)])
        result = self._prices().query()
        validate_schema_ref(PRICES_QUERY_RESULT_REF, result.to_wire())
        self.assertEqual(len(result.records), 1)
        self.assertTrue(result.snapshot.stale)
        self.assertEqual(result.snapshot.last_refresh_error, REFRESH_FETCH_FAILED)
        self.assertEqual(result.snapshot.fetched_at, FETCHED_AT)
        self.assertNotIn("bearer token", str(result.to_wire()))

    def test_first_refresh_failure_leaves_an_empty_but_explained_cache(self) -> None:
        self.source.failure = OSError("connection reset")
        self.assertEqual(self.refresh.refresh(PRICES_KIND).error_code, REFRESH_FETCH_FAILED)
        result = self._prices().query()
        validate_schema_ref(PRICES_QUERY_RESULT_REF, result.to_wire())
        self.assertEqual(result.records, ())
        self.assertTrue(result.snapshot.stale)
        self.assertEqual(result.snapshot.last_refresh_error, REFRESH_FETCH_FAILED)

    def test_invalid_payload_is_rejected_without_losing_the_snapshot(self) -> None:
        self.refresh.refresh(PRICES_KIND)
        broken = (
            price_body(unit_prices={"input_tokens": 0.001}),
            price_body(currency="much too long a currency code"),
            price_body(unit_prices={"input_tokens": -1.0, "output_tokens": None, "cached_tokens": None}),
            {"provider_model_id": "openai/gpt-x"},
            price_body(provenance={"source_id": "com.example.spoof"}),
        )
        for body in broken:
            with self.subTest(body=body):
                self.source._prices = (body,)
                outcome = self.refresh.refresh(PRICES_KIND)
                self.assertEqual(outcome.error_code, REFRESH_INVALID_PAYLOAD)
                kept = self._prices().query()
                self.assertEqual(
                    [record.to_wire() for record in kept.records],
                    [dict(price_body(), provenance=kept.snapshot.to_wire())],
                )

    def test_a_catalog_too_large_for_one_answer_is_still_cached(self) -> None:
        """Caching and serving are bounded separately.

        A catalog bigger than one answer used to be refused outright, so nothing
        was cached at all, every price stayed unknown and no usage record could
        be estimated.
        """
        catalog_size = MAX_PRICE_RECORDS + 1
        self.source._prices = tuple(
            price_body(provider_model_id=f"openai/gpt-{index}")
            for index in range(catalog_size)
        )
        outcome = self.refresh.refresh(PRICES_KIND)
        self.assertTrue(outcome.refreshed)
        self.assertEqual(outcome.record_count, catalog_size)

        prices = self._prices()
        # Pricing one model is the path estimates depend on, and it reads the
        # whole cached catalog without ever approaching the answer bound.
        record = prices.price_for(provider_model_id="openai/gpt-7")
        self.assertIsNotNone(record)
        self.assertEqual(record.unit_prices.input_tokens, 0.001)
        self.assertEqual(len(prices.query(provider_model_id="openai/gpt-7").records), 1)

        # Returning all of them at once is what the frozen result bound refuses,
        # and the refusal names the filters that narrow the question.
        with self.assertRaises(EvidenceResourceExhaustedError):
            prices.query()

    def test_a_fetch_beyond_the_cache_bound_is_refused_rather_than_cached(self) -> None:
        self.source._prices = tuple(
            price_body(provider_model_id=f"openai/gpt-{index}")
            for index in range(MAX_CACHED_PRICE_RECORDS + 1)
        )
        outcome = self.refresh.refresh(PRICES_KIND)
        self.assertFalse(outcome.refreshed)
        self.assertEqual(outcome.error_code, REFRESH_TOO_MANY_RECORDS)
        self.assertEqual(self._prices().query().records, ())

    def test_include_stale_false_hides_stale_records_but_keeps_provenance(self) -> None:
        self.refresh.refresh(PRICES_KIND)
        self.now = "2026-09-17T00:00:00Z"
        stale = self._prices().query()
        self.assertTrue(stale.snapshot.stale)
        self.assertEqual(len(stale.records), 1)
        hidden = self._prices().query(include_stale=False)
        validate_schema_ref(PRICES_QUERY_RESULT_REF, hidden.to_wire())
        self.assertEqual(hidden.records, ())
        self.assertTrue(hidden.snapshot.stale)
        self.assertEqual(hidden.snapshot.fetched_at, FETCHED_AT)

    def test_max_age_boundary_is_inclusive(self) -> None:
        self.refresh.refresh(PRICES_KIND)
        prices = self._prices(max_age_seconds=3600)
        self.now = "2026-09-15T01:00:00Z"
        self.assertFalse(prices.query().snapshot.stale)
        self.assertEqual(len(prices.query(include_stale=False).records), 1)
        self.now = "2026-09-15T01:00:01Z"
        self.assertTrue(prices.query().snapshot.stale)
        self.assertEqual(prices.query(include_stale=False).records, ())

    def test_a_failed_refresh_is_stale_even_inside_the_freshness_window(self) -> None:
        self.refresh.refresh(PRICES_KIND)
        self.assertFalse(self._prices().query().snapshot.stale)
        self.source.failure = RuntimeError("feed down")
        self.refresh.refresh(PRICES_KIND)
        self.assertTrue(self._prices().query().snapshot.stale)

    def test_price_lookup_prefers_a_registration_scoped_price(self) -> None:
        registration_id = "550e8400-e29b-41d4-a716-446655440000"
        self.source._prices = (
            price_body(),
            price_body(registration_id=registration_id, currency="EUR"),
        )
        self.refresh.refresh(PRICES_KIND)
        prices = self._prices()
        scoped = prices.price_for(
            provider_model_id="openai/gpt-x", registration_id=registration_id
        )
        general = prices.price_for(provider_model_id="openai/gpt-x")
        self.assertEqual(scoped.currency, "EUR")
        self.assertIsNone(general.registration_id)
        self.assertIsNone(prices.price_for(provider_model_id="openai/absent"))
        self.assertIsNone(prices.price_for(provider_model_id=None))
        self.assertEqual(self.source.price_calls, 1)

    def test_price_filters_narrow_the_answer(self) -> None:
        self.source._prices = (
            price_body(),
            price_body(provider_model_id="openai/gpt-y"),
        )
        self.refresh.refresh(PRICES_KIND)
        narrowed = self._prices().query(provider_model_id="openai/gpt-y")
        self.assertEqual(
            [record.provider_model_id for record in narrowed.records], ["openai/gpt-y"]
        )

    def test_unknown_price_estimates_to_none_not_zero(self) -> None:
        self.refresh.refresh(PRICES_KIND)
        (record,) = self._prices().query().records
        self.assertEqual(estimate_cost(record, {"input_tokens": 1000.0}), 1.0)
        self.assertIsNone(estimate_cost(record, {"cached_tokens": 1000.0}))
        self.assertIsNone(estimate_cost(record, {"requests": 1.0}))
        self.assertIsNone(estimate_cost(record, {}))
        self.assertIsNone(
            estimate_cost(replace(record, currency=None), {"input_tokens": 1000.0})
        )

    def test_benchmarks_stay_visible_when_the_feed_row_is_unmapped(self) -> None:
        self.source._benchmarks = (
            benchmark_body(),
            benchmark_body(
                source_model_ref="mystery-model", provider_model_id=None, score=None
            ),
        )
        self.refresh.refresh(BENCHMARKS_KIND)
        everything = self._benchmarks().query()
        validate_schema_ref(BENCHMARKS_QUERY_RESULT_REF, everything.to_wire())
        self.assertEqual(len(everything.benchmarks), 2)
        by_source_ref = self._benchmarks().query(model_id="mystery-model")
        self.assertEqual(len(by_source_ref.benchmarks), 1)
        self.assertIsNone(by_source_ref.benchmarks[0].provider_model_id)
        self.assertIsNone(by_source_ref.benchmarks[0].score)
        by_provider = self._benchmarks().query(model_id="openai/gpt-x")
        self.assertEqual(len(by_provider.benchmarks), 1)

    def test_unknown_kind_and_missing_source_are_refused(self) -> None:
        with self.assertRaises(EvidenceKindError):
            self.refresh.refresh("allowances")
        prices_only = RefreshEvidenceUseCase(
            source=PricesOnlySource(), repository=self.repository, clock=self._clock
        )
        self.assertTrue(prices_only.refresh(PRICES_KIND).refreshed)
        with self.assertRaises(EvidenceSourceUnavailableError):
            prices_only.refresh(BENCHMARKS_KIND)

    def test_default_max_age_is_a_day(self) -> None:
        self.assertEqual(DEFAULT_EVIDENCE_MAX_AGE_SECONDS, 24 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
