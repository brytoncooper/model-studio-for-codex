from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.storage.sqlite_evidence import SqliteEvidenceCacheRepository
from model_deck.engine.evidence import (
    BENCHMARKS_KIND,
    PRICES_KIND,
    REFRESH_FETCH_FAILED,
    EvidenceCacheRepository,
    EvidenceKindError,
    EvidenceSnapshot,
    FetchedEvidence,
    QueryPricesUseCase,
    RefreshEvidenceUseCase,
)

FETCHED_AT = "2026-09-15T00:00:00Z"
LATER = "2026-09-15T06:00:00Z"
ATTEMPTED_AT = "2026-09-15T07:00:00Z"


def price_body(provider_model_id: str = "openai/gpt-x") -> dict:
    return {
        "provider_model_id": provider_model_id,
        "currency": "USD",
        "unit_prices": {
            "input_tokens": 0.001,
            "output_tokens": 0.002,
            "cached_tokens": None,
        },
    }


def snapshot(
    *, source_id: str = "com.example.prices", fetched_at: str = FETCHED_AT, records=(price_body(),)
) -> EvidenceSnapshot:
    return EvidenceSnapshot(
        kind=PRICES_KIND,
        source_id=source_id,
        fetched_at=fetched_at,
        records=tuple(records),
        source_url="https://example.com/prices",
        citation="Example price list",
        as_of="2026-09-14",
    )


class FailingSource:
    def fetch_prices(self) -> FetchedEvidence:
        raise RuntimeError("feed unreachable")

    def fetch_benchmarks(self) -> FetchedEvidence:
        raise RuntimeError("feed unreachable")


class SqliteEvidenceCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "evidence.sqlite3")
        self.repository = SqliteEvidenceCacheRepository(self.db_path)

    def test_satisfies_the_repository_port(self) -> None:
        self.assertIsInstance(self.repository, EvidenceCacheRepository)

    def test_unknown_kind_is_refused_everywhere(self) -> None:
        with self.assertRaises(EvidenceKindError):
            self.repository.get_snapshot("allowances")
        with self.assertRaises(EvidenceKindError):
            self.repository.put_snapshot("allowances", snapshot())
        with self.assertRaises(EvidenceKindError):
            self.repository.record_refresh_failure(
                "allowances", ATTEMPTED_AT, REFRESH_FETCH_FAILED
            )

    def test_untouched_kind_has_no_snapshot(self) -> None:
        self.assertIsNone(self.repository.get_snapshot(PRICES_KIND))
        self.assertIsNone(self.repository.get_snapshot(BENCHMARKS_KIND))

    def test_round_trip_survives_reopen(self) -> None:
        self.repository.put_snapshot(PRICES_KIND, snapshot())
        reopened = SqliteEvidenceCacheRepository(self.db_path)
        stored = reopened.get_snapshot(PRICES_KIND)
        self.assertEqual(stored.kind, PRICES_KIND)
        self.assertEqual(stored.source_id, "com.example.prices")
        self.assertEqual(stored.fetched_at, FETCHED_AT)
        self.assertEqual(stored.source_url, "https://example.com/prices")
        self.assertEqual(stored.citation, "Example price list")
        self.assertEqual(stored.as_of, "2026-09-14")
        self.assertEqual(stored.records, (price_body(),))
        self.assertIsNone(stored.last_refresh_error)
        self.assertIsNone(stored.attempted_at)

    def test_put_replaces_the_whole_snapshot_and_clears_the_last_failure(self) -> None:
        self.repository.put_snapshot(PRICES_KIND, snapshot())
        self.repository.record_refresh_failure(
            PRICES_KIND, ATTEMPTED_AT, REFRESH_FETCH_FAILED
        )
        self.repository.put_snapshot(
            PRICES_KIND,
            snapshot(
                source_id="com.example.other",
                fetched_at=LATER,
                records=(price_body("openai/gpt-y"), price_body("openai/gpt-z")),
            ),
        )
        stored = self.repository.get_snapshot(PRICES_KIND)
        self.assertEqual(stored.source_id, "com.example.other")
        self.assertEqual(stored.fetched_at, LATER)
        self.assertEqual(
            [body["provider_model_id"] for body in stored.records],
            ["openai/gpt-y", "openai/gpt-z"],
        )
        self.assertIsNone(stored.last_refresh_error)
        self.assertIsNone(stored.attempted_at)

    def test_failure_keeps_the_last_good_snapshot(self) -> None:
        self.repository.put_snapshot(PRICES_KIND, snapshot())
        self.repository.record_refresh_failure(
            PRICES_KIND, ATTEMPTED_AT, REFRESH_FETCH_FAILED
        )
        stored = SqliteEvidenceCacheRepository(self.db_path).get_snapshot(PRICES_KIND)
        self.assertEqual(stored.records, (price_body(),))
        self.assertEqual(stored.fetched_at, FETCHED_AT)
        self.assertEqual(stored.last_refresh_error, REFRESH_FETCH_FAILED)
        self.assertEqual(stored.attempted_at, ATTEMPTED_AT)

    def test_failure_before_any_snapshot_records_only_the_failure(self) -> None:
        self.repository.record_refresh_failure(
            PRICES_KIND, ATTEMPTED_AT, REFRESH_FETCH_FAILED
        )
        stored = self.repository.get_snapshot(PRICES_KIND)
        self.assertIsNone(stored.fetched_at)
        self.assertIsNone(stored.source_id)
        self.assertEqual(stored.records, ())
        self.assertEqual(stored.last_refresh_error, REFRESH_FETCH_FAILED)

    def test_kinds_do_not_disturb_each_other(self) -> None:
        self.repository.put_snapshot(PRICES_KIND, snapshot())
        self.repository.record_refresh_failure(
            BENCHMARKS_KIND, ATTEMPTED_AT, REFRESH_FETCH_FAILED
        )
        prices = self.repository.get_snapshot(PRICES_KIND)
        benchmarks = self.repository.get_snapshot(BENCHMARKS_KIND)
        self.assertIsNone(prices.last_refresh_error)
        self.assertEqual(prices.records, (price_body(),))
        self.assertIsNone(benchmarks.fetched_at)
        self.assertEqual(benchmarks.last_refresh_error, REFRESH_FETCH_FAILED)

    def test_incomplete_or_mismatched_snapshots_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.repository.put_snapshot(
                PRICES_KIND, EvidenceSnapshot(kind=BENCHMARKS_KIND)
            )
        with self.assertRaises(ValueError):
            self.repository.put_snapshot(PRICES_KIND, EvidenceSnapshot(kind=PRICES_KIND))
        with self.assertRaises(ValueError):
            self.repository.put_snapshot(
                PRICES_KIND,
                EvidenceSnapshot(kind=PRICES_KIND, source_id="com.example.prices"),
            )
        self.assertIsNone(self.repository.get_snapshot(PRICES_KIND))

    def test_only_fixed_payload_free_error_codes_are_stored(self) -> None:
        with self.assertRaises(ValueError):
            self.repository.record_refresh_failure(
                PRICES_KIND, ATTEMPTED_AT, "HTTP 401: bearer token 12345 rejected"
            )
        with self.assertRaises(ValueError):
            self.repository.record_refresh_failure(
                PRICES_KIND, "", REFRESH_FETCH_FAILED
            )
        self.assertIsNone(self.repository.get_snapshot(PRICES_KIND))

    def test_replacement_is_atomic_for_concurrent_readers(self) -> None:
        first = snapshot()
        second = snapshot(
            source_id="com.example.other",
            fetched_at=LATER,
            records=(price_body("openai/gpt-y"), price_body("openai/gpt-z")),
        )
        self.repository.put_snapshot(PRICES_KIND, first)
        seen: list[tuple] = []
        stop = threading.Event()

        def write() -> None:
            for index in range(50):
                self.repository.put_snapshot(
                    PRICES_KIND, second if index % 2 else first
                )
            stop.set()

        def read() -> None:
            reader = SqliteEvidenceCacheRepository(self.db_path)
            while not stop.is_set() and len(seen) < 1000:
                stored = reader.get_snapshot(PRICES_KIND)
                seen.append((stored.source_id, stored.fetched_at, len(stored.records)))

        threads = [threading.Thread(target=write)] + [
            threading.Thread(target=read) for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(seen)
        self.assertEqual(
            set(seen) - {
                ("com.example.prices", FETCHED_AT, 1),
                ("com.example.other", LATER, 2),
            },
            set(),
        )

    def test_failed_refresh_over_real_storage_serves_the_stale_last_good_value(self) -> None:
        self.repository.put_snapshot(PRICES_KIND, snapshot())
        refresh = RefreshEvidenceUseCase(
            source=FailingSource(),
            repository=self.repository,
            clock=lambda: ATTEMPTED_AT,
        )
        outcome = refresh.refresh(PRICES_KIND)
        self.assertEqual(outcome.error_code, REFRESH_FETCH_FAILED)
        prices = QueryPricesUseCase(
            SqliteEvidenceCacheRepository(self.db_path),
            max_age_seconds=86400,
            clock=lambda: "2026-09-15T01:00:00Z",
        )
        result = prices.query()
        self.assertEqual(len(result.records), 1)
        self.assertTrue(result.snapshot.stale)
        self.assertEqual(result.snapshot.last_refresh_error, REFRESH_FETCH_FAILED)
        self.assertEqual(
            prices.price_for(provider_model_id="openai/gpt-x").unit_prices.input_tokens,
            0.001,
        )


if __name__ == "__main__":
    unittest.main()
