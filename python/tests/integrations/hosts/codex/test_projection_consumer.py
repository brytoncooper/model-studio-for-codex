from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from model_deck.engine.projections.file_port import ProjectionFileReceipt
from model_deck.engine.projections.intents import ProjectionMutationIntent
from model_deck.engine.projections.ports import ProjectionOutboxEvent
from model_deck.engine.projections.receipts import (
    ProjectionAppliedState,
    ProjectionReceiptConflictError,
)
from model_deck.integrations.hosts.codex.projection_consumer.consumer import (
    CONSUMER_ID,
    CodexProjectionConsumer,
    CodexProjectionDependencyError,
    CodexProjectionMaterializationError,
    CodexProjectionWrite,
)

DATA = b"owned:reg-1"
DIGEST = hashlib.sha256(DATA).hexdigest()
PATH = Path("m/reg-1.json")


def make_event(outbox_id=1, kind="registered_model.upserted", rev=3, agg="reg-1", payload=None):
    if payload is None:
        payload = {
            "connection_id": "c1",
            "display_name": "d1",
            "provider_model_id": "p1",
            "registration_id": agg,
            "revision": rev,
        }
    return ProjectionOutboxEvent(
        outbox_id=outbox_id,
        aggregate_type="registered_model",
        aggregate_id=agg,
        aggregate_revision=rev,
        event_kind=kind,
        payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
    )


_UNSET = object()


def file_receipt(outcome, reason=None, *, path=PATH, expected=None, result=DIGEST, observed=_UNSET):
    return ProjectionFileReceipt(
        operation="write", path=path, outcome=outcome,
        expected_sha256=expected,
        observed_sha256=(expected if observed is _UNSET else observed),
        result_sha256=result, reason=reason,
    )


class FakeOutbox:
    def __init__(self, events):
        self.events = list(events)
        self.calls = []
    def list_pending(self, *, limit=100):
        self.calls.append(("list_pending", limit))
        return tuple(self.events[:limit])


class FakeJournal:
    def __init__(self, intents=None, record_impl=None):
        self.intents = dict(intents or {})
        self.calls = []
        self.record_impl = record_impl
    def get_intent(self, *, outbox_id, consumer_id):
        self.calls.append(("get_intent", outbox_id, consumer_id))
        return self.intents.get((outbox_id, consumer_id))
    def record_intent(self, event, *, consumer_id, operation, artifact_ref, expected_sha256, desired_sha256):
        self.calls.append(("record_intent", event.outbox_id, consumer_id, operation, artifact_ref, expected_sha256, desired_sha256))
        if self.record_impl is not None:
            return self.record_impl(event, consumer_id, operation, artifact_ref, expected_sha256, desired_sha256)
        intent = ProjectionMutationIntent(
            outbox_id=event.outbox_id, consumer_id=consumer_id,
            aggregate_type=event.aggregate_type, aggregate_id=event.aggregate_id,
            aggregate_revision=event.aggregate_revision, event_kind=event.event_kind,
            payload_sha256="p", operation=operation, artifact_ref=artifact_ref,
            expected_sha256=expected_sha256, desired_sha256=desired_sha256,
        )
        self.intents[(event.outbox_id, consumer_id)] = intent
        return intent


class FakeFiles:
    def __init__(self, receipts=None, impl=None, delete_impl=None):
        self.receipts = list(receipts or [])
        self.calls = []
        self.impl = impl
        self.delete_impl = delete_impl
    def compare_and_write(self, path, data, expected):
        self.calls.append(("compare_and_write", path, data, expected))
        if self.impl is not None:
            return self.impl(path, data, expected)
        if self.receipts:
            return self.receipts.pop(0)
        return file_receipt("applied", expected=expected)
    def compare_and_delete(self, path, expected):
        self.calls.append(("compare_and_delete", path, expected))
        if self.delete_impl is not None:
            return self.delete_impl(path, expected)
        if self.receipts:
            return self.receipts.pop(0)
        return delete_receipt("applied", expected=expected, observed=expected)


class StatefulFiles:
    """In-memory owned-file store with real compare-and-write semantics."""
    def __init__(self, initial=None):
        self.store = dict(initial or {})
        self.calls = []
    def compare_and_write(self, path, data, expected):
        self.calls.append(("compare_and_write", path, data, expected))
        current = self.store.get(path)
        desired = hashlib.sha256(data).hexdigest()
        if current is None:
            if expected is not None:
                return ProjectionFileReceipt(operation="write", path=path, outcome="conflict",
                    expected_sha256=expected, observed_sha256=None, result_sha256=None, reason="missing")
            self.store[path] = data
            return ProjectionFileReceipt(operation="write", path=path, outcome="applied",
                expected_sha256=None, observed_sha256=None, result_sha256=desired, reason=None)
        current_hash = hashlib.sha256(current).hexdigest()
        if expected is None:
            return ProjectionFileReceipt(operation="write", path=path, outcome="conflict",
                expected_sha256=None, observed_sha256=current_hash, result_sha256=current_hash,
                reason="unexpected_existing")
        if current_hash != expected:
            return ProjectionFileReceipt(operation="write", path=path, outcome="conflict",
                expected_sha256=expected, observed_sha256=current_hash, result_sha256=current_hash,
                reason="hash_mismatch")
        if current == data:
            return ProjectionFileReceipt(operation="write", path=path, outcome="noop",
                expected_sha256=expected, observed_sha256=current_hash, result_sha256=current_hash, reason=None)
        self.store[path] = data
        return ProjectionFileReceipt(operation="write", path=path, outcome="applied",
            expected_sha256=expected, observed_sha256=current_hash, result_sha256=desired, reason=None)
    def compare_and_delete(self, path, expected):
        self.calls.append(("compare_and_delete", path, expected))
        current = self.store.get(path)
        if current is None:
            if expected is None:
                return ProjectionFileReceipt(operation="delete", path=path, outcome="noop",
                    expected_sha256=None, observed_sha256=None, result_sha256=None, reason=None)
            return ProjectionFileReceipt(operation="delete", path=path, outcome="conflict",
                expected_sha256=expected, observed_sha256=None, result_sha256=None, reason="missing")
        current_hash = hashlib.sha256(current).hexdigest()
        if expected is None:
            return ProjectionFileReceipt(operation="delete", path=path, outcome="conflict",
                expected_sha256=None, observed_sha256=current_hash, result_sha256=current_hash,
                reason="unexpected_existing")
        if current_hash != expected:
            return ProjectionFileReceipt(operation="delete", path=path, outcome="conflict",
                expected_sha256=expected, observed_sha256=current_hash, result_sha256=current_hash,
                reason="hash_mismatch")
        del self.store[path]
        return ProjectionFileReceipt(operation="delete", path=path, outcome="applied",
            expected_sha256=expected, observed_sha256=current_hash, result_sha256=None, reason=None)


