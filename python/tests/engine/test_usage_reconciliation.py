from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from model_deck.adapters.storage.sqlite_usage import SqliteUsageRepository
from model_deck.engine.evidence import PriceRecord, SourceProvenance, UnitPrices
from model_deck.engine.runs.usage_events import (
    CommittedUsageCursor, CommittedUsageEvent, CommittedUsagePage,
)
from model_deck.engine.usage import (
    QueryUsageUseCase, RecordUsageUseCase, SummarizeUsageUseCase, UsageConflictError,
    UsageEventMismatchError,
)
from model_deck.engine.usage.reconciliation import ReconciledUsageQueryUseCase, UsageReconciliationError

PRICE = PriceRecord(
    provider_model_id="openai/gpt-x", currency="USD",
    unit_prices=UnitPrices(input_tokens=0.001, output_tokens=0.002, cached_tokens=None),
    provenance=SourceProvenance(source_id="com.example.prices",
                                fetched_at="2026-09-15T00:00:00+00:00", stale=True,
                                last_refresh_error="fetch_failed"))


class StubPrices:
    """A cached price snapshot. The read path never fetches."""

    def __init__(self, record=PRICE):
        self.record = record
        self.calls = []

    def price_for(self, *, provider_model_id, registration_id=None):
        self.calls.append((provider_model_id, registration_id))
        if provider_model_id != self.record.provider_model_id:
            return None
        return self.record

RUN_ID = "550e8400-e29b-41d4-a716-446655440004"
SESSION_ID = "550e8400-e29b-41d4-a716-446655440003"


def event(sequence, **fields):
    usage = {"run_id": RUN_ID, "session_id": SESSION_ID, "observed_at": "2026-09-12T10:00:00Z",
             "units": sequence, "unit_kind": "input_tokens", **fields}
    return CommittedUsageEvent(run_id=RUN_ID, session_id=SESSION_ID, sequence=sequence,
        event_schema_version=1, observed_at="2026-09-12T11:00:00Z", payload_json=json.dumps({"usage": usage}))


class SnapshotReader:
    def __init__(self, events):
        self.events = list(events)
        self.calls = []
        self.snapshot = ()

    def read_committed_usage_events(self, *, cursor=None, limit=256):
        self.calls.append((cursor, limit))
        if cursor is None:
            self.snapshot = tuple(self.events)
            offset = 0
        else:
            offset = int(cursor.token)
        page = self.snapshot[offset:offset + limit]
        end = offset + len(page)
        continuation = CommittedUsageCursor(str(end)) if end < len(self.snapshot) else None
        return CommittedUsagePage(page, len(self.snapshot), continuation)


class UsageReconciliationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "usage.sqlite3"
        self.repository = SqliteUsageRepository(self.path)
        self.query_case = QueryUsageUseCase(self.repository)

    def reconciler(self, reader, recorder=None, **options):
        return ReconciledUsageQueryUseCase(reader=reader,
            record_usage=recorder or RecordUsageUseCase(self.repository), query_usage=self.query_case, **options)

    def test_multiple_pages_reconcile_original_fields_before_query(self):
        events = [event(1), event(2, settled_amount=None, currency=None), event(3, estimate_amount=0.2)]
        reader = SnapshotReader(events)
        recorder = RecordUsageUseCase(self.repository)
        with mock.patch.object(recorder, "record", wraps=recorder.record) as record:
            result = self.reconciler(reader, recorder, page_size=1).query()
        self.assertEqual([row.to_wire() for row in result.records], [item.payload["usage"] for item in events])
        self.assertEqual([call[1] for call in reader.calls], [1, 1, 1])
        self.assertIsNone(reader.calls[0][0])
        first = record.call_args_list[0].args[0]
        self.assertEqual(first, {"kind": "usage.observed", "run_id": RUN_ID, "session_id": SESSION_ID,
            "sequence": 1, "event_schema_version": 1, "observed_at": events[0].observed_at,
            "usage": events[0].payload["usage"]})
        self.assertNotIn("settled_amount", result.records[0].to_wire())
        self.assertIsNone(result.records[1].to_wire()["settled_amount"])

    def test_later_insert_waits_for_fresh_scan_and_replay_does_not_duplicate(self):
        reader = SnapshotReader([event(1), event(2)])
        recorder = RecordUsageUseCase(self.repository)
        original = recorder.record

        def insert_later(value):
            if value["sequence"] == 1 and len(reader.events) == 2:
                reader.events.append(event(3))
            return original(value)

        with mock.patch.object(recorder, "record", side_effect=insert_later):
            reconciler = self.reconciler(reader, recorder, page_size=1)
            self.assertEqual(len(reconciler.query().records), 2)
            self.assertEqual(len(reconciler.query().records), 3)
            self.assertEqual(len(reconciler.query().records), 3)
        self.assertEqual(sum(cursor is None for cursor, _ in reader.calls), 3)

    def test_failed_record_returns_no_query_then_reopen_recovers_without_duplicate(self):
        reader = SnapshotReader([event(1), event(2)])
        recorder = RecordUsageUseCase(self.repository)
        original = recorder.record

        def fail_second(value):
            if value["sequence"] == 2:
                raise OSError("private storage detail")
            return original(value)

        with mock.patch.object(recorder, "record", side_effect=fail_second), \
                mock.patch.object(self.query_case, "query", wraps=self.query_case.query) as query:
            with self.assertRaises(UsageReconciliationError) as caught:
                self.reconciler(reader, recorder, page_size=1).query()
            query.assert_not_called()
        self.assertNotIn("private storage detail", str(caught.exception))
        self.assertEqual(len(self.query_case.query().records), 1)
        reopened = SqliteUsageRepository(self.path)
        recovered = ReconciledUsageQueryUseCase(reader=SnapshotReader(reader.events),
            record_usage=RecordUsageUseCase(reopened), query_usage=QueryUsageUseCase(reopened), page_size=1)
        self.assertEqual(len(recovered.query().records), 2)

    def test_conflicting_replay_preserves_error_and_returns_no_result(self):
        reader = SnapshotReader([event(1)])
        reconciler = self.reconciler(reader)
        reconciler.query()
        reader.events = [event(1, units=999)]
        with mock.patch.object(self.query_case, "query") as query:
            with self.assertRaises(UsageConflictError):
                reconciler.query()
            query.assert_not_called()

    def test_malformed_or_mixed_payload_is_not_silently_trimmed(self):
        original = event(1)
        for payload in ({**original.payload, "run_id": "spoofed"}, None, {"other": 1},
                        {"usage": {**original.payload["usage"], "session_id": RUN_ID}}):
            with self.subTest(payload=payload), mock.patch.object(self.query_case, "query") as query:
                with self.assertRaises(UsageEventMismatchError):
                    self.reconciler(SnapshotReader([replace(original, payload_json=json.dumps(payload))])).query()
                query.assert_not_called()

    def test_reader_failure_has_safe_typed_error_and_no_query(self):
        reader = mock.Mock()
        reader.read_committed_usage_events.side_effect = RuntimeError("private reader detail")
        with mock.patch.object(self.query_case, "query") as query:
            with self.assertRaises(UsageReconciliationError) as caught:
                self.reconciler(reader).query()
            query.assert_not_called()
        self.assertNotIn("private reader detail", str(caught.exception))

    def test_highwater_change_and_cursor_cycles_reject(self):
        cursor_a, cursor_b = CommittedUsageCursor("a"), CommittedUsageCursor("b")
        cases = [
            [CommittedUsagePage((), 2, cursor_a), CommittedUsagePage((), 3, None)],
            [CommittedUsagePage((), None, None)],
            [CommittedUsagePage((), 2, cursor_a)] * 4,
            [CommittedUsagePage((), 2, cursor_a), CommittedUsagePage((), 2, cursor_b)] * 4,
        ]
        for pages in cases:
            with self.subTest(pages=pages), mock.patch.object(self.query_case, "query") as query:
                reader = mock.Mock()
                reader.read_committed_usage_events.side_effect = pages
                with self.assertRaises(UsageReconciliationError):
                    self.reconciler(reader).query()
                query.assert_not_called()
                self.assertLessEqual(reader.read_committed_usage_events.call_count, len(pages))

    def test_empty_snapshot_queries_and_storage_failure_is_sanitized(self):
        reader = SnapshotReader([])
        self.assertEqual(self.reconciler(reader).query().records, ())
        self.assertEqual(reader.calls, [(None, 256)])
        with mock.patch.object(self.query_case, "query", side_effect=OSError("private sqlite path")):
            with self.assertRaises(UsageReconciliationError) as caught:
                self.reconciler(reader).query()
        self.assertNotIn("private sqlite path", str(caught.exception))

    def test_page_size_requires_bounded_positive_integer(self):
        for size in (True, 0, -1, 257, "1"):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.reconciler(SnapshotReader([]), page_size=size)

    def test_reconciled_records_are_labelled_without_rewriting_the_ledger(self):
        events = [event(1, settled_amount=0.5, currency="USD"),
                  event(2, provider_model_id="openai/gpt-x")]
        prices = StubPrices()
        labelling_query = QueryUsageUseCase(self.repository, prices=prices)
        result = ReconciledUsageQueryUseCase(reader=SnapshotReader(events),
            record_usage=RecordUsageUseCase(self.repository), query_usage=labelling_query).query()
        settled, estimated = result.records
        self.assertEqual(settled.cost_kind, "provider_settled")
        self.assertEqual(settled.settled_amount, 0.5)
        self.assertIsNone(settled.estimate_amount)
        self.assertEqual(estimated.cost_kind, "estimated")
        self.assertEqual(estimated.estimate_amount, 0.002)
        self.assertIsNone(estimated.settled_amount)
        self.assertEqual(estimated.provenance, PRICE.provenance)
        self.assertTrue(estimated.provenance.stale)
        # A settled record is never priced, and the committed rows never change.
        self.assertEqual(prices.calls, [("openai/gpt-x", None)])
        self.assertEqual([row.to_wire() for row in self.repository.query(None, None).records],
                         [item.payload["usage"] for item in events])

    def test_an_estimate_is_never_promoted_into_settled_money(self):
        events = [event(1, settled_amount=0.5, currency="USD", provider_model_id="openai/gpt-x")]
        query = QueryUsageUseCase(self.repository, prices=StubPrices())
        result = ReconciledUsageQueryUseCase(reader=SnapshotReader(events),
            record_usage=RecordUsageUseCase(self.repository), query_usage=query).query()
        (record,) = result.records
        self.assertEqual(record.cost_kind, "provider_settled")
        self.assertEqual(record.settled_amount, 0.5)
        self.assertIsNone(record.estimate_amount)

    def test_reconciled_summary_keeps_estimated_and_settled_apart(self):
        events = [event(1, settled_amount=0.5, currency="USD"),
                  event(2, provider_model_id="openai/gpt-x")]
        reconciled = ReconciledUsageQueryUseCase(reader=SnapshotReader(events),
            record_usage=RecordUsageUseCase(self.repository),
            query_usage=QueryUsageUseCase(self.repository, prices=StubPrices()))
        totals = {total.cost_kind: total for total in SummarizeUsageUseCase(reconciled).summarize().totals}
        self.assertEqual(sorted(totals), ["estimated", "provider_settled"])
        self.assertEqual(totals["provider_settled"].amount, 0.5)
        self.assertEqual(totals["estimated"].amount, 0.002)
        self.assertEqual(totals["estimated"].units, 2)
        self.assertEqual(totals["provider_settled"].record_count, 1)

    def test_without_a_price_snapshot_records_keep_their_committed_shape(self):
        events = [event(1, provider_model_id="openai/gpt-x", estimate_amount=0.2)]
        result = self.reconciler(SnapshotReader(events)).query()
        self.assertIsNone(result.records[0].cost_kind)
        self.assertEqual(result.to_wire(), {"records": [events[0].payload["usage"]]})
