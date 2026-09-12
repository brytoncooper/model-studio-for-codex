from __future__ import annotations

import hashlib
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from model_deck.adapters.storage.sqlite_host_settings import (
    SQLitePreviewStore,
    SQLiteSaveReceiptStore,
    validate_save_result,
)
from model_deck.engine.host_settings.ports import (
    CLAIM_ADMITTED,
    CLAIM_IN_PROGRESS,
    CLAIM_REPLAY,
    PreviewExistsError,
    PreviewRecord,
    ReceiptConflictError,
    SaveClaimBinding,
)
from model_deck.engine.host_settings.service import HostSettingsService
from model_deck.engine.host_settings.ports import CallerContext, READ_GRANT, WRITE_GRANT

LOCAL = "local-operator-principal"
OTHER = "other-principal"
HOST = "com.example.host"
DOC = "ref:codex-user-config"
CTX = "ctx-1"
RAW = "key = 1\n"


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _result(raw: str = RAW) -> dict[str, Any]:
    return {
        "saved": True,
        "changed": raw != RAW,
        "document_id": DOC,
        "previous_content_hash": _sha(RAW),
        "document_revision": _sha(raw),
        "backup": None,
        "application_effects": [],
        "context_revision": CTX,
    }


def _record(preview_id: str, principal: str = LOCAL) -> PreviewRecord:
    return PreviewRecord(
        preview_id=preview_id,
        principal=principal,
        host_id=HOST,
        document_id=DOC,
        base_content_hash=_sha(RAW),
        context_revision=CTX,
        candidate_content_hash=_sha(RAW),
    )


def _binding(
    preview_id: str,
    key: str = "key-1",
    fingerprint: str = "fp-1",
    principal: str = LOCAL,
) -> SaveClaimBinding:
    return SaveClaimBinding(
        principal=principal,
        idempotency_key=key,
        fingerprint=fingerprint,
        host_id=HOST,
        document_id=DOC,
        base_content_hash=_sha(RAW),
        context_revision=CTX,
        candidate_content_hash=_sha(RAW),
        preview_id=preview_id,
    )