class FakeReceipts:
    def __init__(self, applied=None, applied_impl=None, conflict_impl=None):
        self.applied = applied
        self.calls = []
        self.applied_impl = applied_impl
        self.conflict_impl = conflict_impl
    def get_applied(self, *, consumer_id, aggregate_type, aggregate_id):
        self.calls.append(("get_applied", consumer_id, aggregate_type, aggregate_id))
        return self.applied
    def record_applied(self, event, *, consumer_id, artifact_ref, output_sha256):
        self.calls.append(("record_applied", event.outbox_id, consumer_id, artifact_ref, output_sha256))
        if self.applied_impl is not None:
            return self.applied_impl(event, consumer_id, artifact_ref, output_sha256)
        state = ProjectionAppliedState(
            consumer_id=consumer_id, aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id, applied_revision=event.aggregate_revision,
            applied_outbox_id=event.outbox_id, artifact_ref=artifact_ref, output_sha256=output_sha256,
        )
        self.applied = state
        return state
    def record_deleted(self, event, *, consumer_id, artifact_ref):
        self.calls.append(("record_deleted", event.outbox_id, consumer_id, artifact_ref))
        state = ProjectionAppliedState(
            consumer_id=consumer_id, aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id, applied_revision=event.aggregate_revision,
            applied_outbox_id=event.outbox_id, artifact_ref=artifact_ref, output_sha256=None,
        )
        self.applied = state
        return state
    def record_conflict(self, event, *, consumer_id, detail):
        self.calls.append(("record_conflict", event.outbox_id, consumer_id, detail))
        if self.conflict_impl is not None:
            return self.conflict_impl(event, consumer_id, detail)
        from model_deck.engine.projections.receipts import ProjectionOutboxConflictReceipt
        return ProjectionOutboxConflictReceipt(outbox_id=event.outbox_id, consumer_id=consumer_id, detail=detail)


class FakeMaterializer:
    def __init__(self, fn=None):
        self.fn = fn or (lambda e: CodexProjectionWrite(path=PATH, data=DATA))
        self.calls = []
    def materialize(self, event):
        self.calls.append(("materialize", event.outbox_id))
        return self.fn(event)


def make_removed_event(outbox_id=1, rev=4, agg="reg-1", payload=None):
    if payload is None:
        payload = {"registration_id": agg, "removed": True, "revision": rev}
    return ProjectionOutboxEvent(
        outbox_id=outbox_id,
        aggregate_type="registered_model",
        aggregate_id=agg,
        aggregate_revision=rev,
        event_kind="registered_model.removed",
        payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
    )


def delete_receipt(outcome, reason=None, *, path=PATH, expected=DIGEST, observed=_UNSET, result=None):
    return ProjectionFileReceipt(
        operation="delete", path=path, outcome=outcome,
        expected_sha256=expected,
        observed_sha256=(expected if observed is _UNSET else observed),
        result_sha256=result, reason=reason,
    )


def applied_state(rev=2, outbox_id=1, ref="m/reg-1.json", digest=DIGEST):
    return ProjectionAppliedState(
        consumer_id=CONSUMER_ID, aggregate_type="registered_model", aggregate_id="reg-1",
        applied_revision=rev, applied_outbox_id=outbox_id, artifact_ref=ref, output_sha256=digest,
    )


def make_consumer(events, *, journal=None, files=None, receipts=None, materializer=None):
    outbox = FakeOutbox(events)
    journal = journal or FakeJournal()
    files = files or FakeFiles()
    receipts = receipts or FakeReceipts()
    materializer = materializer or FakeMaterializer()
    consumer = CodexProjectionConsumer(outbox, journal, files, receipts, materializer)
    return consumer, outbox, journal, files, receipts, materializer


class EvilRefWrite(CodexProjectionWrite):
    REF = "evil\x00ref"
    @property
    def artifact_ref(self):
        return self.REF


