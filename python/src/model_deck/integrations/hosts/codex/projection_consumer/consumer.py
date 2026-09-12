from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from model_deck.engine.projections.file_port import (
    PROJECTION_FILE_OPERATION_DELETE,
    PROJECTION_FILE_OPERATION_WRITE,
    PROJECTION_FILE_OUTCOME_APPLIED,
    PROJECTION_FILE_OUTCOME_CONFLICT,
    PROJECTION_FILE_OUTCOME_NOOP,
    PROJECTION_FILE_REASON_FOREIGN_OWNER,
    PROJECTION_FILE_REASON_HASH_MISMATCH,
    PROJECTION_FILE_REASON_MISSING,
    PROJECTION_FILE_REASON_POSTDELETE_INTERFERENCE,
    PROJECTION_FILE_REASON_POSTWRITE_INTERFERENCE,
    PROJECTION_FILE_REASON_SYMLINK,
    PROJECTION_FILE_REASON_UNEXPECTED_EXISTING,
    ConditionalProjectionFiles,
    ProjectionFileReceipt,
)
from model_deck.engine.projections.intents import (
    OPERATION_DELETE,
    OPERATION_WRITE,
    ProjectionMutationIntentJournal,
)
from model_deck.engine.projections.ports import (
    OUTBOX_LIMIT_DEFAULT,
    ProjectionOutboxEvent,
    ProjectionOutboxReader,
    validate_outbox_limit,
)
from model_deck.engine.projections.receipts import ProjectionReceiptConflictError, ProjectionReceiptStore, validate_artifact_ref

CONSUMER_ID = "com.modeldeck.host.codex.managed-agent"

SUPPORTED_EVENT_KIND = "registered_model.upserted"
REMOVED_EVENT_KIND = "registered_model.removed"
SUPPORTED_EVENT_KINDS = frozenset((SUPPORTED_EVENT_KIND, REMOVED_EVENT_KIND))
SUPPORTED_AGGREGATE_TYPE = "registered_model"

_REQUIRED_PAYLOAD_KEYS = frozenset(
    ("connection_id", "display_name", "provider_model_id", "registration_id", "revision")
)

_REQUIRED_REMOVAL_PAYLOAD_KEYS = frozenset(("registration_id", "removed", "revision"))

_ALLOWED_FILE_CONFLICT_REASONS = frozenset(
    (
        PROJECTION_FILE_REASON_MISSING,
        PROJECTION_FILE_REASON_UNEXPECTED_EXISTING,
        PROJECTION_FILE_REASON_HASH_MISMATCH,
        PROJECTION_FILE_REASON_SYMLINK,
        PROJECTION_FILE_REASON_FOREIGN_OWNER,
        PROJECTION_FILE_REASON_POSTWRITE_INTERFERENCE,
    )
)

_ALLOWED_FILE_DELETE_CONFLICT_REASONS = frozenset(
    (
        PROJECTION_FILE_REASON_MISSING,
        PROJECTION_FILE_REASON_UNEXPECTED_EXISTING,
        PROJECTION_FILE_REASON_HASH_MISMATCH,
        PROJECTION_FILE_REASON_SYMLINK,
        PROJECTION_FILE_REASON_FOREIGN_OWNER,
        PROJECTION_FILE_REASON_POSTDELETE_INTERFERENCE,
    )
)


@dataclass(frozen=True, slots=True)
class CodexProjectionWrite:
    """Frozen renderer output for one supported projection event."""

    path: Path
    data: bytes

    @property
    def artifact_ref(self) -> str:
        """Derive the stable artifact reference from the write path."""
        return self.path.as_posix()


class CodexProjectionMaterializationError(Exception):
    """Marker for renderer failures; its message is never persisted."""


class CodexProjectionDependencyError(Exception):
    """Malformed file receipt or artifact reference from a dependency."""


@runtime_checkable
class CodexProjectionMaterializer(Protocol):
    """Runtime renderer turning a supported event into a file write."""

    def materialize(self, event: ProjectionOutboxEvent) -> CodexProjectionWrite:
        """Render one event into a frozen write."""
        ...


