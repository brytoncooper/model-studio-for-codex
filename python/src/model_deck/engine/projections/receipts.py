from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from model_deck.engine.projections.ports import ProjectionOutboxEvent

_SHA256_LOWERCASE_HEX = re.compile(r"^[0-9a-f]{64}$")
_PRINTABLE_ASCII = re.compile(r"^[\x20-\x7e]+$")
_SECRET_DETAIL_MARKERS = (
    "apikey",
    "password",
    "secret",
    "token",
    "credential",
    "authorization",
    "bearer",
)

MAX_CONSUMER_ID_LENGTH = 128
MAX_AGGREGATE_TYPE_LENGTH = 64
MAX_AGGREGATE_ID_LENGTH = 256
MAX_ARTIFACT_REF_LENGTH = 256
MAX_CONFLICT_DETAIL_LENGTH = 512
MAX_EVENT_KIND_LENGTH = 128


class ProjectionReceiptError(Exception):
    """Base error for projection receipt recording."""


class ProjectionOutboxEventMismatchError(ProjectionReceiptError):
    """Supplied outbox event does not match the persisted row."""


class ProjectionOutboxStateError(ProjectionReceiptError):
    """Outbox row is not in the state required for the operation."""


class ProjectionReceiptConflictError(ProjectionReceiptError):
    """Receipt parameters conflict with a prior recorded outcome."""


@dataclass(frozen=True, slots=True)
class ProjectionAppliedState:
    consumer_id: str
    aggregate_type: str
    aggregate_id: str
    applied_revision: int
    applied_outbox_id: int
    artifact_ref: str
    # ``None`` marks a verified applied deletion (tombstone) where no artifact
    # bytes were produced. This is the only legitimate null hash; it never
    # stands in for an empty-file sentinel.
    output_sha256: str | None


@dataclass(frozen=True, slots=True)
class ProjectionOutboxConflictReceipt:
    outbox_id: int
    consumer_id: str
    detail: str


def validate_strict_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    return value


def validate_bounded_printable(name: str, value: object, *, max_length: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > max_length:
        raise ValueError(f"{name} must be at most {max_length} characters")
    if _PRINTABLE_ASCII.fullmatch(value) is None:
        raise ValueError(f"{name} must contain printable ASCII only")
    return value


def validate_consumer_id(value: object) -> str:
    return validate_bounded_printable(
        "consumer_id", value, max_length=MAX_CONSUMER_ID_LENGTH
    )


def validate_aggregate_type(value: object) -> str:
    return validate_bounded_printable(
        "aggregate_type", value, max_length=MAX_AGGREGATE_TYPE_LENGTH
    )


def validate_aggregate_id(value: object) -> str:
    return validate_bounded_printable(
        "aggregate_id", value, max_length=MAX_AGGREGATE_ID_LENGTH
    )


def validate_artifact_ref(value: object) -> str:
    return validate_bounded_printable(
        "artifact_ref", value, max_length=MAX_ARTIFACT_REF_LENGTH
    )


def validate_output_sha256(value: object) -> str:
    """Validate a strict 64-character lowercase hex sha256 digest.

    Applied upserts require a real digest. Deletion receipts use ``None``
    directly and do not call this validator.
    """
    if not isinstance(value, str):
        raise TypeError("output_sha256 must be a str")
    if _SHA256_LOWERCASE_HEX.fullmatch(value) is None:
        raise ValueError(
            "output_sha256 must be a lowercase 64-character hex sha256 digest"
        )
    return value


def validate_conflict_detail(value: object) -> str:
    detail = validate_bounded_printable(
        "detail", value, max_length=MAX_CONFLICT_DETAIL_LENGTH
    )
    lowered = "".join(character for character in detail.casefold() if character.isalnum())
    for marker in _SECRET_DETAIL_MARKERS:
        if marker in lowered:
            raise ValueError("detail must not contain secret-bearing markers")
    return detail


def validate_projection_event(event: ProjectionOutboxEvent) -> ProjectionOutboxEvent:
    outbox_id = validate_strict_int("outbox_id", event.outbox_id)
    if outbox_id < 1:
        raise ValueError("outbox_id must be at least 1")
    revision = validate_strict_int("aggregate_revision", event.aggregate_revision)
    if revision < 1:
        raise ValueError("aggregate_revision must be at least 1")
    validate_aggregate_type(event.aggregate_type)
    validate_aggregate_id(event.aggregate_id)
    validate_bounded_printable(
        "event_kind", event.event_kind, max_length=MAX_EVENT_KIND_LENGTH
    )
    if not isinstance(event.payload_json, str):
        raise TypeError("payload_json must be a str")
    return event


def events_match_row(event: ProjectionOutboxEvent, row: tuple[object, ...]) -> bool:
    outbox_id, aggregate_type, aggregate_id, aggregate_revision, event_kind, payload_json, _state = row
    if validate_strict_int("outbox_id", event.outbox_id) != outbox_id:
        return False
    if event.aggregate_type != aggregate_type:
        return False
    if event.aggregate_id != aggregate_id:
        return False
    if validate_strict_int("aggregate_revision", event.aggregate_revision) != aggregate_revision:
        return False
    if event.event_kind != event_kind:
        return False
    return event.payload_json == payload_json


def row_to_applied_state(row: tuple[object, ...]) -> ProjectionAppliedState:
    return ProjectionAppliedState(
        consumer_id=row[0],
        aggregate_type=row[1],
        aggregate_id=row[2],
        applied_revision=row[3],
        applied_outbox_id=row[4],
        artifact_ref=row[5],
        output_sha256=row[6],
    )


@runtime_checkable
class ProjectionReceiptStore(Protocol):
    """Records projection outcomes against pending outbox rows."""

    def get_applied(
        self,
        *,
        consumer_id: str,
        aggregate_type: str,
        aggregate_id: str,
    ) -> ProjectionAppliedState | None:
        """Return the current applied state for one consumer and aggregate.

        ``output_sha256`` is ``None`` when the stored row is a verified applied
        deletion (tombstone); consumers must treat absent-only semantics
        consistently.
        """
        ...

    def record_applied(
        self,
        event: ProjectionOutboxEvent,
        *,
        consumer_id: str,
        artifact_ref: str,
        output_sha256: str,
    ) -> ProjectionAppliedState:
        """Mark an outbox row applied and record aggregate applied state."""
        ...

    def record_deleted(
        self,
        event: ProjectionOutboxEvent,
        *,
        consumer_id: str,
        artifact_ref: str,
    ) -> ProjectionAppliedState:
        """Mark an outbox row applied as a verified deletion (tombstone).

        ``artifact_ref`` identifies the deleted artifact for replay/conflict
        detection. The stored applied state sets ``output_sha256`` to ``None``
        rather than to any digest; an empty-file hash is never a sentinel.
        Exact replay returns the original deletion receipt metadata even when
        the current aggregate state has advanced; use get_applied for current state.
        """
        ...

    def record_conflict(
        self,
        event: ProjectionOutboxEvent,
        *,
        consumer_id: str,
        detail: str,
    ) -> ProjectionOutboxConflictReceipt:
        """Mark an outbox row conflict and store a redacted receipt."""
        ...