class ProjectionConsumerTest(unittest.TestCase):
    def test_ordered_batch_skip_continues(self):
        ev1 = make_event(1)
        ev2 = make_event(2, kind="other.kind")
        ev3 = make_event(3, rev=4)
        c, outbox, journal, files, receipts, mat = make_consumer([ev1, ev2, ev3])
        batch = c.consume_pending()
        self.assertEqual([i.outcome for i in batch.items], ["applied", "skipped", "applied"])
        self.assertEqual(batch.items[1].reason, "unsupported_event")
        self.assertEqual(len(mat.calls), 2)
        self.assertEqual(len(journal.calls), 4)

    def test_skip_touches_nothing(self):
        ev = make_event(1, kind="other.kind")
        c, outbox, journal, files, receipts, mat = make_consumer([ev])
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].outcome, "skipped")
        self.assertEqual(mat.calls, [])
        self.assertEqual(journal.calls, [])
        self.assertEqual(files.calls, [])
        self.assertEqual(receipts.calls, [])

    def test_event_validation_matrix(self):
        bad_payloads = [
            {"connection_id": "", "display_name": "d", "provider_model_id": "p", "registration_id": "reg-1", "revision": 3},
            {"connection_id": "c", "display_name": "d", "provider_model_id": "p", "registration_id": "other", "revision": 3},
            {"connection_id": "c", "display_name": "d", "provider_model_id": "p", "registration_id": "reg-1", "revision": True},
            {"connection_id": "c", "display_name": "d", "provider_model_id": "p", "registration_id": "reg-1"},
            {"connection_id": "c", "display_name": "d", "provider_model_id": "p", "registration_id": "reg-1", "revision": 3, "extra": 1},
        ]
        for i, payload in enumerate(bad_payloads):
            ev = make_event(10 + i, payload=payload)
            c, outbox, journal, files, receipts, mat = make_consumer([ev])
            batch = c.consume_pending()
            self.assertEqual(batch.items[0].outcome, "conflict", payload)
            self.assertEqual(batch.items[0].reason, "event_invalid")
            self.assertEqual(mat.calls, [])
            self.assertEqual(files.calls, [])
            kinds = [k[0] for k in receipts.calls]
            self.assertIn("record_conflict", kinds)
        ev = make_event(99, rev=4, payload={"connection_id": "c", "display_name": "d", "provider_model_id": "p", "registration_id": "reg-1", "revision": 3})
        c, *_ = make_consumer([ev])
        self.assertEqual(c.consume_pending().items[0].reason, "event_invalid")

    def test_exact_call_order(self):
        log = []
        class J(FakeJournal):
            def get_intent(self, *, outbox_id, consumer_id):
                log.append("get_intent")
                return super().get_intent(outbox_id=outbox_id, consumer_id=consumer_id)
            def record_intent(self, event, **kw):
                log.append("record_intent")
                return super().record_intent(event, **kw)
        class F(FakeFiles):
            def compare_and_write(self, path, data, expected):
                log.append("compare_and_write")
                return super().compare_and_write(path, data, expected)
        class R(FakeReceipts):
            def get_applied(self, **kw):
                log.append("get_applied")
                return super().get_applied(**kw)
            def record_applied(self, event, **kw):
                log.append("record_applied")
                return super().record_applied(event, **kw)
        class M(FakeMaterializer):
            def materialize(self, event):
                log.append("materialize")
                return super().materialize(event)
        c = CodexProjectionConsumer(FakeOutbox([make_event(1)]), J(), F(), R(), M())
        c.consume_pending()
        self.assertEqual(log, ["materialize", "get_applied", "get_intent", "record_intent", "compare_and_write", "record_applied"])

    def test_first_create_applied(self):
        c, outbox, journal, files, receipts, mat = make_consumer([make_event(1)])
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].outcome, "applied")
        self.assertEqual(batch.items[0].artifact_ref, "m/reg-1.json")
        _, _, _, _, _, expected, desired = journal.calls[1]
        self.assertIsNone(expected)
        self.assertEqual(desired, DIGEST)

    def test_write_noop_acknowledged(self):
        applied = ProjectionAppliedState(
            consumer_id=CONSUMER_ID, aggregate_type="registered_model", aggregate_id="reg-1",
            applied_revision=1, applied_outbox_id=1, artifact_ref="m/reg-1.json", output_sha256=DIGEST,
        )
        files = FakeFiles(receipts=[file_receipt("noop", expected=DIGEST, result=DIGEST)])
        c, *_ = make_consumer([make_event(1)], files=files, receipts=FakeReceipts(applied=applied))
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].outcome, "applied")

    def test_prior_applied_matching_hash(self):
        applied = ProjectionAppliedState(
            consumer_id=CONSUMER_ID, aggregate_type="registered_model", aggregate_id="reg-1",
            applied_revision=2, applied_outbox_id=1, artifact_ref="m/reg-1.json", output_sha256=DIGEST,
        )
        receipts = FakeReceipts(applied=applied)
        files = FakeFiles(receipts=[file_receipt("noop", expected=DIGEST, result=DIGEST)])
        c, outbox, journal, f2, r2, mat = make_consumer([make_event(2)], files=files, receipts=receipts)
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].outcome, "applied")
        _, _, _, _, _, expected, _ = journal.calls[1]
        self.assertEqual(expected, DIGEST)

    def test_artifact_change(self):
        applied = ProjectionAppliedState(
            consumer_id=CONSUMER_ID, aggregate_type="registered_model", aggregate_id="reg-1",
            applied_revision=2, applied_outbox_id=1, artifact_ref="m/other.json", output_sha256="0" * 64,
        )
        c, outbox, journal, files, receipts, mat = make_consumer([make_event(2)], receipts=FakeReceipts(applied=applied))
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].reason, "artifact_ref_changed")
        self.assertEqual(files.calls, [])

    def test_existing_intent_replay(self):
        ev = make_event(5)
        existing = ProjectionMutationIntent(
            outbox_id=5, consumer_id=CONSUMER_ID, aggregate_type="registered_model",
            aggregate_id="reg-1", aggregate_revision=3, event_kind=ev.event_kind,
            payload_sha256="p", operation="write", artifact_ref="m/reg-1.json",
            expected_sha256="0" * 64, desired_sha256=DIGEST,
        )
        c, outbox, journal, files, receipts, mat = make_consumer([ev], journal=FakeJournal(intents={(5, CONSUMER_ID): existing}))
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].outcome, "applied")
        _, _, _, _, _, expected, _ = journal.calls[1]
        self.assertEqual(expected, "0" * 64)
        kinds = [k[0] for k in receipts.calls]
        self.assertIn("get_applied", kinds)

    def test_intent_mismatch(self):
        def boom(event, consumer_id, operation, artifact_ref, expected_sha256, desired_sha256):
            raise ProjectionReceiptConflictError("mismatch")
        c, outbox, journal, files, receipts, mat = make_consumer([make_event(1)], journal=FakeJournal(record_impl=boom))
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].reason, "intent_mismatch")
        self.assertEqual(files.calls, [])

    def test_foreign_conflict(self):
        files = FakeFiles(receipts=[file_receipt("conflict", reason="foreign_owner", result=None)])
        c, outbox, journal, f2, receipts, mat = make_consumer([make_event(1)], files=files)
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].outcome, "conflict")
        self.assertEqual(batch.items[0].reason, "foreign_owner")
        details = [k for k in receipts.calls if k[0] == "record_conflict"]
        self.assertEqual(details[0][3], "file_foreign_owner")

    def test_race_conflict_hash_mismatch(self):
        files = FakeFiles(receipts=[file_receipt("conflict", reason="hash_mismatch", result=None)])
        c, *_ = make_consumer([make_event(1)], files=files)
        self.assertEqual(c.consume_pending().items[0].reason, "hash_mismatch")

    def test_malformed_receipts_raise(self):
        good = file_receipt("applied")
        bad_variants = [
            ProjectionFileReceipt(operation="delete", path=PATH, outcome="applied",
                expected_sha256=None, observed_sha256=None, result_sha256=DIGEST, reason=None),
            ProjectionFileReceipt(operation="write", path=Path("m/other.json"), outcome="applied",
                expected_sha256=None, observed_sha256=None, result_sha256=DIGEST, reason=None),
            ProjectionFileReceipt(operation="write", path=PATH, outcome="applied",
                expected_sha256="0" * 64, observed_sha256=None, result_sha256=DIGEST, reason=None),
            ProjectionFileReceipt(operation="write", path=PATH, outcome="applied",
                expected_sha256=None, observed_sha256=None, result_sha256="1" * 64, reason=None),
            ProjectionFileReceipt(operation="write", path=PATH, outcome="noop",
                expected_sha256=None, observed_sha256=None, result_sha256=DIGEST, reason="stale"),
            ProjectionFileReceipt(operation="write", path=PATH, outcome="conflict",
                expected_sha256=None, observed_sha256=None, result_sha256=None, reason="disk exploded: secret token"),
            ProjectionFileReceipt(operation="write", path=PATH, outcome="conflict",
                expected_sha256=None, observed_sha256=None, result_sha256=None, reason="postdelete_interference"),
            ProjectionFileReceipt(operation="write", path=PATH, outcome="applied",
                expected_sha256=None, observed_sha256=DIGEST, result_sha256=DIGEST, reason=None),
            ProjectionFileReceipt(operation="write", path=PATH, outcome="applied",
                expected_sha256=DIGEST, observed_sha256="2" * 64, result_sha256=DIGEST, reason=None),
            ProjectionFileReceipt(operation="write", path=PATH, outcome="noop",
                expected_sha256=None, observed_sha256=None, result_sha256=DIGEST, reason=None),
            ProjectionFileReceipt(operation="write", path=PATH, outcome="noop",
                expected_sha256="0" * 64, observed_sha256="0" * 64, result_sha256=DIGEST, reason=None),
            ProjectionFileReceipt(operation="write", path=PATH, outcome="weird",
                expected_sha256=None, observed_sha256=None, result_sha256=None, reason=None),
            ("not", "a", "receipt"),
        ]
        self.assertEqual(good.result_sha256, DIGEST)
        for bad in bad_variants:
            ev1, ev2 = make_event(1), make_event(2)
            journal, receipts = FakeJournal(), FakeReceipts()
            mat = FakeMaterializer()
            files = FakeFiles(receipts=[bad])
            c = CodexProjectionConsumer(FakeOutbox([ev1, ev2]), journal, files, receipts, mat)
            with self.assertRaises(CodexProjectionDependencyError, msg=str(bad)):
                c.consume_pending()
            kinds = [k[0] for k in receipts.calls]
            self.assertNotIn("record_applied", kinds)
            self.assertNotIn("record_conflict", kinds)
            self.assertEqual([m[1] for m in mat.calls], [1])

    def test_invalid_artifact_refs(self):
        cases = [
            FakeMaterializer(fn=lambda e: CodexProjectionWrite(path=Path("m/a\nb.json"), data=DATA)),
            FakeMaterializer(fn=lambda e: CodexProjectionWrite(path=Path("m/\u00e9.json"), data=DATA)),
            FakeMaterializer(fn=lambda e: CodexProjectionWrite(path=Path("m/" + "a" * 260 + ".json"), data=DATA)),
            FakeMaterializer(fn=lambda e: EvilRefWrite(path=PATH, data=DATA)),
            FakeMaterializer(fn=lambda e: CodexProjectionWrite(path=Path("/abs.json"), data=DATA)),
        ]
        for mat in cases:
            journal, files, receipts = FakeJournal(), FakeFiles(), FakeReceipts()
            c = CodexProjectionConsumer(FakeOutbox([make_event(1)]), journal, files, receipts, mat)
            batch = c.consume_pending()
            self.assertEqual(batch.items[0].reason, "materialization_invalid")
            self.assertEqual([k[0] for k in journal.calls if k[0] == "record_intent"], [])
            self.assertEqual(files.calls, [])
            details = [k[3] for k in receipts.calls if k[0] == "record_conflict"]
            self.assertEqual(details, ["materialization_invalid"])
            for call in receipts.calls:
                self.assertNotIn("evil", str(call))

    def test_crash_after_intent_before_file_retries_from_intent(self):
        ev = make_event(7)
        persisted = {}
        def persist_then_crash(event, consumer_id, operation, artifact_ref, expected_sha256, desired_sha256):
            intent = ProjectionMutationIntent(
                outbox_id=event.outbox_id, consumer_id=consumer_id,
                aggregate_type=event.aggregate_type, aggregate_id=event.aggregate_id,
                aggregate_revision=event.aggregate_revision, event_kind=event.event_kind,
                payload_sha256="p", operation=operation, artifact_ref=artifact_ref,
                expected_sha256=expected_sha256, desired_sha256=desired_sha256,
            )
            persisted[(event.outbox_id, consumer_id)] = intent
            raise RuntimeError("crash after intent")
        journal = FakeJournal(record_impl=persist_then_crash)
        files, receipts = StatefulFiles(), FakeReceipts()
        c1 = CodexProjectionConsumer(FakeOutbox([ev]), journal, files, receipts, FakeMaterializer())
        with self.assertRaises(RuntimeError):
            c1.consume_pending()
        self.assertEqual(files.calls, [])
        self.assertIsNone(receipts.applied)
        journal2 = FakeJournal(intents=persisted)
        c2 = CodexProjectionConsumer(FakeOutbox([ev]), journal2, files, receipts, FakeMaterializer())
        batch = c2.consume_pending()
        self.assertEqual(batch.items[0].outcome, "applied")
        self.assertEqual(files.store[PATH], DATA)
        _, _, _, _, _, expected, _ = journal2.calls[1]
        self.assertIsNone(expected)

    def test_crash_after_first_create_retry_conflicts_no_adoption(self):
        ev = make_event(8)
        files = StatefulFiles()
        receipts = FakeReceipts(applied_impl=None)
        journal = FakeJournal()
        def crash_on_ack(event, consumer_id, artifact_ref, output_sha256):
            raise RuntimeError("ack crash")
        receipts.applied_impl = crash_on_ack
        c1 = CodexProjectionConsumer(FakeOutbox([ev]), journal, files, receipts, FakeMaterializer())
        with self.assertRaises(RuntimeError):
            c1.consume_pending()
        self.assertEqual(files.store[PATH], DATA)
        self.assertIsNone(receipts.applied)
        receipts.applied_impl = None
        c2 = CodexProjectionConsumer(FakeOutbox([ev]), journal, files, receipts, FakeMaterializer())
        batch = c2.consume_pending()
        self.assertEqual(batch.items[0].outcome, "conflict")
        self.assertEqual(batch.items[0].reason, "unexpected_existing")
        self.assertIsNone(receipts.applied)
        self.assertEqual(files.store[PATH], DATA)

    def test_crash_after_replacement_ack_retry_hash_mismatch(self):
        old, new = b"owned:old", b"owned:reg-1"
        old_hash, new_hash = hashlib.sha256(old).hexdigest(), hashlib.sha256(new).hexdigest()
        self.assertNotEqual(old_hash, new_hash)
        ev = make_event(9)
        applied = ProjectionAppliedState(
            consumer_id=CONSUMER_ID, aggregate_type="registered_model", aggregate_id="reg-1",
            applied_revision=2, applied_outbox_id=4, artifact_ref="m/reg-1.json", output_sha256=old_hash,
        )
        receipts = FakeReceipts(applied=applied)
        journal = FakeJournal()
        files = StatefulFiles(initial={PATH: old})
        def crash_on_ack(event, consumer_id, artifact_ref, output_sha256):
            raise RuntimeError("ack crash")
        receipts.applied_impl = crash_on_ack
        c1 = CodexProjectionConsumer(FakeOutbox([ev]), journal, files, receipts, FakeMaterializer())
        with self.assertRaises(RuntimeError):
            c1.consume_pending()
        self.assertEqual(files.store[PATH], new)
        self.assertEqual(receipts.applied.output_sha256, old_hash)
        receipts.applied_impl = None
        c2 = CodexProjectionConsumer(FakeOutbox([ev]), journal, files, receipts, FakeMaterializer())
        batch = c2.consume_pending()
        self.assertEqual(batch.items[0].outcome, "conflict")
        self.assertEqual(batch.items[0].reason, "hash_mismatch")
        self.assertEqual(files.store[PATH], new)
        self.assertEqual(receipts.applied.output_sha256, old_hash)

    def test_materializer_message_redaction(self):
        def boom(event):
            raise CodexProjectionMaterializationError("super secret token abc")
        c, outbox, journal, files, receipts, mat = make_consumer([make_event(1)], materializer=FakeMaterializer(fn=boom))
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].reason, "materialization_invalid")
        for call in receipts.calls:
            self.assertNotIn("secret", str(call))
        self.assertEqual(files.calls, [])

    def test_invalid_mutation_path(self):
        mat = FakeMaterializer(fn=lambda e: CodexProjectionWrite(path=Path("../evil"), data=b"owned:x"))
        c, outbox, journal, files, receipts, m2 = make_consumer([make_event(1)], materializer=mat)
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].reason, "materialization_invalid")
        self.assertEqual(files.calls, [])

    def test_unexpected_exception_stops_later(self):
        ev1, ev2 = make_event(1), make_event(2)
        journal, receipts, mat = FakeJournal(), FakeReceipts(), FakeMaterializer()
        def boom(path, data, expected):
            raise RuntimeError("disk exploded")
        files = FakeFiles(impl=boom)
        c = CodexProjectionConsumer(FakeOutbox([ev1, ev2]), journal, files, receipts, mat)
        with self.assertRaises(RuntimeError):
            c.consume_pending()
        self.assertEqual([m[1] for m in mat.calls], [1])
        self.assertEqual(len(files.calls), 1)
        journal_ids = [k[1] for k in journal.calls if k[0] == "record_intent"]
        self.assertEqual(journal_ids, [1])
        self.assertEqual([k[1] for k in receipts.calls if k[0] in ("record_applied", "record_conflict")], [])


    def test_removal_applied(self):
        ev = make_removed_event(10, rev=4)
        c, outbox, journal, files, receipts, mat = make_consumer(
            [ev], receipts=FakeReceipts(applied=applied_state()))
        batch = c.consume_pending()
        item = batch.items[0]
        self.assertEqual(item.outcome, "applied")
        self.assertEqual(item.artifact_ref, "m/reg-1.json")
        self.assertEqual(files.calls[0][:2], ("compare_and_delete", PATH))
        self.assertEqual(files.calls[0][2], DIGEST)
        kinds = [k[0] for k in receipts.calls]
        self.assertEqual(kinds, ["get_applied", "record_deleted"])
        self.assertEqual(mat.calls, [])
        _, _, _, _, _, expected, desired = journal.calls[1]
        self.assertEqual(expected, DIGEST)
        self.assertIsNone(desired)

    def test_removal_no_prior_applied_unresolved(self):
        ev = make_removed_event(11, rev=2)
        c, outbox, journal, files, receipts, mat = make_consumer([ev])
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].outcome, "conflict")
        self.assertEqual(batch.items[0].reason, "unresolved_no_prior_applied")
        self.assertEqual(files.calls, [])
        details = [k[3] for k in receipts.calls if k[0] == "record_conflict"]
        self.assertEqual(details, ["unresolved_no_prior_applied"])

    def test_removal_stale_revision_never_removes(self):
        ev = make_removed_event(12, rev=2)
        files = StatefulFiles(initial={PATH: DATA})
        c, outbox, journal, f2, receipts, mat = make_consumer(
            [ev], files=files, receipts=FakeReceipts(applied=applied_state(rev=3)))
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].reason, "stale_revision")
        self.assertEqual(files.calls, [])
        self.assertEqual(files.store[PATH], DATA)

    def test_removal_foreign_conflict(self):
        ev = make_removed_event(13, rev=4)
        files = FakeFiles(receipts=[delete_receipt("conflict", reason="foreign_owner")])
        c, *_ = make_consumer([ev], files=files, receipts=FakeReceipts(applied=applied_state()))
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].outcome, "conflict")
        self.assertEqual(batch.items[0].reason, "foreign_owner")

    def test_removal_symlink_conflict(self):
        ev = make_removed_event(14, rev=4)
        files = FakeFiles(receipts=[delete_receipt("conflict", reason="symlink", result=None)])
        c, *_ = make_consumer([ev], files=files, receipts=FakeReceipts(applied=applied_state()))
        self.assertEqual(c.consume_pending().items[0].reason, "symlink")

    def test_removal_content_changed_conflict(self):
        ev = make_removed_event(15, rev=4)
        files = FakeFiles(receipts=[delete_receipt("conflict", reason="hash_mismatch", observed="0" * 64, result="0" * 64)])
        c, *_ = make_consumer([ev], files=files, receipts=FakeReceipts(applied=applied_state()))
        self.assertEqual(c.consume_pending().items[0].reason, "hash_mismatch")

    def test_removal_missing_first_attempt_conflicts(self):
        ev = make_removed_event(16, rev=4)
        files = FakeFiles(receipts=[delete_receipt("conflict", reason="missing", observed=None)])
        c, outbox, journal, f2, receipts, mat = make_consumer(
            [ev], files=files, receipts=FakeReceipts(applied=applied_state()))
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].reason, "missing")
        kinds = [k[0] for k in receipts.calls]
        self.assertNotIn("record_deleted", kinds)

    def test_removal_retry_after_crash_acknowledges(self):
        ev = make_removed_event(17, rev=4)
        persisted = {}
        def persist_then_crash(event, consumer_id, operation, artifact_ref, expected_sha256, desired_sha256):
            intent = ProjectionMutationIntent(
                outbox_id=event.outbox_id, consumer_id=consumer_id,
                aggregate_type=event.aggregate_type, aggregate_id=event.aggregate_id,
                aggregate_revision=event.aggregate_revision, event_kind=event.event_kind,
                payload_sha256="p", operation=operation, artifact_ref=artifact_ref,
                expected_sha256=expected_sha256, desired_sha256=desired_sha256,
            )
            persisted[(event.outbox_id, consumer_id)] = intent
            raise RuntimeError("crash after delete intent")
        files = StatefulFiles(initial={PATH: DATA})
        receipts = FakeReceipts(applied=applied_state())
        c1 = CodexProjectionConsumer(
            FakeOutbox([ev]), FakeJournal(record_impl=persist_then_crash),
            files, receipts, FakeMaterializer())
        with self.assertRaises(RuntimeError):
            c1.consume_pending()
        self.assertEqual(files.calls, [])
        files.store.clear()
        c2 = CodexProjectionConsumer(
            FakeOutbox([ev]), FakeJournal(intents=persisted),
            files, receipts, FakeMaterializer())
        batch = c2.consume_pending()
        self.assertEqual(batch.items[0].outcome, "applied")
        self.assertEqual(batch.items[0].artifact_ref, "m/reg-1.json")
        self.assertIsNone(receipts.applied.output_sha256)

    def test_removal_of_tombstone_noop_acknowledges(self):
        ev = make_removed_event(18, rev=5)
        tombstone = applied_state(rev=4, outbox_id=9, digest=None)
        files = StatefulFiles()
        c, *_ = make_consumer([ev], files=files, receipts=FakeReceipts(applied=tombstone))
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].outcome, "applied")

    def test_recreate_after_tombstone(self):
        tombstone = applied_state(rev=4, outbox_id=9, digest=None)
        files = StatefulFiles()
        c, outbox, journal, f2, receipts, mat = make_consumer(
            [make_event(20, rev=5)], files=files, receipts=FakeReceipts(applied=tombstone))
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].outcome, "applied")
        self.assertEqual(files.store[PATH], DATA)
        _, _, _, _, _, expected, _ = journal.calls[1]
        self.assertIsNone(expected)

    def test_stale_upsert_after_tombstone_never_resurrects(self):
        tombstone = applied_state(rev=3, outbox_id=2, digest=None)
        files = StatefulFiles()
        ev = make_event(3, rev=2)
        c, outbox, journal, f2, receipts, mat = make_consumer(
            [ev], files=files, receipts=FakeReceipts(applied=tombstone))
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].outcome, "conflict")
        self.assertEqual(batch.items[0].reason, "stale_revision")
        self.assertEqual(files.calls, [])
        self.assertEqual(files.store, {})
        self.assertNotIn('record_intent', [k[0] for k in journal.calls])
        kinds = [k[0] for k in receipts.calls]
        self.assertIn("record_conflict", kinds)
        self.assertNotIn("record_applied", kinds)
        self.assertEqual(receipts.applied.applied_revision, 3)
        self.assertIsNone(receipts.applied.output_sha256)

    def test_upsert_exact_replay_acknowledges_without_file_mutation(self):
        state = applied_state(rev=2, outbox_id=7)
        ev = make_event(7, rev=2)
        files = StatefulFiles(initial={PATH: DATA})
        c, outbox, journal, f2, receipts, mat = make_consumer(
            [ev], files=files, receipts=FakeReceipts(applied=state))
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].outcome, "applied")
        self.assertEqual(files.calls, [])
        self.assertEqual(journal.calls, [])

    def test_removal_payload_matrix(self):
        bad_payloads = [
            {"registration_id": "reg-1", "removed": False, "revision": 4},
            {"registration_id": "reg-1", "revision": 4},
            {"registration_id": "", "removed": True, "revision": 4},
            {"registration_id": "other", "removed": True, "revision": 4},
            {"registration_id": "reg-1", "removed": True, "revision": True},
            {"registration_id": "reg-1", "removed": True, "revision": 4, "extra": 1},
        ]
        for i, payload in enumerate(bad_payloads):
            ev = make_removed_event(30 + i, payload=payload)
            c, outbox, journal, files, receipts, mat = make_consumer(
                [ev], receipts=FakeReceipts(applied=applied_state()))
            batch = c.consume_pending()
            self.assertEqual(batch.items[0].reason, "event_invalid", payload)
            self.assertEqual(files.calls, [])
        ev = make_removed_event(99, rev=5, payload={"registration_id": "reg-1", "removed": True, "revision": 4})
        c, *_ = make_consumer([ev], receipts=FakeReceipts(applied=applied_state()))
        self.assertEqual(c.consume_pending().items[0].reason, "event_invalid")

    def test_removal_malformed_receipts_raise(self):
        bad_variants = [
            ProjectionFileReceipt(operation="write", path=PATH, outcome="applied",
                expected_sha256=DIGEST, observed_sha256=DIGEST, result_sha256=None, reason=None),
            ProjectionFileReceipt(operation="delete", path=Path("m/other.json"), outcome="applied",
                expected_sha256=DIGEST, observed_sha256=DIGEST, result_sha256=None, reason=None),
            ProjectionFileReceipt(operation="delete", path=PATH, outcome="applied",
                expected_sha256=DIGEST, observed_sha256=DIGEST, result_sha256=DIGEST, reason=None),
            ProjectionFileReceipt(operation="delete", path=PATH, outcome="applied",
                expected_sha256=DIGEST, observed_sha256=DIGEST, result_sha256=None, reason="x"),
            ProjectionFileReceipt(operation="delete", path=PATH, outcome="noop",
                expected_sha256=DIGEST, observed_sha256=None, result_sha256=None, reason=None),
            ProjectionFileReceipt(operation="delete", path=PATH, outcome="conflict",
                expected_sha256=DIGEST, observed_sha256=DIGEST, result_sha256=None,
                reason="postwrite_interference"),
            ProjectionFileReceipt(operation="delete", path=PATH, outcome="weird",
                expected_sha256=DIGEST, observed_sha256=DIGEST, result_sha256=None, reason=None),
            ("not", "a", "receipt"),
        ]
        for bad in bad_variants:
            ev = make_removed_event(40)
            journal, receipts = FakeJournal(), FakeReceipts(applied=applied_state())
            files = FakeFiles(receipts=[bad])
            c = CodexProjectionConsumer(FakeOutbox([ev]), journal, files, receipts, FakeMaterializer())
            with self.assertRaises(CodexProjectionDependencyError, msg=str(bad)):
                c.consume_pending()
            kinds = [k[0] for k in receipts.calls]
            self.assertNotIn("record_deleted", kinds)
            self.assertNotIn("record_conflict", kinds)

    def test_removal_intent_mismatch(self):
        def boom(event, consumer_id, operation, artifact_ref, expected_sha256, desired_sha256):
            raise ProjectionReceiptConflictError("mismatch")
        ev = make_removed_event(50, rev=4)
        c, outbox, journal, files, receipts, mat = make_consumer(
            [ev], journal=FakeJournal(record_impl=boom),
            receipts=FakeReceipts(applied=applied_state()))
        batch = c.consume_pending()
        self.assertEqual(batch.items[0].reason, "intent_mismatch")
        self.assertEqual(files.calls, [])

    def test_limit_default(self):
        c, outbox, *_ = make_consumer([])
        c.consume_pending()
        self.assertEqual(outbox.calls[0][1], 100)



