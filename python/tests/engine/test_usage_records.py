from __future__ import annotations

import threading
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.storage.sqlite_usage import SqliteUsageRepository
from model_deck.engine.evidence import (
    AllowanceWindow,
    PriceRecord,
    SourceProvenance,
    SubscriptionAllowance,
    UnitPrices,
)
from model_deck.engine.usage import (
    QueryUsageResult,
    QueryUsageUseCase,
    RecordUsageUseCase,
    SummarizeUsageUseCase,
    UsageConflictError,
    UsageCostProjectionError,
    UsageEventMismatchError,
    UsageQueryValidationError,
    UsageRecord,
    UsageResourceExhaustedError,
    project_cost_kind,
)
from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

USAGE_RECORD_REF = (
    "contracts/engine.v1/vocabulary.schema.json#/definitions/usage_record"
)
USAGE_QUERY_RESULT_REF = (
    "contracts/engine.v1/methods/usage.query.result.schema.json"
)
USAGE_SUMMARY_RESULT_REF = (
    "contracts/engine.v1/methods/usage.summary.result.schema.json"
)

PRICE_PROVENANCE = SourceProvenance(
    source_id="com.example.prices",
    fetched_at="2026-09-15T00:00:00+00:00",
    stale=False,
    citation="Example price list",
)
ALLOWANCE = SubscriptionAllowance(
    provider_id="com.example.provider",
    window=AllowanceWindow(
        start="2026-09-01T00:00:00+00:00", end="2026-10-01T00:00:00+00:00"
    ),
    allowance=None,
    used=None,
    provenance=SourceProvenance(
        source_id="com.example.provider",
        fetched_at="2026-09-15T00:00:00+00:00",
        stale=False,
    ),
)


def _price(**overrides: object) -> PriceRecord:
    fields: dict = {
        "provider_model_id": "openai/gpt-x",
        "currency": "USD",
        "unit_prices": UnitPrices(
            input_tokens=0.001, output_tokens=0.002, cached_tokens=None
        ),
        "provenance": PRICE_PROVENANCE,
    }
    fields.update(overrides)
    return PriceRecord(**fields)


class StubPrices:
    """A composed price snapshot. Reads it; never fetches."""

    def __init__(self, record: PriceRecord | None = None) -> None:
        self.record = record
        self.calls: list[tuple] = []

    def price_for(self, *, provider_model_id, registration_id=None):
        self.calls.append((provider_model_id, registration_id))
        if provider_model_id is None or self.record is None:
            return None
        if provider_model_id != self.record.provider_model_id:
            return None
        return self.record


class StubAllowances:
    def __init__(self, allowance: SubscriptionAllowance | None = None) -> None:
        self.allowance = allowance
        self.calls: list[UsageRecord] = []

    def allowance_for(self, record: UsageRecord) -> SubscriptionAllowance | None:
        self.calls.append(record)
        return self.allowance


class StubUsageQuery:
    def __init__(self, *records: UsageRecord) -> None:
        self._records = tuple(records)

    def query(self, since=None, until=None) -> QueryUsageResult:
        return QueryUsageResult(records=self._records)


def _ids() -> tuple[str, str]:
    return str(uuid.uuid4()), str(uuid.uuid4())


def _usage(run_id: str, session_id: str, **overrides: object) -> dict:
    payload: dict = {
        "run_id": run_id,
        "session_id": session_id,
        "observed_at": "2026-09-12T10:00:00+00:00",
        "units": 120.0,
        "unit_kind": "input_tokens",
    }
    payload.update(overrides)
    return payload


def _event(run_id: str, session_id: str, sequence: int, **usage_overrides: object) -> dict:
    usage = _usage(run_id, session_id, **usage_overrides)
    return {
        "kind": "usage.observed",
        "run_id": run_id,
        "session_id": session_id,
        "sequence": sequence,
        "event_schema_version": 1,
        "observed_at": usage["observed_at"],
        "usage": usage,
    }


class UsageRecordsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "usage.sqlite3")

    def _stack(self) -> tuple[RecordUsageUseCase, QueryUsageUseCase]:
        repository = SqliteUsageRepository(self.db_path)
        return RecordUsageUseCase(repository), QueryUsageUseCase(repository)

    def test_record_and_query_round_trip(self) -> None:
        record_case, query_case = self._stack()
        run_id, session_id = _ids()
        result = record_case.record(_event(run_id, session_id, 3))
        self.assertFalse(result.duplicate)
        self.assertEqual(result.record.run_id, run_id)
        page = query_case.query()
        self.assertEqual(len(page.records), 1)
        self.assertEqual(page.records[0].session_id, session_id)

    def test_identical_replay_is_duplicate(self) -> None:
        record_case, query_case = self._stack()
        run_id, session_id = _ids()
        event = _event(run_id, session_id, 0)
        first = record_case.record(event)
        second = record_case.record(dict(event))
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(len(query_case.query().records), 1)

    def test_conflicting_identity_raises(self) -> None:
        record_case, _ = self._stack()
        run_id, session_id = _ids()
        record_case.record(_event(run_id, session_id, 1))
        with self.assertRaises(UsageConflictError):
            record_case.record(_event(run_id, session_id, 1, units=999.0))

    def test_run_session_mismatch_rejected(self) -> None:
        record_case, _ = self._stack()
        run_id, session_id = _ids()
        event = _event(run_id, session_id, 0)
        event["usage"] = dict(event["usage"], run_id=str(uuid.uuid4()))
        with self.assertRaises(UsageEventMismatchError):
            record_case.record(event)

    def test_non_usage_event_rejected(self) -> None:
        record_case, _ = self._stack()
        run_id, session_id = _ids()
        event = _event(run_id, session_id, 0)
        event["kind"] = "run.started"
        with self.assertRaises(UsageEventMismatchError):
            record_case.record(event)

    def test_schema_shape_violation_rejected(self) -> None:
        record_case, _ = self._stack()
        run_id, session_id = _ids()
        event = _event(run_id, session_id, 0)
        del event["usage"]
        with self.assertRaises(UsageEventMismatchError):
            record_case.record(event)

    def test_unknown_costs_preserved_exactly(self) -> None:
        record_case, query_case = self._stack()
        run_id, session_id = _ids()
        record_case.record(
            _event(
                run_id,
                session_id,
                0,
                settled_amount=None,
                currency=None,
                estimate_amount=None,
            )
        )
        (stored,) = query_case.query().records
        self.assertIsNone(stored.settled_amount)
        self.assertIsNone(stored.currency)
        self.assertIsNone(stored.estimate_amount)

    def test_settled_costs_preserved_exactly(self) -> None:
        record_case, query_case = self._stack()
        run_id, session_id = _ids()
        record_case.record(
            _event(
                run_id,
                session_id,
                0,
                settled_amount=0.0125,
                currency="USD",
                estimate_amount=0.02,
            )
        )
        (stored,) = query_case.query().records
        self.assertEqual(stored.settled_amount, 0.0125)
        self.assertEqual(stored.currency, "USD")
        self.assertEqual(stored.estimate_amount, 0.02)

    def test_reopen_persists_and_replays(self) -> None:
        record_case, _ = self._stack()
        run_id, session_id = _ids()
        event = _event(run_id, session_id, 2)
        record_case.record(event)
        reopened = RecordUsageUseCase(SqliteUsageRepository(self.db_path))
        replay = reopened.record(dict(event))
        self.assertTrue(replay.duplicate)
        with self.assertRaises(UsageConflictError):
            reopened.record(_event(run_id, session_id, 2, units=7.0))

    def test_time_offsets_filter_chronologically(self) -> None:
        record_case, query_case = self._stack()
        early_run, early_session = _ids()
        late_run, late_session = _ids()
        record_case.record(
            _event(early_run, early_session, 0, observed_at="2026-09-12T08:00:00+02:00")
        )
        record_case.record(
            _event(late_run, late_session, 0, observed_at="2026-09-12T09:00:00+02:00")
        )
        page = query_case.query(
            since="2026-09-12T06:00:00Z", until="2026-09-12T06:00:00Z"
        )
        self.assertEqual([record.run_id for record in page.records], [early_run])
        ordered = query_case.query()
        self.assertEqual(
            [record.run_id for record in ordered.records], [early_run, late_run]
        )

    def test_strict_date_validation(self) -> None:
        _, query_case = self._stack()
        with self.assertRaises(UsageQueryValidationError):
            query_case.query(since="not-a-date")
        with self.assertRaises(UsageQueryValidationError):
            query_case.query(since="2026-09-12T10:00:00")
        with self.assertRaises(UsageQueryValidationError):
            query_case.query(
                since="2026-09-13T00:00:00Z", until="2026-09-12T00:00:00Z"
            )

    def test_query_result_matches_frozen_schema(self) -> None:
        record_case, query_case = self._stack()
        run_id, session_id = _ids()
        record_case.record(_event(run_id, session_id, 0))
        page = query_case.query()
        validate_schema_ref(USAGE_QUERY_RESULT_REF, page.to_wire())
        self.assertEqual(page.to_wire(), {"records": [_usage(run_id, session_id)]})
        validate_schema_ref(
            USAGE_RECORD_REF,
            {
                "run_id": run_id,
                "session_id": session_id,
                "observed_at": "2026-09-12T10:00:00+00:00",
                "units": 120.0,
                "unit_kind": "input_tokens",
            },
        )

    def test_limit_exceeded_is_resource_exhausted(self) -> None:
        repository = SqliteUsageRepository(self.db_path)
        record_case = RecordUsageUseCase(repository)
        for index in range(1001):
            run_id, session_id = _ids()
            record_case.record(_event(run_id, session_id, 0))
        with self.assertRaises(UsageResourceExhaustedError):
            QueryUsageUseCase(repository).query()

    def test_concurrent_identical_insert_proves_single_row(self) -> None:
        record_case, query_case = self._stack()
        run_id, session_id = _ids()
        outcomes: list = []

        def attempt() -> None:
            try:
                outcomes.append(record_case.record(_event(run_id, session_id, 5)).duplicate)
            except UsageConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertNotIn("conflict", outcomes)
        self.assertEqual(outcomes.count(False), 1)
        self.assertEqual(outcomes.count(True), 7)
        self.assertEqual(len(query_case.query().records), 1)

    def test_optional_field_presence_survives_full_wire_round_trip(self):
        record_case, query_case = self._stack()
        run_id, session_id = _ids()
        optional = dict(registration_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()),
                        provider_model_id="provider/model", settled_amount=None,
                        currency=None, estimate_amount=0.25)
        record_case.record(_event(run_id, session_id, 0, **optional))
        wire = query_case.query().to_wire()
        validate_schema_ref(USAGE_QUERY_RESULT_REF, wire)
        self.assertEqual(wire, {"records": [_usage(run_id, session_id, **optional)]})
        reopened = QueryUsageUseCase(SqliteUsageRepository(self.db_path))
        self.assertEqual(reopened.query().to_wire(), wire)

    def test_absent_and_explicit_null_are_conflicting_replays(self):
        for field in ("settled_amount", "currency", "estimate_amount"):
            for first_has_null in (False, True):
                with self.subTest(field=field, first_has_null=first_has_null):
                    record_case, query_case = self._stack()
                    run_id, session_id = _ids()
                    absent = _event(run_id, session_id, 1)
                    explicit_null = _event(run_id, session_id, 1, **{field: None})
                    first, changed = (explicit_null, absent) if first_has_null else (absent, explicit_null)
                    record_case.record(first)
                    with self.assertRaises(UsageConflictError):
                        record_case.record(changed)
                    self.assertTrue(record_case.record(first).duplicate)
                    stored = next(r for r in query_case.query().records if r.run_id == run_id)
                    self.assertEqual(stored.to_wire(), first["usage"])

    def test_fractional_precision_and_equal_instant_offsets(self):
        record_case, query_case = self._stack()
        run_id, session_id = _ids()
        stamp = "2026-09-12T12:00:00.0000009000+02:00"
        record_case.record(_event(run_id, session_id, 0, observed_at=stamp))
        self.assertEqual(query_case.query(until="2026-09-12T10:00:00.0000001Z").records, ())
        self.assertEqual(query_case.query(since="2026-09-12T10:00:00.000001Z").records, ())
        same = query_case.query(since="2026-09-12T10:00:00.0000009Z",
                                until="2026-09-12T09:00:00.000000900-01:00")
        self.assertEqual([r.observed_at for r in same.records], [stamp])
        with self.assertRaises(UsageQueryValidationError):
            query_case.query(since="2026-09-12T10:00:00.0000009Z",
                             until="2026-09-12T10:00:00.0000001Z")

    def test_full_timestamp_precision_and_calendar_edges(self):
        record_case, query_case = self._stack()
        stamps = ("0001-01-01T00:00:00.1+23:59", "9999-12-31T23:59:59.9-23:59",
                  "2026-09-12T10:00:00.123456789123456789Z")
        for stamp in stamps:
            run_id, session_id = _ids()
            record_case.record(_event(run_id, session_id, 0, observed_at=stamp))
            self.assertEqual([r.observed_at for r in query_case.query(stamp, stamp).records], [stamp])

    def test_payload_time_controls_order_and_replay_not_envelope_receipt_time(self):
        record_case, query_case = self._stack()
        run_id, session_id = _ids()
        event = _event(run_id, session_id, 0)
        event["observed_at"] = "2026-09-13T00:00:00Z"
        record_case.record(event)
        event["observed_at"] = "2026-09-14T00:00:00Z"
        self.assertTrue(record_case.record(event).duplicate)
        self.assertEqual(len(query_case.query(until="2026-09-12T10:00:00Z").records), 1)

    def test_query_interleaved_commit_cannot_exceed_frozen_limit(self):
        repository = SqliteUsageRepository(self.db_path)
        writer = RecordUsageUseCase(SqliteUsageRepository(self.db_path))
        run_id, session_id = _ids()
        for sequence in range(1000):
            writer.record(_event(run_id, session_id, sequence))
        original_connect = repository._connect
        inserted = []
        def traced_connection():
            connection = original_connect()
            def before_select(sql):
                if sql.startswith("SELECT record_json FROM usage_records") and not inserted:
                    inserted.append(True)
                    writer.record(_event(run_id, session_id, 1000))
            connection.set_trace_callback(before_select)
            return connection
        repository._connect = traced_connection
        with self.assertRaises(UsageResourceExhaustedError):
            QueryUsageUseCase(repository).query()
        self.assertEqual(inserted, [True])