@dataclass(frozen=True, slots=True)
class CodexProjectionItemResult:
    """Frozen per-event outcome."""

    outbox_id: int
    event_kind: str
    outcome: str
    artifact_ref: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class CodexProjectionBatchResult:
    """Frozen ordered batch outcome."""

    items: tuple[CodexProjectionItemResult, ...]


def _payload_valid(event: ProjectionOutboxEvent) -> bool:
    if event.aggregate_type != SUPPORTED_AGGREGATE_TYPE:
        return False
    try:
        payload = json.loads(event.payload_json)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    if set(payload.keys()) != set(_REQUIRED_PAYLOAD_KEYS):
        return False
    for key in ("connection_id", "display_name", "provider_model_id", "registration_id"):
        value = payload[key]
        if not isinstance(value, str) or not value:
            return False
    revision = payload["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int):
        return False
    if payload["registration_id"] != event.aggregate_id:
        return False
    return revision == event.aggregate_revision


def _removal_payload_valid(event: ProjectionOutboxEvent) -> bool:
    if event.aggregate_type != SUPPORTED_AGGREGATE_TYPE:
        return False
    try:
        payload = json.loads(event.payload_json)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    if set(payload.keys()) != set(_REQUIRED_REMOVAL_PAYLOAD_KEYS):
        return False
    registration_id = payload["registration_id"]
    if not isinstance(registration_id, str) or not registration_id:
        return False
    if payload["removed"] is not True:
        return False
    revision = payload["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int):
        return False
    if registration_id != event.aggregate_id:
        return False
    return revision == event.aggregate_revision


def _mutation_valid(write: CodexProjectionWrite) -> bool:
    if not isinstance(write.path, Path):
        return False
    if not isinstance(write.data, bytes):
        return False
    if write.path.is_absolute():
        return False
    parts = write.path.parts
    if not parts:
        return False
    if any(part in (".", "..") for part in parts):
        return False
    return True


def _artifact_ref_valid(artifact_ref: object) -> bool:
    try:
        validated = validate_artifact_ref(artifact_ref)
    except (TypeError, ValueError):
        return False
    if validated.startswith("/"):
        return False
    if any(segment in (".", "..") or segment == "" for segment in validated.split("/")):
        return False
    return True


def _check_file_receipt(receipt: object, *, path: Path, expected: str | None, desired: str) -> None:
    if not isinstance(receipt, ProjectionFileReceipt):
        raise CodexProjectionDependencyError("file receipt is not a ProjectionFileReceipt")
    if receipt.operation != PROJECTION_FILE_OPERATION_WRITE:
        raise CodexProjectionDependencyError("file receipt operation is not write")
    if receipt.path != path:
        raise CodexProjectionDependencyError("file receipt path mismatch")
    if receipt.expected_sha256 != expected:
        raise CodexProjectionDependencyError("file receipt expected hash mismatch")
    if receipt.outcome in (PROJECTION_FILE_OUTCOME_APPLIED, PROJECTION_FILE_OUTCOME_NOOP):
        if receipt.reason is not None:
            raise CodexProjectionDependencyError("success receipt carries a reason")
        if receipt.result_sha256 != desired:
            raise CodexProjectionDependencyError("success receipt result hash mismatch")
        if receipt.observed_sha256 != expected:
            raise CodexProjectionDependencyError("success receipt observed hash mismatch")
        if receipt.outcome == PROJECTION_FILE_OUTCOME_NOOP and expected != desired:
            raise CodexProjectionDependencyError("noop receipt without matching expected hash")
        return
    if receipt.outcome == PROJECTION_FILE_OUTCOME_CONFLICT:
        if receipt.reason not in _ALLOWED_FILE_CONFLICT_REASONS:
            raise CodexProjectionDependencyError("conflict receipt reason not allowlisted")
        return
    raise CodexProjectionDependencyError("file receipt outcome unknown")


def _check_delete_receipt(receipt: object, *, path: Path, expected: str | None) -> None:
    if not isinstance(receipt, ProjectionFileReceipt):
        raise CodexProjectionDependencyError("file receipt is not a ProjectionFileReceipt")
    if receipt.operation != PROJECTION_FILE_OPERATION_DELETE:
        raise CodexProjectionDependencyError("file receipt operation is not delete")
    if receipt.path != path:
        raise CodexProjectionDependencyError("file receipt path mismatch")
    if receipt.expected_sha256 != expected:
        raise CodexProjectionDependencyError("file receipt expected hash mismatch")
    if receipt.outcome == PROJECTION_FILE_OUTCOME_APPLIED:
        if receipt.reason is not None:
            raise CodexProjectionDependencyError("success receipt carries a reason")
        if receipt.result_sha256 is not None:
            raise CodexProjectionDependencyError("delete success receipt retains bytes")
        if receipt.observed_sha256 != expected:
            raise CodexProjectionDependencyError("success receipt observed hash mismatch")
        return
    if receipt.outcome == PROJECTION_FILE_OUTCOME_NOOP:
        if expected is not None:
            raise CodexProjectionDependencyError("noop receipt without absent-only precondition")
        if receipt.reason is not None:
            raise CodexProjectionDependencyError("success receipt carries a reason")
        if receipt.observed_sha256 is not None or receipt.result_sha256 is not None:
            raise CodexProjectionDependencyError("delete noop receipt carries hashes")
        return
    if receipt.outcome == PROJECTION_FILE_OUTCOME_CONFLICT:
        if receipt.reason not in _ALLOWED_FILE_DELETE_CONFLICT_REASONS:
            raise CodexProjectionDependencyError("conflict receipt reason not allowlisted")
        if receipt.reason == PROJECTION_FILE_REASON_MISSING and (
            receipt.observed_sha256 is not None or receipt.result_sha256 is not None
        ):
            raise CodexProjectionDependencyError("missing receipt carries file hashes")
        return
    raise CodexProjectionDependencyError("file receipt outcome unknown")


class CodexProjectionConsumer:
    """Consume pending registered_model upserts and removals into owned projection files."""

    def __init__(
        self,
        outbox: ProjectionOutboxReader,
        journal: ProjectionMutationIntentJournal,
        files: ConditionalProjectionFiles,
        receipts: ProjectionReceiptStore,
        materializer: CodexProjectionMaterializer,
    ) -> None:
        self._outbox = outbox
        self._journal = journal
        self._files = files
        self._receipts = receipts
        self._materializer = materializer

    def consume_pending(self, *, limit: int = OUTBOX_LIMIT_DEFAULT) -> CodexProjectionBatchResult:
        """Consume up to ``limit`` pending events in outbox order."""
        validate_outbox_limit(limit)
        events = self._outbox.list_pending(limit=limit)
        items: list[CodexProjectionItemResult] = []
        for event in events:
            items.append(self._consume_one(event))
        return CodexProjectionBatchResult(items=tuple(items))

    def _consume_one(self, event: ProjectionOutboxEvent) -> CodexProjectionItemResult:
        if event.event_kind not in SUPPORTED_EVENT_KINDS:
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id,
                event_kind=event.event_kind,
                outcome="skipped",
                artifact_ref=None,
                reason="unsupported_event",
            )
        if event.event_kind == REMOVED_EVENT_KIND:
            return self._consume_removal(event)
        return self._consume_upsert(event)

    def _consume_upsert(self, event: ProjectionOutboxEvent) -> CodexProjectionItemResult:
        if not _payload_valid(event):
            self._receipts.record_conflict(event, consumer_id=CONSUMER_ID, detail="event_invalid")
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id,
                event_kind=event.event_kind,
                outcome="conflict",
                artifact_ref=None,
                reason="event_invalid",
            )
        try:
            write = self._materializer.materialize(event)
        except CodexProjectionMaterializationError:
            self._receipts.record_conflict(
                event, consumer_id=CONSUMER_ID, detail="materialization_invalid"
            )
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id,
                event_kind=event.event_kind,
                outcome="conflict",
                artifact_ref=None,
                reason="materialization_invalid",
            )
        if not isinstance(write, CodexProjectionWrite) or not _mutation_valid(write):
            self._receipts.record_conflict(
                event, consumer_id=CONSUMER_ID, detail="materialization_invalid"
            )
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id,
                event_kind=event.event_kind,
                outcome="conflict",
                artifact_ref=None,
                reason="materialization_invalid",
            )
        artifact_ref = write.artifact_ref
        if not _artifact_ref_valid(artifact_ref):
            self._receipts.record_conflict(
                event, consumer_id=CONSUMER_ID, detail="materialization_invalid"
            )
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id,
                event_kind=event.event_kind,
                outcome="conflict",
                artifact_ref=None,
                reason="materialization_invalid",
            )
        desired_sha256 = hashlib.sha256(write.data).hexdigest()
        applied = self._receipts.get_applied(
            consumer_id=CONSUMER_ID,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
        )
        if applied is not None and event.aggregate_revision <= applied.applied_revision:
            if (event.aggregate_revision == applied.applied_revision
                    and event.outbox_id == applied.applied_outbox_id):
                if applied.artifact_ref != artifact_ref or applied.output_sha256 != desired_sha256:
                    raise ProjectionReceiptConflictError("applied upsert replay parameters differ")
                # Validate the exact persisted event/receipt through the store.
                # Replaying an acknowledgment never grants a new file mutation.
                self._receipts.record_applied(
                    event, consumer_id=CONSUMER_ID,
                    artifact_ref=artifact_ref, output_sha256=desired_sha256,
                )
                return CodexProjectionItemResult(
                    outbox_id=event.outbox_id, event_kind=event.event_kind,
                    outcome="applied", artifact_ref=artifact_ref, reason=None,
                )
            self._receipts.record_conflict(
                event, consumer_id=CONSUMER_ID, detail="stale_revision"
            )
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id, event_kind=event.event_kind,
                outcome="conflict", artifact_ref=None, reason="stale_revision",
            )
        if applied is not None and applied.artifact_ref != artifact_ref:
            self._receipts.record_conflict(
                event, consumer_id=CONSUMER_ID, detail="artifact_ref_changed"
            )
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id, event_kind=event.event_kind,
                outcome="conflict", artifact_ref=None, reason="artifact_ref_changed",
            )
        existing = self._journal.get_intent(outbox_id=event.outbox_id, consumer_id=CONSUMER_ID)
        if existing is not None:
            expected = existing.expected_sha256
        else:
            expected = applied.output_sha256 if applied is not None else None
        try:
            self._journal.record_intent(
                event,
                consumer_id=CONSUMER_ID,
                operation=OPERATION_WRITE,
                artifact_ref=artifact_ref,
                expected_sha256=expected,
                desired_sha256=desired_sha256,
            )
        except ProjectionReceiptConflictError:
            self._receipts.record_conflict(event, consumer_id=CONSUMER_ID, detail="intent_mismatch")
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id,
                event_kind=event.event_kind,
                outcome="conflict",
                artifact_ref=None,
                reason="intent_mismatch",
            )
        receipt = self._files.compare_and_write(write.path, write.data, expected)
        _check_file_receipt(receipt, path=write.path, expected=expected, desired=desired_sha256)
        if receipt.outcome in (PROJECTION_FILE_OUTCOME_APPLIED, PROJECTION_FILE_OUTCOME_NOOP):
            self._receipts.record_applied(
                event,
                consumer_id=CONSUMER_ID,
                artifact_ref=artifact_ref,
                output_sha256=desired_sha256,
            )
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id,
                event_kind=event.event_kind,
                outcome="applied",
                artifact_ref=artifact_ref,
                reason=None,
            )
        file_reason = receipt.reason
        assert file_reason is not None
        self._receipts.record_conflict(
            event, consumer_id=CONSUMER_ID, detail=f"file_{file_reason}"
        )
        return CodexProjectionItemResult(
            outbox_id=event.outbox_id,
            event_kind=event.event_kind,
            outcome="conflict",
            artifact_ref=None,
            reason=file_reason,
        )

    def _consume_removal(self, event: ProjectionOutboxEvent) -> CodexProjectionItemResult:
        if not _removal_payload_valid(event):
            self._receipts.record_conflict(event, consumer_id=CONSUMER_ID, detail="event_invalid")
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id,
                event_kind=event.event_kind,
                outcome="conflict",
                artifact_ref=None,
                reason="event_invalid",
            )
        applied = self._receipts.get_applied(
            consumer_id=CONSUMER_ID,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
        )
        if applied is None:
            self._receipts.record_conflict(
                event, consumer_id=CONSUMER_ID, detail="unresolved_no_prior_applied"
            )
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id,
                event_kind=event.event_kind,
                outcome="conflict",
                artifact_ref=None,
                reason="unresolved_no_prior_applied",
            )
        if (event.aggregate_revision == applied.applied_revision
                and event.outbox_id == applied.applied_outbox_id):
            if not _artifact_ref_valid(applied.artifact_ref):
                raise CodexProjectionDependencyError("prior applied artifact reference malformed")
            if applied.output_sha256 is not None:
                raise ProjectionReceiptConflictError("applied removal replay is not a tombstone")
            self._receipts.record_deleted(
                event, consumer_id=CONSUMER_ID, artifact_ref=applied.artifact_ref,
            )
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id, event_kind=event.event_kind,
                outcome="applied", artifact_ref=applied.artifact_ref, reason=None,
            )
        if event.aggregate_revision <= applied.applied_revision:
            self._receipts.record_conflict(
                event, consumer_id=CONSUMER_ID, detail="stale_revision"
            )
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id,
                event_kind=event.event_kind,
                outcome="conflict",
                artifact_ref=None,
                reason="stale_revision",
            )
        artifact_ref = applied.artifact_ref
        if not _artifact_ref_valid(artifact_ref):
            raise CodexProjectionDependencyError("prior applied artifact reference malformed")
        path = Path(artifact_ref)
        expected = applied.output_sha256
        existing = self._journal.get_intent(outbox_id=event.outbox_id, consumer_id=CONSUMER_ID)
        retry_proven = (
            existing is not None
            and existing.operation == OPERATION_DELETE
            and existing.artifact_ref == artifact_ref
            and existing.expected_sha256 == expected
            and existing.desired_sha256 is None
        )
        try:
            self._journal.record_intent(
                event,
                consumer_id=CONSUMER_ID,
                operation=OPERATION_DELETE,
                artifact_ref=artifact_ref,
                expected_sha256=expected,
                desired_sha256=None,
            )
        except ProjectionReceiptConflictError:
            self._receipts.record_conflict(event, consumer_id=CONSUMER_ID, detail="intent_mismatch")
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id,
                event_kind=event.event_kind,
                outcome="conflict",
                artifact_ref=None,
                reason="intent_mismatch",
            )
        receipt = self._files.compare_and_delete(path, expected)
        _check_delete_receipt(receipt, path=path, expected=expected)
        if receipt.outcome in (PROJECTION_FILE_OUTCOME_APPLIED, PROJECTION_FILE_OUTCOME_NOOP):
            self._receipts.record_deleted(
                event,
                consumer_id=CONSUMER_ID,
                artifact_ref=artifact_ref,
            )
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id,
                event_kind=event.event_kind,
                outcome="applied",
                artifact_ref=artifact_ref,
                reason=None,
            )
        if receipt.reason == PROJECTION_FILE_REASON_MISSING and expected is not None and retry_proven:
            self._receipts.record_deleted(
                event,
                consumer_id=CONSUMER_ID,
                artifact_ref=artifact_ref,
            )
            return CodexProjectionItemResult(
                outbox_id=event.outbox_id,
                event_kind=event.event_kind,
                outcome="applied",
                artifact_ref=artifact_ref,
                reason=None,
            )
        file_reason = receipt.reason
        assert file_reason is not None
        self._receipts.record_conflict(
            event, consumer_id=CONSUMER_ID, detail=f"file_{file_reason}"
        )
        return CodexProjectionItemResult(
            outbox_id=event.outbox_id,
            event_kind=event.event_kind,
            outcome="conflict",
            artifact_ref=None,
            reason=file_reason,
        )