class _Stores(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.db = Path(self._tmp.name) / "settings.db"
        self.previews = SQLitePreviewStore(self.db)
        self.receipts = SQLiteSaveReceiptStore(self.db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fresh(self) -> tuple[SQLitePreviewStore, SQLiteSaveReceiptStore]:
        return (SQLitePreviewStore(self.db), SQLiteSaveReceiptStore(self.db))


class PreviewLedgerTest(_Stores):
    def test_admit_lookup_consume_roundtrip(self) -> None:
        self.previews.admit(_record("tok-1"))
        found = self.previews.lookup("tok-1")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.preview_id, "tok-1")
        self.assertFalse(self.previews.is_consumed("tok-1"))
        consumed = self.previews.consume("tok-1")
        self.assertIsNotNone(consumed)
        self.assertIsNone(self.previews.lookup("tok-1"))
        self.assertTrue(self.previews.is_consumed("tok-1"))

    def test_duplicate_admit_rejected_before_and_after_consume(self) -> None:
        self.previews.admit(_record("tok-1"))
        with self.assertRaises(PreviewExistsError):
            self.previews.admit(_record("tok-1"))
        self.previews.consume("tok-1")
        with self.assertRaises(PreviewExistsError):
            self.previews.admit(_record("tok-1"))

    def test_unknown_token_lookup_and_consume(self) -> None:
        self.assertIsNone(self.previews.lookup("missing"))
        self.assertFalse(self.previews.is_consumed("missing"))
        self.assertIsNone(self.previews.consume("missing"))

    def test_double_consume_returns_none(self) -> None:
        self.previews.admit(_record("tok-1"))
        self.assertIsNotNone(self.previews.consume("tok-1"))
        self.assertIsNone(self.previews.consume("tok-1"))

    def test_consumed_records_survive_restart(self) -> None:
        self.previews.admit(_record("tok-1"))
        self.previews.consume("tok-1")
        fresh_previews, _ = self._fresh()
        self.assertIsNone(fresh_previews.lookup("tok-1"))
        self.assertTrue(fresh_previews.is_consumed("tok-1"))


class ReceiptLedgerTest(_Stores):
    def test_claim_settle_lookup_replay(self) -> None:
        outcome, settled = self.receipts.claim(_binding("tok-1"))
        self.assertEqual(outcome, CLAIM_ADMITTED)
        self.assertIsNone(settled)
        self.assertIsNone(self.receipts.lookup(LOCAL, "key-1", "fp-1"))
        self.receipts.settle(LOCAL, "key-1", "fp-1", _result())
        replayed = self.receipts.lookup(LOCAL, "key-1", "fp-1")
        self.assertEqual(replayed, _result())
        outcome, settled = self.receipts.claim(_binding("tok-1"))
        self.assertEqual(outcome, CLAIM_REPLAY)
        self.assertEqual(settled, _result())

    def test_uncertain_claim_reports_in_progress_and_survives_restart(self) -> None:
        self.assertEqual(self.receipts.claim(_binding("tok-1")), (CLAIM_ADMITTED, None))
        self.assertEqual(
            self.receipts.claim(_binding("tok-1")), (CLAIM_IN_PROGRESS, None)
        )
        _, fresh_receipts = self._fresh()
        self.assertIsNone(fresh_receipts.lookup(LOCAL, "key-1", "fp-1"))
        self.assertEqual(
            fresh_receipts.claim(_binding("tok-1")), (CLAIM_IN_PROGRESS, None)
        )

    def test_changed_fingerprint_conflicts_before_and_after_settle(self) -> None:
        self.receipts.claim(_binding("tok-1", fingerprint="fp-1"))
        with self.assertRaises(ReceiptConflictError):
            self.receipts.claim(_binding("tok-1", fingerprint="fp-2"))
        self.receipts.settle(LOCAL, "key-1", "fp-1", _result())
        with self.assertRaises(ReceiptConflictError):
            self.receipts.claim(_binding("tok-1", fingerprint="fp-2"))
        with self.assertRaises(ReceiptConflictError):
            self.receipts.settle(LOCAL, "key-1", "fp-2", _result())

    def test_settled_replay_returned_for_exact_binding(self) -> None:
        self.receipts.claim(_binding("tok-1"))
        self.receipts.settle(LOCAL, "key-1", "fp-1", _result())
        outcome, settled = self.receipts.claim(_binding("tok-1"))
        self.assertEqual(outcome, CLAIM_REPLAY)
        self.assertEqual(settled, _result())

    def test_cross_key_same_preview_single_winner(self) -> None:
        self.assertEqual(
            self.receipts.claim(_binding("tok-1", key="key-a")),
            (CLAIM_ADMITTED, None),
        )
        with self.assertRaises(ReceiptConflictError):
            self.receipts.claim(_binding("tok-1", key="key-b"))

    def test_cross_principal_same_preview_conflicts(self) -> None:
        self.receipts.claim(_binding("tok-1", principal=LOCAL))
        with self.assertRaises(ReceiptConflictError):
            self.receipts.claim(_binding("tok-1", principal=OTHER))

    def test_cross_principal_same_key_isolated(self) -> None:
        self.assertEqual(
            self.receipts.claim(_binding("tok-a", principal=LOCAL)),
            (CLAIM_ADMITTED, None),
        )
        self.assertEqual(
            self.receipts.claim(_binding("tok-b", principal=OTHER, key="key-1")),
            (CLAIM_ADMITTED, None),
        )

    def test_release_before_save_frees_key_and_preview(self) -> None:
        self.receipts.claim(_binding("tok-1"))
        self.receipts.release(LOCAL, "key-1", "fp-1")
        self.assertEqual(
            self.receipts.claim(_binding("tok-1")), (CLAIM_ADMITTED, None)
        )

    def test_release_after_settle_retains_reservation(self) -> None:
        self.receipts.claim(_binding("tok-1"))
        self.receipts.settle(LOCAL, "key-1", "fp-1", _result())
        self.receipts.release(LOCAL, "key-1", "fp-1")
        self.assertEqual(self.receipts.lookup(LOCAL, "key-1", "fp-1"), _result())
        with self.assertRaises(ReceiptConflictError):
            self.receipts.claim(_binding("tok-1", key="key-2"))

    def test_release_wrong_fingerprint_conflicts(self) -> None:
        self.receipts.claim(_binding("tok-1", fingerprint="fp-1"))
        with self.assertRaises(ReceiptConflictError):
            self.receipts.release(LOCAL, "key-1", "fp-2")

    def test_release_missing_claim_is_noop(self) -> None:
        self.receipts.release(LOCAL, "key-1", "fp-1")

    def test_settle_without_claim_conflicts(self) -> None:
        with self.assertRaises(ReceiptConflictError):
            self.receipts.settle(LOCAL, "key-1", "fp-1", _result())

    def test_concurrent_claims_admit_exactly_one_writer(self) -> None:
        outcomes: list[str] = []
        lock = threading.Lock()

        def race() -> None:
            store = SQLiteSaveReceiptStore(self.db)
            try:
                outcome, _ = store.claim(_binding("tok-race"))
            except ReceiptConflictError:
                outcome = "conflict"
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=race) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(outcomes), 8)
        self.assertEqual(outcomes.count(CLAIM_ADMITTED), 1)
        rest = [o for o in outcomes if o != CLAIM_ADMITTED]
        self.assertTrue(all(o in (CLAIM_IN_PROGRESS, "conflict") for o in rest))

    def test_concurrent_cross_key_preview_single_winner(self) -> None:
        outcomes: list[str] = []
        lock = threading.Lock()

        def race(index: int) -> None:
            store = SQLiteSaveReceiptStore(self.db)
            try:
                outcome, _ = store.claim(_binding("tok-shared", key=f"key-{index}"))
            except ReceiptConflictError:
                outcome = "conflict"
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=race, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(outcomes.count(CLAIM_ADMITTED), 1)
        self.assertEqual(outcomes.count("conflict"), 7)

    def test_failed_settle_leaves_claim_uncertain(self) -> None:
        self.receipts.claim(_binding("tok-1"))
        bad = _result()
        bad["candidate_raw_toml"] = RAW
        with self.assertRaises(ValueError):
            self.receipts.settle(LOCAL, "key-1", "fp-1", bad)
        self.assertIsNone(self.receipts.lookup(LOCAL, "key-1", "fp-1"))
        self.assertEqual(
            self.receipts.claim(_binding("tok-1")), (CLAIM_IN_PROGRESS, None)
        )

    def test_store_compat_settles_and_replays(self) -> None:
        self.assertEqual(self.receipts.claim(_binding("tok-1"))[0], CLAIM_ADMITTED)
        self.receipts.store(LOCAL, "key-1", "fp-1", _result())
        self.assertEqual(self.receipts.lookup(LOCAL, "key-1", "fp-1"), _result())
        with self.assertRaises(ReceiptConflictError):
            self.receipts.store(LOCAL, "key-1", "fp-2", _result())

    def test_store_without_admitted_claim_rejected(self) -> None:
        with self.assertRaises(ReceiptConflictError):
            self.receipts.store(LOCAL, "key-9", "fp-9", _result())
        self.assertIsNone(self.receipts.lookup(LOCAL, "key-9", "fp-9"))

    def test_store_rejects_raw_and_unknown_fields(self) -> None:
        raw_result = _result()
        raw_result["candidate_raw_toml"] = RAW
        with self.assertRaises(ValueError):
            self.receipts.store(LOCAL, "key-1", "fp-1", raw_result)
        unknown = _result()
        unknown["surprise"] = 1
        with self.assertRaises(ValueError):
            self.receipts.store(LOCAL, "key-1", "fp-1", unknown)
        self.assertIsNone(self.receipts.lookup(LOCAL, "key-1", "fp-1"))

    def test_no_raw_toml_persisted(self) -> None:
        self.receipts.claim(_binding("tok-1"))
        self.receipts.settle(LOCAL, "key-1", "fp-1", _result())
        conn = sqlite3.connect(str(self.db))
        try:
            texts = []
            for table in ("host_save_receipts", "host_preview_reservations"):
                for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                    texts.append(repr(row))
            dump = "\n".join(texts)
        finally:
            conn.close()
        self.assertNotIn("key = 1", dump)
        self.assertNotIn("candidate_raw_toml", dump)
        self.assertIn(_sha(RAW), dump)


