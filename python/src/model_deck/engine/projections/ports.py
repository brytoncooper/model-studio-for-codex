from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

OUTBOX_LIMIT_DEFAULT = 100
OUTBOX_LIMIT_MIN = 1
OUTBOX_LIMIT_MAX = 1000


def validate_outbox_limit(limit: int) -> int:
    """Validate a projection-outbox read limit.

    :param limit: Requested maximum event count.
    :returns: The validated limit unchanged.
    :raises TypeError: If ``limit`` is not a strict ``int`` (``bool`` rejected).
    :raises ValueError: If ``limit`` is outside 1..1000 inclusive.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an int")
    if limit < OUTBOX_LIMIT_MIN or limit > OUTBOX_LIMIT_MAX:
        raise ValueError("limit must be between 1 and 1000 inclusive")
    return limit


@dataclass(frozen=True, slots=True)
class ProjectionOutboxEvent:
    outbox_id: int
    aggregate_type: str
    aggregate_id: str
    aggregate_revision: int
    event_kind: str
    payload_json: str


@runtime_checkable
class ProjectionOutboxReader(Protocol):
    """Read-only ordered view over pending projection-outbox rows."""

    def list_pending(self, *, limit: int = OUTBOX_LIMIT_DEFAULT) -> tuple[ProjectionOutboxEvent, ...]:
        """Return pending events ordered by outbox_id ascending.

        :param limit: Maximum events to return, strict int in 1..1000.
        :returns: Snapshot tuple; never exposes mutable producer state.
        """
        ...