class ProjectionConsumerSQLiteIntegrationTest(unittest.TestCase):
    """Real producer, journal, receipts, and files with a deterministic renderer."""

    def setUp(self):
        import sqlite3
        from tempfile import TemporaryDirectory
        from model_deck.adapters.storage.sqlite_outbox import ensure_projection_outbox_schema
        from model_deck.adapters.storage.sqlite_projection_outbox import SQLiteProjectionOutboxReader
        from model_deck.adapters.storage.sqlite_projection_intents import SQLiteProjectionMutationIntentJournal
        from model_deck.adapters.storage.sqlite_projection_receipts import SQLiteProjectionReceiptStore
        from model_deck.adapters.filesystem.conditional_projection_files import FixtureConditionalProjectionFiles

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / PATH.parent).mkdir()
        self.path = self.root / PATH
        self.db = self.root / 'projection.sqlite3'
        with sqlite3.connect(self.db) as conn:
            ensure_projection_outbox_schema(conn)
        self.outbox = SQLiteProjectionOutboxReader(self.db)
        self.journal = SQLiteProjectionMutationIntentJournal(self.db)
        self.receipts = SQLiteProjectionReceiptStore(self.db)
        self.files = FixtureConditionalProjectionFiles(self.root, owned_prefix=b'owned:')
        self.materializer = FakeMaterializer(
            fn=lambda event: CodexProjectionWrite(PATH, DATA + str(event.aggregate_revision).encode())
        )

    def enqueue(self, revision, *, remove=False):
        import sqlite3
        from model_deck.adapters.storage.sqlite_outbox import (
            enqueue_registered_model_upserted, enqueue_registered_model_removed,
        )
        with sqlite3.connect(self.db) as conn:
            if remove:
                enqueue_registered_model_removed(conn, registration_id='reg-1', revision=revision)
            else:
                enqueue_registered_model_upserted(
                    conn, registration_id='reg-1', revision=revision,
                    provider_model_id='fixture', connection_id='fixture', display_name='Fixture',
                )
        return self.outbox.list_pending()[-1]

    def consume(self, *, files=None, receipts=None, events=None):
        # An explicit snapshot models an already-read batch or a later worker
        # overtaking a crashed event. Its event still comes from the real outbox.
        outbox = self.outbox if events is None else FakeOutbox(events)
        return CodexProjectionConsumer(
            outbox, self.journal, files or self.files,
            receipts or self.receipts, self.materializer,
        ).consume_pending()

    def state(self):
        return self.receipts.get_applied(
            consumer_id=CONSUMER_ID, aggregate_type='registered_model', aggregate_id='reg-1',
        )

    def outbox_state(self, event):
        import sqlite3
        with sqlite3.connect(self.db) as conn:
            return conn.execute('SELECT state FROM projection_outbox WHERE outbox_id=?',
                                (event.outbox_id,)).fetchone()[0]

    def create_then_delete(self):
        self.enqueue(1)
        self.consume()
        self.enqueue(3, remove=True)
        self.consume()
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.state().output_sha256)

    def test_stale_upsert_and_equal_revision_tombstone_never_recreate(self):
        self.create_then_delete()
        for revision in (2, 3):
            with self.subTest(revision=revision):
                event = self.enqueue(revision)
                result = self.consume().items[0]
                self.assertEqual(result.reason, 'stale_revision')
                self.assertFalse(self.path.exists())
                self.assertEqual(self.state().applied_revision, 3)
                self.assertIsNone(self.state().output_sha256)
                self.assertEqual(self.outbox_state(event), 'conflict')
                self.assertIsNone(self.journal.get_intent(outbox_id=event.outbox_id, consumer_id=CONSUMER_ID))

    def test_stale_intent_retry_cannot_bypass_current_tombstone(self):
        self.enqueue(1)
        self.consume()
        stale = self.enqueue(2)
        class BeforeWriteCrash:
            def compare_and_write(self, *args):
                raise RuntimeError('before write')
        with self.assertRaisesRegex(RuntimeError, 'before write'):
            self.consume(files=BeforeWriteCrash())
        self.assertIsNotNone(self.journal.get_intent(outbox_id=stale.outbox_id, consumer_id=CONSUMER_ID))
        removal = self.enqueue(3, remove=True)
        self.consume(events=[removal])
        result = self.consume().items[0]
        self.assertEqual(result.reason, 'stale_revision')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.outbox_state(stale), 'conflict')
        self.assertIsNone(self.state().output_sha256)

    def test_exact_current_receipt_replay_never_mutates_a_replaced_file(self):
        event = self.enqueue(1)
        self.consume()
        original_state = self.state()
        self.path.write_bytes(b'foreign replacement')
        class NoMutation:
            def compare_and_write(self, *args):
                raise AssertionError('exact acknowledgment replay attempted a write')
        result = self.consume(files=NoMutation(), events=[event]).items[0]
        self.assertEqual(result.outcome, 'applied')
        self.assertEqual(self.path.read_bytes(), b'foreign replacement')
        self.assertEqual(self.state(), original_state)

    def crash_after_delete(self):
        self.enqueue(1)
        self.consume()
        event = self.enqueue(2, remove=True)
        real = self.receipts
        class BeforeReceiptCrash:
            def get_applied(self, **kwargs):
                return real.get_applied(**kwargs)
            def record_deleted(self, *args, **kwargs):
                raise RuntimeError('after delete before receipt')
        with self.assertRaisesRegex(RuntimeError, 'after delete before receipt'):
            self.consume(receipts=BeforeReceiptCrash())
        self.assertFalse(self.path.exists())
        self.assertEqual(self.outbox_state(event), 'pending')
        return event

    def test_exact_removal_receipt_replay_has_no_file_or_journal_call(self):
        self.enqueue(1)
        self.consume()
        event = self.enqueue(2, remove=True)
        self.consume()
        tombstone = self.state()
        self.path.write_bytes(b'foreign replacement')
        class NoMutation:
            def compare_and_delete(self, *args):
                raise AssertionError('removal replay attempted a file operation')
        class NoJournal:
            def get_intent(self, **kwargs):
                raise AssertionError('removal replay accessed the journal')
            def record_intent(self, *args, **kwargs):
                raise AssertionError('removal replay changed the journal')
        self.journal = NoJournal()
        for _ in range(2):
            result = self.consume(files=NoMutation(), events=[event]).items[0]
            self.assertEqual(result.outcome, 'applied')
            self.assertEqual(self.state(), tombstone)
            self.assertEqual(self.path.read_bytes(), b'foreign replacement')
            self.assertEqual(self.outbox_state(event), 'applied')

    def test_removal_replay_rejects_hash_bearing_receipt_without_mutation(self):
        self.enqueue(1)
        self.consume()
        event = self.enqueue(2, remove=True)
        self.receipts.record_applied(
            event, consumer_id=CONSUMER_ID, artifact_ref=PATH.as_posix(),
            output_sha256=self.state().output_sha256,
        )
        before = self.state()
        before_bytes = self.path.read_bytes()
        with self.assertRaises(ProjectionReceiptConflictError):
            self.consume(events=[event])
        self.assertEqual(self.state(), before)
        self.assertEqual(self.path.read_bytes(), before_bytes)
        self.assertIsNone(self.journal.get_intent(outbox_id=event.outbox_id, consumer_id=CONSUMER_ID))

    def test_delete_crash_retry_then_newer_recreation(self):
        event = self.crash_after_delete()
        self.assertEqual(self.consume().items[0].outcome, 'applied')
        self.assertEqual(self.outbox_state(event), 'applied')
        self.assertIsNone(self.state().output_sha256)
        self.enqueue(3)
        self.assertEqual(self.consume().items[0].outcome, 'applied')
        self.assertEqual(self.path.read_bytes(), DATA + b'3')
        self.assertEqual(self.state().output_sha256, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_delete_crash_retry_preserves_foreign_replacement(self):
        event = self.crash_after_delete()
        self.path.write_bytes(b'foreign replacement')
        self.assertEqual(self.consume().items[0].reason, 'foreign_owner')
        self.assertEqual(self.path.read_bytes(), b'foreign replacement')
        self.assertEqual(self.state().applied_revision, 1)
        self.assertEqual(self.outbox_state(event), 'conflict')

    def test_contradictory_missing_receipts_never_acknowledge_deletion(self):
        from dataclasses import replace
        event = self.crash_after_delete()
        self.path.write_bytes(b'foreign replacement')
        real = self.files
        for observed, result in ((DIGEST, None), (None, DIGEST), (DIGEST, DIGEST)):
            with self.subTest(observed=observed, result=result):
                class ContradictoryReceipt:
                    def compare_and_delete(self, *args):
                        receipt = real.compare_and_delete(*args)
                        return replace(receipt, reason='missing', observed_sha256=observed, result_sha256=result)
                with self.assertRaises(CodexProjectionDependencyError):
                    self.consume(files=ContradictoryReceipt())
                self.assertEqual(self.outbox_state(event), 'pending')
                self.assertEqual(self.state().applied_revision, 1)
                self.assertEqual(self.path.read_bytes(), b'foreign replacement')


if __name__ == '__main__':
    unittest.main()