class UsageCostKindTest(unittest.TestCase):
    """Estimated, provider-settled and subscription-allowance never mix."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "usage.sqlite3")
        self.repository = SqliteUsageRepository(self.db_path)
        self.record_case = RecordUsageUseCase(self.repository)

    def _query(self, **ports) -> QueryUsageUseCase:
        return QueryUsageUseCase(self.repository, **ports)

    def test_settled_amount_is_labelled_provider_settled(self) -> None:
        run_id, session_id = _ids()
        self.record_case.record(
            _event(run_id, session_id, 0, settled_amount=0.5, currency="USD")
        )
        (stored,) = self._query().query().records
        self.assertEqual(stored.cost_kind, "provider_settled")
        self.assertEqual(stored.settled_amount, 0.5)
        self.assertIsNone(stored.estimate_amount)
        validate_schema_ref(USAGE_RECORD_REF, stored.to_wire())

    def test_estimate_is_computed_on_read_and_never_written_into_settled(self) -> None:
        run_id, session_id = _ids()
        self.record_case.record(
            _event(run_id, session_id, 0, provider_model_id="openai/gpt-x")
        )
        prices = StubPrices(_price())
        page = self._query(prices=prices).query()
        validate_schema_ref(USAGE_QUERY_RESULT_REF, page.to_wire())
        (stored,) = page.records
        self.assertEqual(stored.cost_kind, "estimated")
        self.assertEqual(stored.estimate_amount, 0.12)
        self.assertEqual(stored.currency, "USD")
        self.assertIsNone(stored.settled_amount)
        self.assertEqual(stored.provenance, PRICE_PROVENANCE)
        self.assertEqual(prices.calls, [("openai/gpt-x", None)])
        # The stored row is untouched: the estimate exists only on the read.
        self.assertNotIn(
            "estimate_amount", self.repository.query(None, None).records[0].to_wire()
        )

    def test_a_settled_record_is_never_re_estimated_or_relabelled(self) -> None:
        run_id, session_id = _ids()
        self.record_case.record(
            _event(
                run_id,
                session_id,
                0,
                provider_model_id="openai/gpt-x",
                settled_amount=0.5,
                currency="USD",
            )
        )
        (stored,) = self._query(prices=StubPrices(_price())).query().records
        self.assertEqual(stored.cost_kind, "provider_settled")
        self.assertEqual(stored.settled_amount, 0.5)
        self.assertIsNone(stored.estimate_amount)

    def test_unknown_prices_leave_the_record_unlabelled(self) -> None:
        cases = {
            "unpriced unit kind": dict(unit_kind="requests", units=3.0),
            "null unit price": dict(unit_kind="cached_tokens"),
            "unknown provider model": dict(provider_model_id="openai/absent"),
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                run_id, session_id = _ids()
                self.record_case.record(
                    _event(
                        run_id,
                        session_id,
                        0,
                        **{"provider_model_id": "openai/gpt-x", **overrides},
                    )
                )
                page = self._query(prices=StubPrices(_price())).query()
                stored = next(
                    record for record in page.records if record.run_id == run_id
                )
                self.assertIsNone(stored.cost_kind)
                self.assertIsNone(stored.estimate_amount)

    def test_a_currency_the_price_does_not_share_is_not_estimated(self) -> None:
        run_id, session_id = _ids()
        self.record_case.record(
            _event(
                run_id,
                session_id,
                0,
                provider_model_id="openai/gpt-x",
                currency="EUR",
            )
        )
        (stored,) = self._query(prices=StubPrices(_price())).query().records
        self.assertIsNone(stored.cost_kind)
        self.assertEqual(stored.currency, "EUR")

    def test_an_observed_estimate_is_labelled_but_never_recomputed(self) -> None:
        run_id, session_id = _ids()
        self.record_case.record(
            _event(
                run_id,
                session_id,
                0,
                provider_model_id="openai/gpt-x",
                estimate_amount=9.99,
            )
        )
        (stored,) = self._query(prices=StubPrices(_price())).query().records
        self.assertEqual(stored.cost_kind, "estimated")
        self.assertEqual(stored.estimate_amount, 9.99)
        self.assertIsNone(stored.provenance)

    def test_without_composed_evidence_records_keep_their_exact_wire_shape(self) -> None:
        run_id, session_id = _ids()
        observed = _usage(
            run_id, session_id, provider_model_id="openai/gpt-x", estimate_amount=0.25
        )
        self.record_case.record(
            _event(
                run_id,
                session_id,
                0,
                provider_model_id="openai/gpt-x",
                estimate_amount=0.25,
            )
        )
        page = self._query().query()
        self.assertEqual(page.to_wire(), {"records": [observed]})
        self.assertIsNone(page.records[0].cost_kind)

    def test_an_estimate_in_settled_amount_is_refused_by_code_and_by_schema(self) -> None:
        run_id, session_id = _ids()
        contradiction = _usage(
            run_id, session_id, cost_kind="estimated", settled_amount=0.5, currency="USD"
        )
        with self.assertRaises(UsageCostProjectionError):
            project_cost_kind(UsageRecord.from_wire(contradiction))
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(USAGE_RECORD_REF, contradiction)

    def test_an_allowance_source_labels_without_inventing_money(self) -> None:
        run_id, session_id = _ids()
        self.record_case.record(
            _event(run_id, session_id, 0, provider_model_id="openai/gpt-x")
        )
        allowances = StubAllowances(ALLOWANCE)
        (stored,) = (
            self._query(prices=StubPrices(_price()), subscription_allowance=allowances)
            .query()
            .records
        )
        self.assertEqual(stored.cost_kind, "subscription_allowance")
        self.assertIsNone(stored.settled_amount)
        self.assertIsNone(stored.estimate_amount)
        self.assertEqual(stored.provenance, ALLOWANCE.provenance)
        validate_schema_ref(USAGE_RECORD_REF, stored.to_wire())
        self.assertEqual(len(allowances.calls), 1)

    def test_an_uncomposed_allowance_is_unknown_not_zero(self) -> None:
        run_id, session_id = _ids()
        self.record_case.record(_event(run_id, session_id, 0))
        (without_source,) = self._query().query().records
        (with_silent_source,) = (
            self._query(subscription_allowance=StubAllowances(None)).query().records
        )
        self.assertIsNone(without_source.cost_kind)
        self.assertIsNone(with_silent_source.cost_kind)

    def test_summary_keeps_the_three_kinds_apart(self) -> None:
        settled_run, settled_session = _ids()
        allowance_run, allowance_session = _ids()
        estimated_run, estimated_session = _ids()
        self.record_case.record(
            _event(
                settled_run,
                settled_session,
                0,
                settled_amount=0.5,
                currency="USD",
                units=10.0,
            )
        )
        self.record_case.record(
            _event(
                estimated_run,
                estimated_session,
                0,
                provider_model_id="openai/gpt-x",
                units=1000.0,
            )
        )
        self.record_case.record(
            _event(
                allowance_run,
                allowance_session,
                0,
                provider_model_id="openai/gpt-y",
                units=7.0,
            )
        )

        class OnlyGptY(StubAllowances):
            def allowance_for(self, record):
                if record.provider_model_id == "openai/gpt-y":
                    return ALLOWANCE
                return None

        summary = SummarizeUsageUseCase(
            self._query(prices=StubPrices(_price()), subscription_allowance=OnlyGptY())
        ).summarize()
        wire = summary.to_wire()
        validate_schema_ref(USAGE_SUMMARY_RESULT_REF, wire)
        totals = {total["cost_kind"]: total for total in wire["totals"]}
        self.assertEqual(len(wire["totals"]), 3)
        self.assertEqual(
            totals["provider_settled"],
            {
                "cost_kind": "provider_settled",
                "amount": 0.5,
                "currency": "USD",
                "units": 10.0,
                "record_count": 1,
            },
        )
        self.assertEqual(totals["estimated"]["amount"], 1.0)
        self.assertEqual(totals["estimated"]["currency"], "USD")
        self.assertIsNone(totals["subscription_allowance"]["amount"])
        self.assertIsNone(totals["subscription_allowance"]["currency"])
        self.assertEqual(totals["subscription_allowance"]["units"], 7.0)

    def test_summary_reports_unknown_and_mixed_currency_totals_as_null(self) -> None:
        run_id, session_id = _ids()
        settled = UsageRecord.from_wire(
            _usage(run_id, session_id, settled_amount=0.5, currency="USD")
        )
        # A provider-reported row whose amount has not settled yet keeps its
        # kind and makes the total unknown rather than a partial sum.
        unknown = UsageRecord.from_wire(
            _usage(
                run_id,
                session_id,
                cost_kind="provider_settled",
                settled_amount=None,
                currency="USD",
            )
        )
        euro = UsageRecord.from_wire(
            _usage(run_id, session_id, settled_amount=0.25, currency="EUR")
        )
        no_currency = UsageRecord.from_wire(
            _usage(run_id, session_id, settled_amount=0.25, currency=None)
        )
        labelled = [project_cost_kind(record) for record in (settled, unknown)]
        self.assertEqual({record.cost_kind for record in labelled}, {"provider_settled"})
        for name, group in {
            "unknown amount": (settled, unknown),
            "mixed currency": (settled, euro),
            "no currency": (settled, no_currency),
        }.items():
            with self.subTest(name=name):
                summary = SummarizeUsageUseCase(
                    StubUsageQuery(*(project_cost_kind(row) for row in group))
                ).summarize()
                validate_schema_ref(USAGE_SUMMARY_RESULT_REF, summary.to_wire())
                (total,) = summary.totals
                self.assertIsNone(total.amount)
                self.assertIsNone(total.currency)
                self.assertEqual(total.record_count, 2)

    def test_summary_leaves_unlabelled_records_to_usage_query(self) -> None:
        run_id, session_id = _ids()
        plain = UsageRecord.from_wire(_usage(run_id, session_id))
        summary = SummarizeUsageUseCase(StubUsageQuery(plain)).summarize()
        self.assertEqual(summary.to_wire(), {"totals": []})
        validate_schema_ref(USAGE_SUMMARY_RESULT_REF, summary.to_wire())

    def test_summary_validates_its_window(self) -> None:
        summarize = SummarizeUsageUseCase(self._query()).summarize
        with self.assertRaises(UsageQueryValidationError):
            summarize(since="not-a-date")
        with self.assertRaises(UsageQueryValidationError):
            summarize(since="2026-09-13T00:00:00Z", until="2026-09-12T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
