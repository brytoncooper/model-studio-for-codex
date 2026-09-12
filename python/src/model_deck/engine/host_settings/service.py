from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

from model_deck.engine.host_settings.ports import (
    CLAIM_ADMITTED,
    CLAIM_IN_PROGRESS,
    CLAIM_REPLAY,
    READ_GRANT,
    WRITE_GRANT,
    CallerContext,
    SaveClaimBinding,
    PreviewExistsError,
    PreviewRecord,
    PreviewStorePort,
    ReceiptConflictError,
    SaveReceiptStorePort,
    SettingsConflictError,
    SettingsDeniedError,
    SettingsDocumentPort,
    SettingsExhaustedError,
    SettingsInternalError,
    SettingsInvalidError,
    SettingsNotFoundError,
    SettingsUnsupportedError,
    SettingsVersionMismatchError,
    TokenFactory,
)

READ_PARAMS = "contracts/engine.v1/methods/hosts.settings.read.params.schema.json"
READ_RESULT = "contracts/engine.v1/methods/hosts.settings.read.result.schema.json"
VALIDATE_PARAMS = "contracts/engine.v1/methods/hosts.settings.validate.params.schema.json"
VALIDATE_RESULT = "contracts/engine.v1/methods/hosts.settings.validate.result.schema.json"
PREVIEW_PARAMS = "contracts/engine.v1/methods/hosts.settings.preview.params.schema.json"
PREVIEW_RESULT = "contracts/engine.v1/methods/hosts.settings.preview.result.schema.json"
SAVE_PARAMS = "contracts/engine.v1/methods/hosts.settings.save.params.schema.json"
SAVE_RESULT = "contracts/engine.v1/methods/hosts.settings.save.result.schema.json"

RAW_TOML_UTF8_LIMIT = 262144
DIFF_UTF8_LIMIT = 131072
FRAME_UTF8_LIMIT = 1048576
MAX_DESCRIPTORS = 256
MAX_ENTRIES = 256
MAX_ENTRY_DEPTH = 4
PREVIEW_TOKEN_ATTEMPTS = 3

_SAFE_ERRORS = (
    SettingsDeniedError,
    SettingsNotFoundError,
    SettingsConflictError,
    SettingsVersionMismatchError,
    SettingsUnsupportedError,
    SettingsExhaustedError,
    SettingsInvalidError,
    SettingsInternalError,
)


