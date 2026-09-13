"""Query-time projection of committed usage events through public application ports."""
from __future__ import annotations

from model_deck.engine.runs.usage_events import (
    CommittedUsageEvent, CommittedUsageEventReader, MAX_COMMITTED_USAGE_PAGE_EVENTS,
)
from model_deck.engine.usage.ports import (
    QueryUsageResult, UsageConflictError, UsageEventMismatchError,
    UsageQueryValidationError, UsageResourceExhaustedError,
)
from model_deck.engine.usage.use_cases import QueryUsageUseCase, RecordUsageUseCase


class UsageReconciliationError(RuntimeError):
    """Committed usage could not be fully reconciled; no query result is available."""


class ReconciledUsageQueryUseCase:
    def __init__(self, *, reader: CommittedUsageEventReader, record_usage: RecordUsageUseCase,
                 query_usage: QueryUsageUseCase, page_size: int = MAX_COMMITTED_USAGE_PAGE_EVENTS) -> None:
        if type(page_size) is not int or not 1 <= page_size <= MAX_COMMITTED_USAGE_PAGE_EVENTS:
            raise ValueError("usage reconciliation page size must be between 1 and 256")
        self._reader = reader
        self._record_usage = record_usage
        self._query_usage = query_usage
        self._page_size = page_size

    def query(self, since: str | None = None, until: str | None = None) -> QueryUsageResult:
        try:
            self._reconcile()
            return self._query_usage.query(since, until)
        except (UsageConflictError, UsageEventMismatchError, UsageQueryValidationError,
                UsageResourceExhaustedError, UsageReconciliationError):
            raise
        except Exception:
            raise UsageReconciliationError("committed usage reconciliation unavailable") from None

    def _reconcile(self) -> None:
        cursor = None
        high_water = None
        # Exponentially spaced checkpoints detect cursor cycles in constant
        # memory, including A -> B -> A, without retaining every page token.
        checkpoint_cursor = None
        checkpoint_interval = 1
        steps_since_checkpoint = 0
        while True:
            page = self._reader.read_committed_usage_events(cursor=cursor, limit=self._page_size)
            if type(page.high_water) is not int or page.high_water < 0:
                raise UsageReconciliationError("committed usage snapshot marker invalid")
            if high_water is None:
                high_water = page.high_water
            elif page.high_water != high_water:
                raise UsageReconciliationError("committed usage snapshot changed during reconciliation")
            if len(page.events) > self._page_size:
                raise UsageReconciliationError("committed usage page exceeds requested bound")
            for event in page.events:
                self._record_usage.record(_wire_event(event))
            cursor = page.next_cursor
            if cursor is None:
                return
            if checkpoint_cursor is None:
                checkpoint_cursor = cursor
            else:
                steps_since_checkpoint += 1
                if cursor == checkpoint_cursor:
                    raise UsageReconciliationError("committed usage cursor cycle")
                if steps_since_checkpoint == checkpoint_interval:
                    checkpoint_cursor = cursor
                    checkpoint_interval *= 2
                    steps_since_checkpoint = 0


def _wire_event(event: CommittedUsageEvent) -> dict:
    payload = event.payload
    if not isinstance(payload, dict) or set(payload) != {"usage"}:
        raise UsageEventMismatchError("committed usage payload must contain only usage")
    return {
        "kind": event.kind,
        "run_id": event.run_id,
        "session_id": event.session_id,
        "sequence": event.sequence,
        "event_schema_version": event.event_schema_version,
        "observed_at": event.observed_at,
        "usage": payload["usage"],
    }