class ResultValidationTest(unittest.TestCase):
    def test_accepts_frozen_save_result(self) -> None:
        self.assertEqual(validate_save_result(_result()), _result())

    def test_rejects_unknown_fields(self) -> None:
        bad = _result()
        bad["raw"] = "x"
        with self.assertRaises(ValueError):
            validate_save_result(bad)

    def test_rejects_missing_fields(self) -> None:
        bad = _result()
        del bad["backup"]
        with self.assertRaises(ValueError):
            validate_save_result(bad)


class FakeDocument:
    def __init__(self) -> None:
        self.candidate = {
            "valid": True,
            "validation_level": "schema",
            "candidate_content_hash": _sha(RAW),
            "candidate_raw_toml": RAW,
            "candidate_structured": {"sections": []},
            "diagnostics": [],
            "context_revision": CTX,
        }
        self.saved = _result()

    def validate(self, *args: Any) -> dict[str, Any]:
        return dict(self.candidate)

    def preview(self, *args: Any) -> dict[str, Any]:
        return dict(self.candidate)

    def save(self, *args: Any) -> dict[str, Any]:
        return dict(self.saved)


class ServiceIntegrationTest(_Stores):
    def test_save_replay_after_stale_returns_original(self) -> None:
        doc = FakeDocument()
        service = HostSettingsService(
            doc, self.previews, self.receipts, lambda: "tok-1", LOCAL
        )
        caller = CallerContext(principal=LOCAL, grants=frozenset({READ_GRANT, WRITE_GRANT}))
        params = {
            "host_id": HOST,
            "document_id": DOC,
            "expected_content_hash": _sha(RAW),
            "context_revision": CTX,
            "preview_id": "tok-1",
            "candidate_content_hash": _sha(RAW),
            "candidate_raw_toml": RAW,
            "idempotency_key": "key-1",
        }
        self.previews.admit(_record("tok-1"))
        first = service.save(caller, params)
        self.assertTrue(first["saved"])
        self.assertTrue(self.previews.is_consumed("tok-1"))
        second = service.save(caller, dict(params))
        self.assertEqual(second, first)
        changed = dict(params)
        changed["candidate_raw_toml"] = "key = 2\n"
        changed["candidate_content_hash"] = _sha("key = 2\n")
        changed["idempotency_key"] = "key-2"
        with self.assertRaises(Exception):
            service.save(caller, changed)

