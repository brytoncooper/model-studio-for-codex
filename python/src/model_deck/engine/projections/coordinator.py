"""Engine-owned narrow projection coordinator port.

A coordinator owns one explicit host projection and reconciles its outbox into
files. The engine exposes ``ProjectionCoordinator`` to the dispatch layer and
the bootstrap composition layer so that wiring is explicit and testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

ProjectionStatusValue = Literal["ready", "pending", "failed"]


@dataclass(frozen=True, slots=True)
class ProjectionStatusReport:
    """Engine-internal view of one host's projection health."""

    host_id: str
    status: ProjectionStatusValue
    last_reconciled_at: str | None
    pending_count: int
    last_conflict_reason: str | None


@runtime_checkable
class ProjectionCoordinator(Protocol):
    """One host's projection coordinator. Engine-owned narrow port."""

    host_id: str

    def reconcile_after(self, *, trigger: str) -> None:
        """Drain pending outbox events into projection files. Best-effort."""
        ...

    def status(self) -> ProjectionStatusReport:
        """Return a fresh snapshot of the host's projection health."""
        ...
