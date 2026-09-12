import hashlib
import unittest
from typing import Any

from model_deck.engine.host_settings.ports import (
    READ_GRANT,
    WRITE_GRANT,
    CallerContext,
    PreviewExistsError,
    PreviewRecord,
    ReceiptConflictError,
    SettingsConflictError,
    SettingsDeniedError,
    SettingsExhaustedError,
    SettingsInternalError,
    SettingsInvalidError,
)
from model_deck.engine.host_settings.service import HostSettingsService

LOCAL = "local-operator-principal"
OTHER = "other-principal"
HOST = "com.example.host"
DOC = "ref:codex-user-config"
CTX = "ctx-1"
RAW = "key = 1\n"


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshot() -> dict[str, Any]:
    return {
        "host_id": HOST,
        "document_id": DOC,
        "document_revision": _sha(RAW),
        "exists": True,
        "target": {
            "display_name": "config",
            "display_path": "/tmp/config.toml",
            "scope": "user",
            "writable": True,
        },
        "schema_profile": {
            "schema_id": "codex-settings",
            "schema_revision": "3",
            "host_version": "1.0",
            "support_level": "supported",
        },
        "precedence": [],
        "raw_toml": RAW,
        "structured": {"sections": []},
        "context_revision": CTX,
    }


def _candidate(raw: str = RAW) -> dict[str, Any]:
    return {
        "valid": True,
        "validation_level": "schema",
        "candidate_content_hash": _sha(raw),
        "candidate_raw_toml": raw,
        "candidate_structured": {"sections": []},
        "diagnostics": [],
        "context_revision": CTX,
    }


def _adapter_preview(raw: str = RAW) -> dict[str, Any]:
    base = _candidate(raw)
    base["preview"] = {
        "base_content_hash": _sha(RAW),
        "candidate_content_hash": _sha(raw),
        "changed": raw != RAW,
        "diff": "-a\n+b\n",
        "diff_truncated": False,
        "changed_field_ids": [],
        "application_effects": [],
        "protected_projection_changes": False,
    }
    return base


def _save_result(raw: str = RAW) -> dict[str, Any]:
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


class FakeDocument:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.snapshot = _snapshot()
        self.candidate = _candidate()
        self.preview_payload = _adapter_preview()
        self.saved = _save_result()

    def read(self, host_id: str) -> dict[str, Any]:
        self.calls.append(("read", (host_id,)))
        return self.snapshot

    def validate(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("validate", args))
        return self.candidate

    def preview(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("preview", args))
        return self.preview_payload

    def save(self, *args: Any) -> dict[str, Any]:
        self.calls.append(("save", args))
        return self.saved


class FakePreviews:
    def __init__(self) -> None:
        self.live: dict[str, PreviewRecord] = {}
        self.consumed: set[str] = set()

    def admit(self, record: PreviewRecord) -> None:
        if record.preview_id in self.live or record.preview_id in self.consumed:
            raise PreviewExistsError("duplicate preview token")
        self.live[record.preview_id] = record

    def lookup(self, preview_id: str) -> PreviewRecord | None:
        return self.live.get(preview_id)

    def is_consumed(self, preview_id: str) -> bool:
        return preview_id in self.consumed

    def consume(self, preview_id: str) -> PreviewRecord | None:
        record = self.live.pop(preview_id, None)
        if record is not None:
            self.consumed.add(preview_id)
        return record