class ReceiptBypassTest(_Stores):
    def test_store_without_claim_rejected_and_no_preview_bypass(self) -> None:
        with self.assertRaises(ReceiptConflictError):
            self.receipts.store(LOCAL, "key-a", "fp-a", _result())
        outcome, _ = self.receipts.claim(_binding("prev-bypass", key="key-b", fingerprint="fp-b"))
        self.assertEqual(outcome, CLAIM_ADMITTED)
        with self.assertRaises(ReceiptConflictError):
            self.receipts.claim(_binding("prev-bypass", key="key-a", fingerprint="fp-a"))
        outcome2, _ = self.receipts.claim(_binding("prev-bypass", key="key-b", fingerprint="fp-b"))
        self.assertEqual(outcome2, CLAIM_IN_PROGRESS)

    def test_cross_key_store_cannot_steal_preview(self) -> None:
        outcome, _ = self.receipts.claim(_binding("prev-x", key="key-a", fingerprint="fp-a"))
        self.assertEqual(outcome, CLAIM_ADMITTED)
        with self.assertRaises(ReceiptConflictError):
            self.receipts.claim(_binding("prev-x", key="key-b", fingerprint="fp-b"))

    def test_stored_rows_and_file_dump_contain_no_raw_secret(self) -> None:
        secret = "super-secret-toml-value-xyz"
        outcome, _ = self.receipts.claim(_binding("prev-secret-dump"))
        self.assertEqual(outcome, CLAIM_ADMITTED)
        self.receipts.settle(LOCAL, "key-1", "fp-1", _result())
        self.assertNotIn(secret.encode(), self.db.read_bytes())
        row = sqlite3.connect(str(self.db)).execute(
            "SELECT result_json FROM host_save_receipts"
        ).fetchone()
        assert row is not None
        self.assertNotIn("candidate_raw_toml", row[0])
        self.assertNotIn(secret, row[0])


class ApplicationEffectsValidationTest(unittest.TestCase):
    def test_application_effects_raw_dict_rejected_and_claim_survives(self) -> None:
        bad = _result()
        bad["application_effects"] = [{"effect": "immediate"}]
        with self.assertRaises(ValueError):
            validate_save_result(bad)
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "s.db"
            store = SQLiteSaveReceiptStore(db)
            outcome, _ = store.claim(_binding("prev-raw-effect"))
            self.assertEqual(outcome, CLAIM_ADMITTED)
            with self.assertRaises(ValueError):
                store.settle(LOCAL, "key-1", "fp-1", bad)
            outcome2, _ = store.claim(_binding("prev-raw-effect"))
            self.assertEqual(outcome2, CLAIM_IN_PROGRESS)

    def test_application_effects_rejects_unknown_string(self) -> None:
        bad = _result()
        bad["application_effects"] = ["reboot_everything"]
        with self.assertRaises(ValueError):
            validate_save_result(bad)


if __name__ == "__main__":
    unittest.main()
