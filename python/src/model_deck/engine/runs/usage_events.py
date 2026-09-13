"""Internal snapshot paging port for committed usage reconciliation."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Protocol, runtime_checkable

MAX_COMMITTED_USAGE_PAGE_EVENTS = 256
MAX_COMMITTED_USAGE_PAGE_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class CommittedUsageCursor:
    """Opaque, reader-instance-bound continuation; never persist or inspect it."""
    token: str


@dataclass(frozen=True, slots=True)
class CommittedUsageEvent:
    """Immutable stored event; payload access returns a fresh JSON value."""
    run_id: str
    session_id: str
    sequence: int
    event_schema_version: int
    observed_at: str
    payload_json: str

    @property
    def kind(self) -> str:
        return "usage.observed"

    @property
    def payload(self) -> Any:
        return json.loads(self.payload_json)


@dataclass(frozen=True, slots=True)
class CommittedUsagePage:
    events: tuple[CommittedUsageEvent, ...]
    high_water: int
    next_cursor: CommittedUsageCursor | None


class CommittedUsageCursorError(ValueError):
    """Invalid, foreign or expired-lifetime cursor."""


class CommittedUsageReadError(ValueError):
    """Persisted event cannot be represented within the bounded reader contract."""


@runtime_checkable
class CommittedUsageEventReader(Protocol):
    def read_committed_usage_events(
        self, *, cursor: CommittedUsageCursor | None = None, limit: int = 256,
    ) -> CommittedUsagePage:
        """Start or continue one ephemeral append-only snapshot reconciliation.

        None starts a fresh snapshot. Continue with exactly next_cursor until it
        is None. Later inserts await a fresh query. Cursors cannot cross reader
        instances/restart and are invalid across maintenance or replacement.
        """
        ...