class FakeReceipts:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], tuple[str, dict[str, Any] | None, Any]] = {}
        self.preview_index: dict[str, tuple[str, str, str]] = {}

    def lookup(self, principal: str, key: str, fingerprint: str) -> dict[str, Any] | None:
        hit = self.records.get((principal, key))
        if hit is None or hit[0] != fingerprint:
            return None
        if hit[1] is None:
            return None
        return dict(hit[1])

    def store(self, principal: str, key: str, fingerprint: str, result: dict[str, Any]) -> None:
        hit = self.records.get((principal, key))
        if hit is not None and hit[0] != fingerprint:
            raise ReceiptConflictError("idempotency key reuse")
        self.records[(principal, key)] = (fingerprint, dict(result), None)

    def claim(self, binding: Any) -> tuple[str, dict[str, Any] | None]:
        owner = self.preview_index.get(binding.preview_id)
        mine = (binding.principal, binding.idempotency_key, binding.fingerprint)
        if owner is not None and owner != mine:
            raise ReceiptConflictError("preview already reserved")
        hit = self.records.get((binding.principal, binding.idempotency_key))
        if hit is not None and hit[0] != binding.fingerprint:
            raise ReceiptConflictError("idempotency key reuse")
        if hit is None:
            self.records[(binding.principal, binding.idempotency_key)] = (
                binding.fingerprint, None, binding,
            )
            self.preview_index[binding.preview_id] = mine
            return ("admitted", None)
        if hit[1] is not None:
            return ("replay", dict(hit[1]))
        return ("in_progress", None)

    def settle(self, principal: str, key: str, fingerprint: str, result: dict[str, Any]) -> None:
        hit = self.records.get((principal, key))
        if hit is not None and hit[0] != fingerprint:
            raise ReceiptConflictError("idempotency key reuse")
        if hit is None:
            raise ReceiptConflictError("no admitted claim")
        self.records[(principal, key)] = (fingerprint, dict(result), hit[2])

    def release(self, principal: str, key: str, fingerprint: str) -> None:
        hit = self.records.get((principal, key))
        if hit is None:
            return
        if hit[0] != fingerprint:
            raise ReceiptConflictError("idempotency key reuse")
        if hit[1] is not None:
            return
        bound = hit[2]
        preview_id = getattr(bound, "preview_id", None)
        del self.records[(principal, key)]
        if isinstance(preview_id, str) and self.preview_index.get(preview_id) == (principal, key, fingerprint):
            del self.preview_index[preview_id]


def _service(doc: FakeDocument, token: str = "tok-1") -> tuple[HostSettingsService, FakePreviews, FakeReceipts]:
    previews = FakePreviews()
    receipts = FakeReceipts()
    svc = HostSettingsService(doc, previews, receipts, lambda: token, LOCAL)
    return svc, previews, receipts


def _reader() -> CallerContext:
    return CallerContext(principal=LOCAL, grants=frozenset({READ_GRANT}))


def _writer() -> CallerContext:
    return CallerContext(principal=LOCAL, grants=frozenset({READ_GRANT, WRITE_GRANT}))


def _params(preview_id: str, raw: str = RAW) -> dict[str, Any]:
    return {
        "host_id": HOST,
        "document_id": DOC,
        "expected_content_hash": _sha(RAW),
        "context_revision": CTX,
        "preview_id": preview_id,
        "candidate_content_hash": _sha(raw),
        "candidate_raw_toml": raw,
        "idempotency_key": "key-1",
    }


