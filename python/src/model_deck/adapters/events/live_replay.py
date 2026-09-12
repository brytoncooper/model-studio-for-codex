from __future__ import annotations

import json
import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque

from model_deck.engine.runs.ports import (
    RUN_LIVE_REPLAY_MAX_BYTES,
    RUN_LIVE_REPLAY_MAX_DURATION_SECONDS,
    SUBSCRIBER_QUEUE_MAX_BYTES,
    SUBSCRIBER_QUEUE_MAX_EVENTS,
    ApplicationRunEvent,
    EventReplayAckResult,
    EventReplayOutcome,
    EventReplayPage,
    ReplaySubscriptionHandle,
)

Clock = Callable[[], float]
IdFactory = Callable[[], str]


class _CreditRangeError(ValueError):
    pass


def _validate_credit(name: str, value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _CreditRangeError(f"{name} must be an integer")
    if value < 1 or value > 256:
        raise _CreditRangeError(f"{name} must be between 1 and 256")
    return value


def _event_to_payload_dict(event: ApplicationRunEvent) -> dict[str, Any]:
    return {
        "kind": event.kind,
        "run_id": event.run_id,
        "session_id": event.session_id,
        "sequence": event.sequence,
        "event_schema_version": event.event_schema_version,
        "observed_at": event.observed_at,
        "payload": event.payload,
    }


def _validate_json_value(name: str, value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return [_validate_json_value(f"{name}[{index}]", item) for index, item in enumerate(value)]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{name} must use string object keys")
            normalized[key] = _validate_json_value(f"{name}.{key}", item)
        return normalized
    raise ValueError(f"{name} must be a JSON value")


def encoded_event_bytes(event: ApplicationRunEvent) -> int:
    payload = _event_to_payload_dict(event)
    _validate_json_value("event.payload", payload["payload"])
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(encoded)


def _detached_event(event: ApplicationRunEvent) -> ApplicationRunEvent:
    return ApplicationRunEvent(
        kind=event.kind,
        run_id=event.run_id,
        session_id=event.session_id,
        sequence=event.sequence,
        event_schema_version=event.event_schema_version,
        observed_at=event.observed_at,
        payload=_validate_json_value("event.payload", event.payload),
    )


@dataclass(slots=True)
class _BufferedEvent:
    event: ApplicationRunEvent
    encoded_bytes: int
    stored_at: float


@dataclass(slots=True)
class _RunBuffer:
    entries: Deque[_BufferedEvent]
    max_sequence: int
    min_sequence: int | None

    def __init__(self) -> None:
        self.entries = deque()
        self.max_sequence = 0
        self.min_sequence = None


@dataclass(slots=True)
class _Subscription:
    subscription_id: str
    run_id: str
    pending: Deque[ApplicationRunEvent]
    pending_bytes: int
    credit_remaining: int
    slow_reader: bool
    last_acked_sequence: int
    delivered_through: int
    resume_unavailable: bool

    def __init__(self, subscription_id: str, run_id: str, grant_credit: int) -> None:
        self.subscription_id = subscription_id
        self.run_id = run_id
        self.pending = deque()
        self.pending_bytes = 0
        self.credit_remaining = grant_credit
        self.slow_reader = False
        self.last_acked_sequence = 0
        self.delivered_through = 0
        self.resume_unavailable = False


class LiveRunEventReplay:
    def __init__(
        self,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
    ) -> None:
        import time as time_module
        import uuid

        self._clock = clock or time_module.monotonic
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._lock = threading.Lock()
        self._buffers: dict[str, _RunBuffer] = {}
        self._last_published_sequence: dict[str, int] = {}
        self._subscriptions: dict[str, _Subscription] = {}
        self._subscriptions_by_run: dict[str, set[str]] = {}

    def publish_application_event(self, event: ApplicationRunEvent) -> None:
        stored_event = _detached_event(event)
        encoded = encoded_event_bytes(stored_event)
        with self._lock:
            last = self._last_published_sequence.get(stored_event.run_id)
            if last is None:
                if stored_event.sequence < 0:
                    raise ValueError("event sequence must be non-negative")
                last = stored_event.sequence - 1
            expected = last + 1
            if stored_event.sequence != expected:
                raise ValueError(
                    f"event sequence {stored_event.sequence} must be {expected}"
                )
            self._last_published_sequence[stored_event.run_id] = stored_event.sequence
            buffer = self._buffers.setdefault(stored_event.run_id, _RunBuffer())
            now = self._clock()
            buffer.entries.append(
                _BufferedEvent(event=stored_event, encoded_bytes=encoded, stored_at=now)
            )
            buffer.max_sequence = stored_event.sequence
            self._evict_run_buffer(buffer, now)
            if buffer.entries:
                buffer.min_sequence = buffer.entries[0].event.sequence
            self._fanout(stored_event, encoded)

    def subscribe(
        self,
        run_id: str,
        *,
        after_sequence: int | None,
        grant_credit: int,
    ) -> tuple[ReplaySubscriptionHandle, EventReplayPage]:
        grant = _validate_credit("grant_credit", grant_credit)
        cursor = 0 if after_sequence is None else after_sequence
        if cursor < 0:
            raise ValueError("after_sequence must be non-negative")
        with self._lock:
            buffer = self._buffers.get(run_id)
            if buffer is not None:
                self._evict_run_buffer(buffer, self._clock())
            if self._resume_unavailable(run_id, cursor):
                handle = ReplaySubscriptionHandle(self._id_factory(), run_id)
                sub = _Subscription(handle.subscription_id, run_id, grant)
                sub.resume_unavailable = True
                self._register_subscription(sub)
                page = EventReplayPage(
                    (),
                    cursor + 1,
                    EventReplayOutcome.RESUME_UNAVAILABLE,
                    0,
                    grant,
                )
                return handle, page

            handle = ReplaySubscriptionHandle(self._id_factory(), run_id)
            sub = _Subscription(handle.subscription_id, run_id, grant)
            self._register_subscription(sub)
            self._seed_subscription_from_buffer(sub, cursor)
            page = self._drain_page(sub)
            return handle, page

    def read_available(self, handle: ReplaySubscriptionHandle) -> EventReplayPage:
        with self._lock:
            sub = self._require_subscription(handle)
            buffer = self._buffers.get(sub.run_id)
            if buffer is not None:
                self._evict_run_buffer(buffer, self._clock())
            if sub.resume_unavailable:
                return EventReplayPage(
                    (),
                    sub.last_acked_sequence + 1,
                    EventReplayOutcome.RESUME_UNAVAILABLE,
                    0,
                    sub.credit_remaining,
                )
            return self._drain_page(sub)

    def ack(
        self,
        handle: ReplaySubscriptionHandle,
        through_sequence: int,
        return_credit: int,
    ) -> EventReplayAckResult:
        _validate_credit("return_credit", return_credit)
        with self._lock:
            sub = self._require_subscription(handle)
            if sub.resume_unavailable:
                return EventReplayAckResult(
                    EventReplayOutcome.RESUME_UNAVAILABLE,
                    sub.credit_remaining,
                )
            if sub.slow_reader:
                return EventReplayAckResult(
                    EventReplayOutcome.SLOW_READER,
                    sub.credit_remaining,
                )
            if through_sequence < sub.last_acked_sequence:
                return EventReplayAckResult(
                    EventReplayOutcome.DELIVERED,
                    sub.credit_remaining,
                )
            if through_sequence > sub.delivered_through:
                raise ValueError("cannot ack beyond delivered sequences")
            newly_acked = through_sequence - sub.last_acked_sequence
            sub.last_acked_sequence = through_sequence
            if newly_acked > 0:
                sub.credit_remaining = min(
                    256,
                    sub.credit_remaining + return_credit,
                )
            return EventReplayAckResult(
                EventReplayOutcome.DELIVERED,
                sub.credit_remaining,
            )

    def unsubscribe(self, handle: ReplaySubscriptionHandle) -> None:
        with self._lock:
            sub = self._subscriptions.pop(handle.subscription_id, None)
            if sub is None:
                return
            run_subs = self._subscriptions_by_run.get(sub.run_id)
            if run_subs is not None:
                run_subs.discard(sub.subscription_id)
                if not run_subs:
                    self._subscriptions_by_run.pop(sub.run_id, None)

    def _register_subscription(self, sub: _Subscription) -> None:
        self._subscriptions[sub.subscription_id] = sub
        self._subscriptions_by_run.setdefault(sub.run_id, set()).add(sub.subscription_id)

    def _require_subscription(self, handle: ReplaySubscriptionHandle) -> _Subscription:
        sub = self._subscriptions.get(handle.subscription_id)
        if sub is None or sub.run_id != handle.run_id:
            raise KeyError("unknown replay subscription")
        return sub

    def _resume_unavailable(self, run_id: str, after_sequence: int) -> bool:
        buffer = self._buffers.get(run_id)
        max_seq = self._last_published_sequence.get(run_id, 0)
        if after_sequence >= max_seq:
            return False
        if buffer is None or not buffer.entries:
            return after_sequence < max_seq
        min_seq = buffer.entries[0].event.sequence
        return after_sequence < min_seq - 1

    def _seed_subscription_from_buffer(self, sub: _Subscription, after_sequence: int) -> None:
        buffer = self._buffers.get(sub.run_id)
        if buffer is None:
            return
        for entry in buffer.entries:
            if entry.event.sequence <= after_sequence:
                continue
            self._enqueue_for_subscription(sub, entry.event, entry.encoded_bytes)

    def _fanout(self, event: ApplicationRunEvent, encoded: int) -> None:
        for subscription_id in list(self._subscriptions_by_run.get(event.run_id, ())):
            sub = self._subscriptions.get(subscription_id)
            if sub is None:
                continue
            self._enqueue_for_subscription(sub, event, encoded)

    def _enqueue_for_subscription(
        self,
        sub: _Subscription,
        event: ApplicationRunEvent,
        encoded: int,
    ) -> None:
        if sub.slow_reader or sub.resume_unavailable:
            return
        if len(sub.pending) >= SUBSCRIBER_QUEUE_MAX_EVENTS:
            sub.slow_reader = True
            return
        if sub.pending_bytes + encoded > SUBSCRIBER_QUEUE_MAX_BYTES:
            sub.slow_reader = True
            return
        sub.pending.append(event)
        sub.pending_bytes += encoded

    def _drain_page(self, sub: _Subscription) -> EventReplayPage:
        if sub.resume_unavailable:
            return EventReplayPage(
                (),
                sub.last_acked_sequence + 1,
                EventReplayOutcome.RESUME_UNAVAILABLE,
                0,
                sub.credit_remaining,
            )
        delivered: list[ApplicationRunEvent] = []
        bytes_delivered = 0
        while sub.pending and sub.credit_remaining > 0:
            event = sub.pending.popleft()
            encoded = encoded_event_bytes(event)
            sub.pending_bytes = max(0, sub.pending_bytes - encoded)
            delivered.append(_detached_event(event))
            bytes_delivered += encoded
            sub.credit_remaining -= 1
            sub.delivered_through = max(sub.delivered_through, event.sequence)
        outcome = (
            EventReplayOutcome.SLOW_READER
            if sub.slow_reader
            else EventReplayOutcome.DELIVERED
        )
        next_sequence = (
            delivered[-1].sequence + 1 if delivered else self._resume_cursor(sub)
        )
        return EventReplayPage(
            tuple(delivered),
            next_sequence,
            outcome,
            bytes_delivered,
            sub.credit_remaining,
        )

    def _resume_cursor(self, sub: _Subscription) -> int:
        if sub.delivered_through > sub.last_acked_sequence:
            return sub.last_acked_sequence + 1
        if sub.pending:
            return sub.pending[0].sequence
        buffer = self._buffers.get(sub.run_id)
        if buffer and buffer.entries:
            return buffer.entries[-1].event.sequence + 1
        return sub.last_acked_sequence + 1

    def _evict_run_buffer(self, buffer: _RunBuffer, now: float) -> None:
        while buffer.entries:
            oldest = buffer.entries[0]
            total_bytes = sum(entry.encoded_bytes for entry in buffer.entries)
            expired = (
                now - oldest.stored_at > RUN_LIVE_REPLAY_MAX_DURATION_SECONDS
            )
            over_bytes = total_bytes > RUN_LIVE_REPLAY_MAX_BYTES
            if not expired and not over_bytes:
                break
            buffer.entries.popleft()
        if buffer.entries:
            buffer.min_sequence = buffer.entries[0].event.sequence
        else:
            buffer.min_sequence = None