class HostSettingsService:
    """Generic settings engine: auth, bounds, tokens, receipts; adapter owns files."""

    def __init__(
        self,
        document: SettingsDocumentPort,
        previews: PreviewStorePort,
        receipts: SaveReceiptStorePort,
        new_preview_id: TokenFactory,
        local_operator_principal: str,
    ) -> None:
        self._document = document
        self._previews = previews
        self._receipts = receipts
        self._new_preview_id = new_preview_id
        self._local_operator_principal = local_operator_principal

    def read(self, caller: CallerContext, params: Mapping[str, Any]) -> dict[str, Any]:
        self._require_grant(caller, READ_GRANT)
        body = self._checked_params(READ_PARAMS, params)
        snapshot = self._call_adapter("read", self._document.read, body["host_id"])
        self._check_snapshot_shape(snapshot)
        return self._checked_result(READ_RESULT, {"snapshot": snapshot})

    def validate(self, caller: CallerContext, params: Mapping[str, Any]) -> dict[str, Any]:
        self._require_grant(caller, READ_GRANT)
        self._precheck_source_bounds(params)
        body = self._checked_params(VALIDATE_PARAMS, params)
        self._check_draft_source(body["draft"])
        candidate = self._call_adapter(
            "validate",
            self._document.validate,
            body["host_id"],
            body["document_id"],
            body["expected_content_hash"],
            body["context_revision"],
            body["draft"],
        )
        self._check_candidate_shape(candidate)
        if candidate.get("valid") is True:
            self._verify_candidate_hash(candidate)
        return self._checked_result(VALIDATE_RESULT, candidate)

    def preview(self, caller: CallerContext, params: Mapping[str, Any]) -> dict[str, Any]:
        self._require_grant(caller, READ_GRANT)
        self._precheck_source_bounds(params)
        body = self._checked_params(PREVIEW_PARAMS, params)
        self._check_draft_source(body["draft"])
        candidate = self._call_adapter(
            "preview",
            self._document.preview,
            body["host_id"],
            body["document_id"],
            body["expected_content_hash"],
            body["context_revision"],
            body["draft"],
        )
        self._check_candidate_shape(candidate)
        if candidate.get("valid") is not True:
            candidate["preview"] = None
            return self._checked_result(PREVIEW_RESULT, candidate)
        self._verify_candidate_hash(candidate)
        issued = self._issue_preview(
            caller,
            host_id=body["host_id"],
            document_id=body["document_id"],
            base_content_hash=body["expected_content_hash"],
            context_revision=candidate.get("context_revision", body["context_revision"]),
            candidate_content_hash=candidate["candidate_content_hash"],
        )
        adapter_preview = candidate.get("preview")
        if not isinstance(adapter_preview, dict):
            raise SettingsInternalError("settings preview unavailable") from None
        self._check_diff_bytes(adapter_preview.get("diff"))
        candidate["preview"] = {**adapter_preview, "preview_id": issued.preview_id}
        return self._checked_result(PREVIEW_RESULT, candidate)

    def save(self, caller: CallerContext, params: Mapping[str, Any]) -> dict[str, Any]:
        self._require_grant(caller, WRITE_GRANT)
        self._precheck_source_bounds(params)
        body = self._checked_params(SAVE_PARAMS, params)
        self._check_source_bytes(body["candidate_raw_toml"])
        fingerprint = self._fingerprint(body)
        try:
            replayed = self._receipts.lookup(
                caller.principal, body["idempotency_key"], fingerprint
            )
        except Exception:
            raise SettingsInternalError(
                "settings receipt ledger unavailable"
            ) from None
        if replayed is not None:
            return self._checked_result(SAVE_RESULT, dict(replayed))
        record = self._live_preview(body["preview_id"])
        self._check_preview_binding(caller, record, body)
        self._verify_save_hash(body)
        binding = SaveClaimBinding(
            principal=caller.principal,
            idempotency_key=body["idempotency_key"],
            fingerprint=fingerprint,
            host_id=body["host_id"],
            document_id=body["document_id"],
            base_content_hash=body["expected_content_hash"],
            context_revision=body["context_revision"],
            candidate_content_hash=body["candidate_content_hash"],
            preview_id=record.preview_id,
        )
        try:
            outcome, settled = self._receipts.claim(binding)
        except ReceiptConflictError:
            raise SettingsConflictError(
                "idempotency key already used for a different save",
                reason="conflict",
            ) from None
        except Exception:
            raise SettingsInternalError(
                "settings receipt ledger unavailable"
            ) from None
        if outcome == CLAIM_REPLAY:
            if settled is None:
                raise SettingsInternalError(
                    "settings receipt unavailable"
                ) from None
            return self._checked_result(SAVE_RESULT, dict(settled))
        if outcome == CLAIM_IN_PROGRESS:
            raise SettingsConflictError(
                "settings save already in progress; retry with same key",
                reason="conflict",
            ) from None
        if outcome != CLAIM_ADMITTED:
            raise SettingsInternalError(
                "settings receipt unavailable"
            ) from None
        try:
            revalidated = self._call_adapter(
                "validate",
                self._document.validate,
                body["host_id"],
                body["document_id"],
                body["expected_content_hash"],
                body["context_revision"],
                {"kind": "raw", "raw_toml": body["candidate_raw_toml"]},
            )
            if not isinstance(revalidated, Mapping):
                raise SettingsInternalError("settings result unavailable") from None
            self._check_candidate_shape(revalidated)
            if revalidated.get("valid") is not True:
                raise SettingsInvalidError("settings candidate failed revalidation") from None
            self._verify_candidate_hash(revalidated)
        except Exception as pre_exc:
            try:
                self._receipts.release(
                    caller.principal, body["idempotency_key"], fingerprint
                )
            except Exception:
                raise SettingsConflictError(
                    "settings save outcome uncertain; retry with same key",
                    reason="conflict",
                ) from None
            if isinstance(pre_exc, _SAFE_ERRORS):
                raise
            raise SettingsInternalError("settings result unavailable") from None
        try:
            saved = self._document.save(
                body["host_id"],
                body["document_id"],
                body["expected_content_hash"],
                body["context_revision"],
                body["candidate_content_hash"],
                body["candidate_raw_toml"],
            )
        except _SAFE_ERRORS:
            raise SettingsConflictError(
                "settings save outcome uncertain; retry with same key",
                reason="conflict",
            ) from None
        except Exception:
            raise SettingsConflictError(
                "settings save outcome uncertain; retry with same key",
                reason="conflict",
            ) from None
        result = self._checked_result(SAVE_RESULT, saved)
        try:
            self._receipts.settle(
                caller.principal, body["idempotency_key"], fingerprint, result
            )
        except ReceiptConflictError:
            raise SettingsConflictError(
                "idempotency key already used for a different save",
                reason="conflict",
            ) from None
        except Exception:
            raise SettingsConflictError(
                "settings save outcome uncertain; retry with same key",
                reason="conflict",
            ) from None
        try:
            self._previews.consume(record.preview_id)
        except Exception:
            raise SettingsInternalError(
                "settings preview ledger unavailable"
            ) from None
        return result

    def _require_grant(self, caller: CallerContext, grant: str) -> None:
        if caller.principal != self._local_operator_principal:
            raise SettingsDeniedError("local operator authorization required") from None
        if grant not in caller.grants:
            raise SettingsDeniedError("required grant not held") from None

    def _checked_params(self, schema: str, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            body = json.loads(json.dumps(dict(params)))
        except (TypeError, ValueError):
            raise SettingsInvalidError("settings request envelope rejected") from None
        try:
            validate_schema_ref(schema, body)
        except SchemaValidationError:
            raise SettingsInvalidError("settings request envelope rejected") from None
        return body

    def _checked_result(self, schema: str, result: Mapping[str, Any]) -> dict[str, Any]:
        try:
            body = json.loads(json.dumps(dict(result)))
        except (TypeError, ValueError):
            raise SettingsInternalError("settings result unavailable") from None
        try:
            validate_schema_ref(schema, body)
        except SchemaValidationError:
            raise SettingsInternalError("settings result unavailable") from None
        if len(json.dumps(body).encode("utf-8")) > FRAME_UTF8_LIMIT:
            raise SettingsExhaustedError("settings result exceeds frame bound") from None
        return body

    def _call_adapter(self, operation: str, func: Any, *args: Any) -> Any:
        try:
            return func(*args)
        except SettingsDeniedError:
            raise SettingsDeniedError("local operator authorization required") from None
        except SettingsNotFoundError:
            raise SettingsNotFoundError("settings document not found") from None
        except SettingsConflictError:
            raise SettingsConflictError(
                "settings adapter reported a conflict", reason="conflict"
            ) from None
        except SettingsVersionMismatchError:
            raise SettingsVersionMismatchError(
                "settings version no longer applies"
            ) from None
        except SettingsUnsupportedError:
            raise SettingsUnsupportedError("settings capability unsupported") from None
        except SettingsExhaustedError:
            raise SettingsExhaustedError("settings result exceeds bound") from None
        except SettingsInvalidError:
            raise SettingsInvalidError("settings adapter rejected the call") from None
        except SettingsInternalError:
            raise SettingsInternalError("settings adapter unavailable") from None
        except (ValueError, LookupError, PermissionError):
            raise SettingsInternalError("settings adapter rejected the call") from None
        except Exception:
            raise SettingsInternalError("settings adapter unavailable") from None

    def _precheck_source_bounds(self, params: Mapping[str, Any]) -> None:
        try:
            draft = params.get("draft")  # type: ignore[union-attr]
        except AttributeError:
            draft = None
        if isinstance(draft, dict) and draft.get("kind") == "raw":
            raw = draft.get("raw_toml")
            if isinstance(raw, str) and len(raw.encode("utf-8")) > RAW_TOML_UTF8_LIMIT:
                raise SettingsExhaustedError("settings source exceeds size bound") from None
        try:
            candidate = params.get("candidate_raw_toml")  # type: ignore[union-attr]
        except AttributeError:
            candidate = None
        if isinstance(candidate, str) and len(candidate.encode("utf-8")) > RAW_TOML_UTF8_LIMIT:
            raise SettingsExhaustedError("settings source exceeds size bound") from None

    def _check_draft_source(self, draft: Mapping[str, Any]) -> None:
        if draft.get("kind") == "raw":
            raw = draft.get("raw_toml")
            if not isinstance(raw, str):
                raise SettingsInvalidError("settings request envelope rejected") from None
            self._check_source_bytes(raw)

    def _check_source_bytes(self, raw_toml: Any) -> None:
        if not isinstance(raw_toml, str):
            raise SettingsInvalidError("settings request envelope rejected") from None
        if len(raw_toml.encode("utf-8")) > RAW_TOML_UTF8_LIMIT:
            raise SettingsExhaustedError("settings source exceeds size bound") from None

    def _check_diff_bytes(self, diff: Any) -> None:
        if not isinstance(diff, str):
            raise SettingsInternalError("settings preview unavailable") from None
        if len(diff.encode("utf-8")) > DIFF_UTF8_LIMIT:
            raise SettingsInternalError("settings preview unavailable") from None

    def _check_snapshot_shape(self, snapshot: Any) -> None:
        if not isinstance(snapshot, dict):
            raise SettingsInternalError("settings result unavailable") from None
        raw = snapshot.get("raw_toml")
        if not isinstance(raw, str):
            raise SettingsInternalError("settings result unavailable") from None
        self._check_source_bytes(raw)
        self._check_structured_bounds(snapshot.get("structured"))

    def _check_candidate_shape(self, candidate: Any) -> None:
        if not isinstance(candidate, dict):
            raise SettingsInternalError("settings result unavailable") from None
        self._check_structured_bounds(candidate.get("candidate_structured"))

    def _check_structured_bounds(self, structured: Any) -> None:
        if structured is None:
            return
        if not isinstance(structured, dict):
            raise SettingsInternalError("settings result unavailable") from None
        sections = structured.get("sections", [])
        if not isinstance(sections, list):
            raise SettingsInternalError("settings result unavailable") from None
        descriptors = 0
        entries = 0
        for section in sections:
            if not isinstance(section, dict):
                raise SettingsInternalError("settings result unavailable") from None
            fields = section.get("fields", [])
            if not isinstance(fields, list):
                raise SettingsInternalError("settings result unavailable") from None
            for field in fields:
                descriptors += 1
                if descriptors > MAX_DESCRIPTORS:
                    raise SettingsInternalError("settings result unavailable") from None
                entries += self._count_entries(field, 1)
                if entries > MAX_ENTRIES:
                    raise SettingsInternalError("settings result unavailable") from None

    def _count_entries(self, field: Any, depth: int) -> int:
        if not isinstance(field, dict):
            return 0
        nested = field.get("entries")
        if nested is None:
            return 0
        if depth > MAX_ENTRY_DEPTH:
            raise SettingsInternalError("settings result unavailable") from None
        if not isinstance(nested, list):
            raise SettingsInternalError("settings result unavailable") from None
        total = len(nested)
        for entry in nested:
            if not isinstance(entry, dict):
                raise SettingsInternalError("settings result unavailable") from None
            for child in entry.get("fields", []):
                total += 1
                total += self._count_entries(child, depth + 1)
        return total

    def _verify_candidate_hash(self, candidate: Mapping[str, Any]) -> None:
        try:
            claimed = candidate["candidate_content_hash"]
            raw = candidate["candidate_raw_toml"]
        except KeyError:
            raise SettingsInternalError("settings result unavailable") from None
        if not isinstance(claimed, str) or not isinstance(raw, str):
            raise SettingsInternalError("settings result unavailable") from None
        recomputed = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if recomputed != claimed:
            raise SettingsInternalError("settings result unavailable") from None

    def _issue_preview(
        self,
        caller: CallerContext,
        *,
        host_id: str,
        document_id: str,
        base_content_hash: str,
        context_revision: str,
        candidate_content_hash: str,
    ) -> PreviewRecord:
        last_error: Exception | None = None
        for _ in range(PREVIEW_TOKEN_ATTEMPTS):
            try:
                preview_id = self._new_preview_id()
            except Exception as exc:
                raise SettingsInternalError("settings preview unavailable") from None
            if not isinstance(preview_id, str) or not preview_id:
                raise SettingsInternalError("settings preview unavailable") from None
            record = PreviewRecord(
                preview_id=preview_id,
                principal=caller.principal,
                host_id=host_id,
                document_id=document_id,
                base_content_hash=base_content_hash,
                context_revision=context_revision,
                candidate_content_hash=candidate_content_hash,
            )
            try:
                self._previews.admit(record)
            except PreviewExistsError as exc:
                last_error = exc
                continue
            except Exception as exc:
                raise SettingsInternalError(
                    "settings preview ledger unavailable"
                ) from None
            return record
        raise SettingsInternalError("settings preview unavailable") from None

    def _live_preview(self, preview_id: Any) -> PreviewRecord:
        if not isinstance(preview_id, str) or not preview_id:
            raise SettingsInvalidError("settings preview reference rejected") from None
        try:
            record = self._previews.lookup(preview_id)
        except Exception:
            raise SettingsInternalError(
                "settings preview ledger unavailable"
            ) from None
        if record is not None:
            return record
        try:
            consumed = self._previews.is_consumed(preview_id)
        except Exception:
            raise SettingsInternalError(
                "settings preview ledger unavailable"
            ) from None
        if consumed:
            raise SettingsConflictError("settings preview already consumed", reason="preview_consumed") from None
        raise SettingsInvalidError("settings preview reference rejected") from None

    def _check_preview_binding(self, caller: CallerContext, record: PreviewRecord, body: Mapping[str, Any]) -> None:
        if record.principal != caller.principal:
            raise SettingsInvalidError("settings preview reference rejected") from None
        if record.host_id != body["host_id"] or record.document_id != body["document_id"]:
            raise SettingsConflictError("settings preview does not match request", reason="conflict") from None
        if record.base_content_hash != body["expected_content_hash"]:
            raise SettingsConflictError("settings document changed since preview", reason="conflict") from None
        if record.context_revision != body["context_revision"]:
            raise SettingsConflictError("settings context changed since preview", reason="conflict") from None
        if record.candidate_content_hash != body["candidate_content_hash"]:
            raise SettingsConflictError("settings candidate does not match preview", reason="conflict") from None

    def _verify_save_hash(self, body: Mapping[str, Any]) -> None:
        recomputed = "sha256:" + hashlib.sha256(
            str(body["candidate_raw_toml"]).encode("utf-8")
        ).hexdigest()
        if recomputed != body["candidate_content_hash"]:
            raise SettingsConflictError("settings candidate does not match preview", reason="conflict") from None

    def _fingerprint(self, body: Mapping[str, Any]) -> str:
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
