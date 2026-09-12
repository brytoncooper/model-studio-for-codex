from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from model_deck.engine.projections.ports import ProjectionOutboxEvent
from model_deck.engine.projections.receipts import validate_strict_int

OPERATION_WRITE = "write"
OPERATION_DELETE = "delete"


def validate_operation(value: object) -> str:
    """Validate that a value is one of the intent operation constants."""
    if not isinstance(value, str) or value not in (OPERATION_WRITE, OPERATION_DELETE):
        raise ValueError("operation must be 'write' or 'delete'")
    return value


def validate_outbox_id(value: object) -> int:
    """Validate an outbox id for read-only lookups: strict int and >= 1."""
    result = validate_strict_int("outbox_id", value)
    if result < 1:
        raise ValueError("outbox_id must be at least 1")
    return result


def compute_payload_sha256(event: ProjectionOutboxEvent) -> str:
    """Return the lowercase hex SHA-256 of the event's exact UTF-8 payload bytes."""
    return hashlib.sha256(event.payload_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProjectionMutationIntent:
    """Frozen record of a consumer's intended write/delete against an outbox row."""

    outbox_id: int
    consumer_id: str
    aggregate_type: str
    aggregate_id: str
    aggregate_revision: int
    event_kind: str
    payload_sha256: str
    operation: str
    artifact_ref: str
    expected_sha256: str | None
    desired_sha256: str | None


@runtime_checkable
class ProjectionMutationIntentJournal(Protocol):
    """Records and retrieves per-consumer mutation intent against outbox rows."""

    def get_intent(
        self, *, outbox_id: int, consumer_id: str
    ) -> ProjectionMutationIntent | None:
        """Retrieve recorded intent for one outbox row and consumer, if any."""
        ...

    def record_intent(
        self,
        event: ProjectionOutboxEvent,
        *,
        consumer_id: str,
        operation: str,
        artifact_ref: str,
        expected_sha256: str | None,
        desired_sha256: str | None,
    ) -> ProjectionMutationIntent:
        """Record a consumer's intent for an outbox event.

        write requires ``desired_sha256`` non-None; delete requires it None.

        Every call verifies the supplied event matches the persisted outbox
        row first: a missing or content-mismatched row raises
        ``ProjectionOutboxEventMismatchError``.

        When an existing intent for ``(outbox_id, consumer_id)`` matches the
        supplied event and parameters exactly, it is returned unchanged
        regardless of the current outbox state (an exact replay can succeed
        even after the outbox row has progressed to ``applied`` or
        ``conflict``). A changed replay raises ``ProjectionReceiptConflictError``.

        A fresh record (no existing intent) requires the outbox row to be
        ``pending``; any other state raises ``ProjectionOutboxStateError``.
        """
        ...