class HostSettingsServiceTest(unittest.TestCase):
    def test_read_ok(self) -> None:
        doc = FakeDocument()
        svc, _, _ = _service(doc)
        result = svc.read(_reader(), {"host_id": HOST})
        self.assertEqual(result["snapshot"]["document_id"], DOC)

    def test_read_denied_principal_skips_port(self) -> None:
        doc = FakeDocument()
        svc, _, _ = _service(doc)
        caller = CallerContext(principal=OTHER, grants=frozenset({READ_GRANT}))
        with self.assertRaises(SettingsDeniedError):
            svc.read(caller, {"host_id": HOST})
        self.assertEqual(doc.calls, [])

    def test_read_denied_missing_grant(self) -> None:
        doc = FakeDocument()
        svc, _, _ = _service(doc)
        caller = CallerContext(principal=LOCAL, grants=frozenset())
        with self.assertRaises(SettingsDeniedError):
            svc.read(caller, {"host_id": HOST})
        self.assertEqual(doc.calls, [])

    def test_validate_raw_ok(self) -> None:
        doc = FakeDocument()
        svc, _, _ = _service(doc)
        result = svc.validate(
            _reader(),
            {
                "host_id": HOST,
                "document_id": DOC,
                "expected_content_hash": _sha(RAW),
                "context_revision": CTX,
                "draft": {"kind": "raw", "raw_toml": RAW},
            },
        )
        self.assertTrue(result["valid"])

    def test_preview_then_save_ok(self) -> None:
        doc = FakeDocument()
        svc, previews, _ = _service(doc)
        previewed = svc.preview(
            _reader(),
            {
                "host_id": HOST,
                "document_id": DOC,
                "expected_content_hash": _sha(RAW),
                "context_revision": CTX,
                "draft": {"kind": "raw", "raw_toml": RAW},
            },
        )
        self.assertEqual(previewed["preview"]["preview_id"], "tok-1")
        saved = svc.save(_writer(), _params("tok-1"))
        self.assertTrue(saved["saved"])
        self.assertTrue(previews.is_consumed("tok-1"))

    def test_save_tampered_candidate_conflicts(self) -> None:
        doc = FakeDocument()
        svc, _, _ = _service(doc)
        svc.preview(
            _reader(),
            {
                "host_id": HOST,
                "document_id": DOC,
                "expected_content_hash": _sha(RAW),
                "context_revision": CTX,
                "draft": {"kind": "raw", "raw_toml": RAW},
            },
        )
        tampered = _params("tok-1", raw="key = 2\n")
        tampered["candidate_content_hash"] = _sha(RAW)
        with self.assertRaises(SettingsConflictError):
            svc.save(_writer(), tampered)

    def test_save_replay_after_stale_returns_original(self) -> None:
        doc = FakeDocument()
        svc, previews, _ = _service(doc)
        svc.preview(
            _reader(),
            {
                "host_id": HOST,
                "document_id": DOC,
                "expected_content_hash": _sha(RAW),
                "context_revision": CTX,
                "draft": {"kind": "raw", "raw_toml": RAW},
            },
        )
        first = svc.save(_writer(), _params("tok-1"))
        second = svc.save(_writer(), _params("tok-1"))
        self.assertEqual(first, second)
        self.assertTrue(previews.is_consumed("tok-1"))

    def test_save_consumed_preview_new_key_conflicts(self) -> None:
        doc = FakeDocument()
        svc, _, _ = _service(doc)
        svc.preview(
            _reader(),
            {
                "host_id": HOST,
                "document_id": DOC,
                "expected_content_hash": _sha(RAW),
                "context_revision": CTX,
                "draft": {"kind": "raw", "raw_toml": RAW},
            },
        )
        svc.save(_writer(), _params("tok-1"))
        retry = _params("tok-1")
        retry["idempotency_key"] = "key-2"
        with self.assertRaises(SettingsConflictError) as ctx:
            svc.save(_writer(), retry)
        self.assertEqual(ctx.exception.reason, "preview_consumed")

    def test_save_same_key_changed_request_conflicts(self) -> None:
        doc = FakeDocument()
        svc, _, _ = _service(doc)
        svc.preview(
            _reader(),
            {
                "host_id": HOST,
                "document_id": DOC,
                "expected_content_hash": _sha(RAW),
                "context_revision": CTX,
                "draft": {"kind": "raw", "raw_toml": RAW},
            },
        )
        svc.save(_writer(), _params("tok-1"))
        changed = _params("tok-1", raw="key = 3\n")
        changed["candidate_content_hash"] = _sha("key = 3\n")
        with self.assertRaises(SettingsConflictError):
            svc.save(_writer(), changed)

    def test_save_unknown_preview_rejected(self) -> None:
        doc = FakeDocument()
        svc, _, _ = _service(doc)
        with self.assertRaises(SettingsInvalidError):
            svc.save(_writer(), _params("tok-missing"))
        self.assertEqual([c for c in doc.calls if c[0] == "save"], [])

    def test_schema_invalid_adapter_is_internal(self) -> None:
        doc = FakeDocument()
        doc.snapshot = {"host_id": HOST}
        svc, _, _ = _service(doc)
        with self.assertRaises(SettingsInternalError):
            svc.read(_reader(), {"host_id": HOST})

    def test_oversize_source_rejected_before_port(self) -> None:
        doc = FakeDocument()
        svc, _, _ = _service(doc)
        with self.assertRaises(SettingsExhaustedError):
            svc.validate(
                _reader(),
                {
                    "host_id": HOST,
                    "document_id": DOC,
                    "expected_content_hash": _sha(RAW),
                    "context_revision": CTX,
                    "draft": {"kind": "raw", "raw_toml": "x" * (262144 + 1)},
                },
            )
        self.assertEqual(doc.calls, [])

    def test_oversize_adapter_diff_is_internal(self) -> None:
        doc = FakeDocument()
        payload = _adapter_preview()
        payload["preview"]["diff"] = "x" * (131072 + 1)
        doc.preview_payload = payload
        svc, _, _ = _service(doc)
        with self.assertRaises(SettingsInternalError):
            svc.preview(
                _reader(),
                {
                    "host_id": HOST,
                    "document_id": DOC,
                    "expected_content_hash": _sha(RAW),
                    "context_revision": CTX,
                    "draft": {"kind": "raw", "raw_toml": RAW},
                },
            )



    def test_adapter_exception_leaks_no_secret(self) -> None:
        secret = "super-secret-toml-value-9f3k"
        class LeakyDocument(FakeDocument):
            def validate(self, *args: Any) -> dict[str, Any]:
                raise ValueError("adapter blew up on " + secret)
        doc = LeakyDocument()
        svc, _, _ = _service(doc)
        with self.assertRaises(SettingsInternalError) as ctx:
            svc.validate(
                _reader(),
                {
                    "host_id": HOST,
                    "document_id": DOC,
                    "expected_content_hash": _sha(RAW),
                    "context_revision": CTX,
                    "draft": {"kind": "raw", "raw_toml": RAW},
                },
            )
        self.assertNotIn(secret, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)

    def test_schema_exception_leaks_no_secret(self) -> None:
        from model_deck_contracts.validator import SchemaValidationError
        secret = "schema-secret-77zz"
        class LeakySchemaDocument(FakeDocument):
            def read(self, host_id: str) -> dict[str, Any]:
                raise SchemaValidationError("bad " + secret)
        doc = LeakySchemaDocument()
        svc, _, _ = _service(doc)
        with self.assertRaises(SettingsInternalError) as ctx:
            svc.read(_reader(), {"host_id": HOST})
        self.assertNotIn(secret, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)

    def test_read_oversize_snapshot_rejected(self) -> None:
        doc = FakeDocument()
        snap = _snapshot()
        snap["raw_toml"] = "x" * (262144 + 2)
        doc.snapshot = snap
        svc, _, _ = _service(doc)
        with self.assertRaises(SettingsExhaustedError):
            svc.read(_reader(), {"host_id": HOST})

    def test_read_descriptor_flood_rejected(self) -> None:
        doc = FakeDocument()
        snap = _snapshot()
        snap["structured"] = {
            "sections": [
                {"fields": [{"id": f"f-{i}"} for i in range(200)]},
                {"fields": [{"id": f"g-{i}"} for i in range(57)]},
            ]
        }
        doc.snapshot = snap
        svc, _, _ = _service(doc)
        with self.assertRaises(SettingsInternalError):
            svc.read(_reader(), {"host_id": HOST})

    def test_receipt_store_failure_surfaces_uncertainty(self) -> None:
        doc = FakeDocument()
        svc, previews, receipts = _service(doc)
        svc.preview(
            _reader(),
            {
                "host_id": HOST,
                "document_id": DOC,
                "expected_content_hash": _sha(RAW),
                "context_revision": CTX,
                "draft": {"kind": "raw", "raw_toml": RAW},
            },
        )
        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("ledger disk on fire super-secret")
        receipts.settle = boom  # type: ignore[method-assign]
        with self.assertRaises(SettingsConflictError) as ctx:
            svc.save(_writer(), _params("tok-1"))
        self.assertNotIn("super-secret", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertIsNone(receipts.lookup(LOCAL, "key-1", "x"))
        self.assertFalse(previews.is_consumed("tok-1"))
        saves = [c for c in doc.calls if c[0] == "save"]
        self.assertEqual(len(saves), 1)

    def test_preview_ledger_failure_sanitized(self) -> None:
        doc = FakeDocument()
        svc, previews, _ = _service(doc)
        def boom_lookup(preview_id: str) -> None:
            raise RuntimeError("ledger leak super-secret-ledger")
        previews.lookup = boom_lookup  # type: ignore[method-assign]
        with self.assertRaises(SettingsInternalError) as ctx:
            svc.save(_writer(), _params("tok-1"))
        self.assertNotIn("super-secret-ledger", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertEqual([c for c in doc.calls if c[0] == "save"], [])

    def test_settle_failure_retry_writes_once(self) -> None:
        doc = FakeDocument()
        svc, previews, receipts = _service(doc)
        svc.preview(
            _reader(),
            {
                "host_id": HOST,
                "document_id": DOC,
                "expected_content_hash": _sha(RAW),
                "context_revision": CTX,
                "draft": {"kind": "raw", "raw_toml": RAW},
            },
        )
        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("settle disk on fire")
        receipts.settle = boom  # type: ignore[method-assign]
        with self.assertRaises(SettingsConflictError):
            svc.save(_writer(), _params("tok-1"))
        with self.assertRaises(SettingsConflictError):
            svc.save(_writer(), _params("tok-1"))
        saves = [c for c in doc.calls if c[0] == "save"]
        self.assertEqual(len(saves), 1)
        self.assertFalse(previews.is_consumed("tok-1"))

    def test_concurrent_same_key_second_gets_in_progress(self) -> None:
        doc = FakeDocument()
        svc, _, receipts = _service(doc)
        svc.preview(
            _reader(),
            {
                "host_id": HOST,
                "document_id": DOC,
                "expected_content_hash": _sha(RAW),
                "context_revision": CTX,
                "draft": {"kind": "raw", "raw_toml": RAW},
            },
        )
        from model_deck.engine.host_settings.ports import SaveClaimBinding
        fp = svc._fingerprint(_params("tok-1"))
        binding = SaveClaimBinding(
            principal=LOCAL,
            idempotency_key="key-1",
            fingerprint=fp,
            host_id=HOST,
            document_id=DOC,
            base_content_hash=_sha(RAW),
            context_revision=CTX,
            candidate_content_hash=_sha(RAW),
            preview_id="tok-1",
        )
        outcome, _ = receipts.claim(binding)
        self.assertEqual(outcome, "admitted")
        with self.assertRaises(SettingsConflictError):
            svc.save(_writer(), _params("tok-1"))
        saves = [c for c in doc.calls if c[0] == "save"]
        self.assertEqual(len(saves), 0)

    def test_prewrite_validation_failure_releases_claim(self) -> None:
        doc = FakeDocument()
        doc.candidate = dict(_candidate())
        doc.candidate["valid"] = False
        svc, previews, receipts = _service(doc)
        svc.preview(
            _reader(),
            {
                "host_id": HOST,
                "document_id": DOC,
                "expected_content_hash": _sha(RAW),
                "context_revision": CTX,
                "draft": {"kind": "raw", "raw_toml": RAW},
            },
        )
        with self.assertRaises(SettingsInvalidError):
            svc.save(_writer(), _params("tok-1"))
        self.assertEqual([c for c in doc.calls if c[0] == "save"], [])
        doc.candidate = _candidate()
        saved = svc.save(_writer(), _params("tok-1"))
        self.assertTrue(saved["saved"])

    def test_binding_persists_no_raw_toml(self) -> None:
        doc = FakeDocument()
        svc, _, receipts = _service(doc)
        svc.preview(
            _reader(),
            {
                "host_id": HOST,
                "document_id": DOC,
                "expected_content_hash": _sha(RAW),
                "context_revision": CTX,
                "draft": {"kind": "raw", "raw_toml": RAW},
            },
        )
        svc.save(_writer(), _params("tok-1"))
        stored = receipts.records[(LOCAL, "key-1")][2]
        self.assertEqual(stored.candidate_content_hash, _sha(RAW))
        self.assertNotIn(RAW, repr(stored))

    def test_same_preview_different_keys_single_write(self) -> None:
        svc, _, _ = _service(FakeDocument(), token="tok-1")
        svc.preview(
            _reader(),
            {"host_id": HOST, "document_id": DOC, "expected_content_hash": _sha(RAW),
             "context_revision": CTX, "draft": {"kind": "raw", "raw_toml": RAW}},
        )
        from model_deck.engine.host_settings.ports import SaveClaimBinding
        fp1 = svc._fingerprint(_params("tok-1"))
        retry_params = _params("tok-1")
        retry_params["idempotency_key"] = "key-2"
        fp2 = svc._fingerprint(retry_params)
        import hashlib as _hl
        b1 = SaveClaimBinding(principal=LOCAL, idempotency_key="key-1", fingerprint=fp1,
            host_id=HOST, document_id=DOC, base_content_hash=_sha(RAW),
            context_revision=CTX, candidate_content_hash=_sha(RAW), preview_id="tok-1")
        b2 = SaveClaimBinding(principal=LOCAL, idempotency_key="key-2", fingerprint=fp2,
            host_id=HOST, document_id=DOC, base_content_hash=_sha(RAW),
            context_revision=CTX, candidate_content_hash=_sha(RAW), preview_id="tok-1")
        from model_deck.engine.host_settings import service as _sm
        r1_docs, r1_prev, r1_rec = FakeDocument(), FakePreviews(), FakeReceipts()
        from model_deck.engine.host_settings.ports import PreviewRecord
        r1_prev.admit(PreviewRecord(preview_id="tok-1", principal=LOCAL, host_id=HOST,
            document_id=DOC, base_content_hash=_sha(RAW), context_revision=CTX,
            candidate_content_hash=_sha(RAW)))
        self.assertEqual(r1_rec.claim(b1), ("admitted", None))
        with self.assertRaises(ReceiptConflictError):
            r1_rec.claim(b2)
        first = svc.save(_writer(), _params("tok-1"))
        self.assertTrue(first["saved"])
        retry = _params("tok-1")
        retry["idempotency_key"] = "key-2"
        with self.assertRaises(SettingsConflictError):
            svc.save(_writer(), retry)

    def test_prewrite_adapter_error_releases_claim(self) -> None:
        doc = FakeDocument()
        svc, _, receipts = _service(doc)
        svc.preview(
            _reader(),
            {"host_id": HOST, "document_id": DOC, "expected_content_hash": _sha(RAW),
             "context_revision": CTX, "draft": {"kind": "raw", "raw_toml": RAW}},
        )
        orig_validate = doc.validate
        def boom(*args: Any) -> dict[str, Any]:
            raise RuntimeError("prewrite adapter blew up")
        doc.validate = boom  # type: ignore[method-assign]
        with self.assertRaises(SettingsInternalError) as ctx:
            svc.save(_writer(), _params("tok-1"))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertEqual([c for c in doc.calls if c[0] == "save"], [])
        doc.validate = orig_validate  # type: ignore[method-assign]
        doc.candidate = _candidate()
        saved = svc.save(_writer(), _params("tok-1"))
        self.assertTrue(saved["saved"])
        self.assertEqual(len([c for c in doc.calls if c[0] == "save"]), 1)

    def test_typed_adapter_error_text_sanitized(self) -> None:
        secret = "typed-secret-abc123"
        class TypedLeaky(FakeDocument):
            def validate(self, *args: Any) -> dict[str, Any]:
                raise SettingsInvalidError("leaky " + secret)
        doc = TypedLeaky()
        svc, _, _ = _service(doc)
        with self.assertRaises(SettingsInvalidError) as ctx:
            svc.validate(
                _reader(),
                {"host_id": HOST, "document_id": DOC, "expected_content_hash": _sha(RAW),
                 "context_revision": CTX, "draft": {"kind": "raw", "raw_toml": RAW}},
            )
        self.assertNotIn(secret, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)

    def test_prewrite_none_output_releases_claim_no_attr_leak(self) -> None:
        doc = FakeDocument()
        svc, _, receipts = _service(doc)
        svc.preview(
            _reader(),
            {"host_id": HOST, "document_id": DOC, "expected_content_hash": _sha(RAW),
             "context_revision": CTX, "draft": {"kind": "raw", "raw_toml": RAW}},
        )
        doc.candidate = None  # type: ignore[assignment]
        with self.assertRaises(SettingsInternalError) as ctx:
            svc.save(_writer(), _params("tok-1"))
        self.assertNotIn("NoneType", str(ctx.exception))
        self.assertNotIn("get", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertEqual([c for c in doc.calls if c[0] == "save"], [])
        doc.candidate = _candidate()
        saved = svc.save(_writer(), _params("tok-1"))
        self.assertTrue(saved["saved"])
        self.assertEqual(len([c for c in doc.calls if c[0] == "save"]), 1)

    def test_prewrite_wrong_type_output_releases_claim(self) -> None:
        doc = FakeDocument()
        svc, _, receipts = _service(doc)
        svc.preview(
            _reader(),
            {"host_id": HOST, "document_id": DOC, "expected_content_hash": _sha(RAW),
             "context_revision": CTX, "draft": {"kind": "raw", "raw_toml": RAW}},
        )
        doc.candidate = ["not", "a", "mapping"]  # type: ignore[assignment]
        with self.assertRaises(SettingsInternalError):
            svc.save(_writer(), _params("tok-1"))
        self.assertEqual([c for c in doc.calls if c[0] == "save"], [])
        doc.candidate = _candidate()
        saved = svc.save(_writer(), _params("tok-1"))
        self.assertTrue(saved["saved"])
        self.assertEqual(len([c for c in doc.calls if c[0] == "save"]), 1)

if __name__ == "__main__":
    unittest.main()
