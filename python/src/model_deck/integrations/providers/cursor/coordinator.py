from __future__ import annotations

import copy
import re
import uuid
from collections import deque
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from model_deck.engine.runs.ports import (
    CancelProviderRunResult,
    ProviderCancelTerminationStatus,
    ProviderExecutionPort,
    ProviderRunEvent,
    ProviderRunEventSink,
    ProviderRunHandle,
    RunRequest,
    SubmitToolResultProviderOutcome,
    SubmitToolResultProviderResult,
)

CURSOR_PROVIDER_ID = "com.modeldeck.provider.cursor"

# Frozen wire schemas live in
# `python/src/model_deck_contracts/schemas/contracts/{common,engine.v1}/*.json`.
# The enums below mirror those schemas; the adapter validates against the same
# closed set so the SDK never reaches the sink with a payload that wouldn't
# survive contract validation upstream.

_ALLOWED_SDK_EVENT_KINDS = frozenset(
    {
        "run.started",
        "content.delta",
        "tool.requested",
        "usage.observed",
        "run.cancelling",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.interrupted",
    }
)

_TERMINAL_SDK_EVENT_KINDS = frozenset(
    {
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.interrupted",
    }
)

_TERMINAL_OUTCOME_BY_KIND = {
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
    "run.interrupted": "interrupted",
}

_ALLOWED_DELTA_CHANNELS = frozenset({"text", "reasoning"})

_ALLOWED_USAGE_UNIT_KINDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "tool_calls",
        "requests",
        "other",
    }
)

_ALLOWED_DOMAIN_ERROR_CODES = frozenset(
    {
        "invalid_argument",
        "unsupported_capability",
        "capability_denied",
        "not_found",
        "conflict",
        "version_mismatch",
        "provider_unavailable",
        "plugin_unavailable",
        "rate_limited",
        "deadline_exceeded",
        "interrupted",
        "resume_unavailable",
        "resource_exhausted",
        "projection_pending",
        "internal",
    }
)

_ALLOWED_USAGE_RECORD_FIELDS = frozenset(
    {
        "run_id",
        "session_id",
        "observed_at",
        "units",
        "unit_kind",
        "registration_id",
        "connection_id",
        "provider_model_id",
        "settled_amount",
        "currency",
        "estimate_amount",
    }
)

_ALLOWED_TERMINAL_ERROR_FIELDS = frozenset(
    {"code", "retryable", "message", "request_id"}
)


class CursorProtocolError(Exception):
    pass


class CursorDuplicateStartError(CursorProtocolError):
    pass


class CursorCoordinatorClosedError(CursorProtocolError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _detached_json(payload: Any) -> Any:
    detached = copy.deepcopy(payload)
    try:
        json.dumps(detached, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CursorProtocolError("sdk event payload must be finite JSON") from exc
    return detached


def _reject_unknown_keys(
    payload: Mapping[str, Any], allowed: frozenset[str], *, context: str
) -> None:
    unknown = sorted(str(name) for name in payload.keys() if name not in allowed)
    if unknown:
        raise CursorProtocolError(
            f"{context} has unknown field(s): {', '.join(unknown)}"
        )


def _require_keys(
    payload: Mapping[str, Any], required: tuple[str, ...], *, context: str
) -> None:
    missing = sorted(field for field in required if field not in payload)
    if missing:
        raise CursorProtocolError(
            f"{context} missing required field(s): {', '.join(missing)}"
        )


def _validate_terminal_error(value: Any, *, context: str) -> None:
    if not isinstance(value, Mapping):
        raise CursorProtocolError(f"{context} must be an object")
    _reject_unknown_keys(value, _ALLOWED_TERMINAL_ERROR_FIELDS, context=context)
    _require_keys(value, ("code", "retryable"), context=context)
    code = value["code"]
    if not isinstance(code, str) or code not in _ALLOWED_DOMAIN_ERROR_CODES:
        raise CursorProtocolError(
            f"{context}.code must be a domain_error_code"
        )
    retryable = value["retryable"]
    if not isinstance(retryable, bool):
        raise CursorProtocolError(f"{context}.retryable must be a boolean")
    if "message" in value and not isinstance(value["message"], str):
        raise CursorProtocolError(f"{context}.message must be a string")
    if "request_id" in value and not isinstance(value["request_id"], str):
        raise CursorProtocolError(f"{context}.request_id must be a string")

    for field, maximum in (("message", 2048), ("request_id", 64)):
        if field in value and len(value[field]) > maximum:
            raise CursorProtocolError(f"{context}.{field} exceeds {maximum} characters")


def _validate_usage_record(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise CursorProtocolError("usage_record must be an object")
    _reject_unknown_keys(value, _ALLOWED_USAGE_RECORD_FIELDS, context="usage_record")
    _require_keys(
        value,
        ("run_id", "session_id", "observed_at", "units", "unit_kind"),
        context="usage_record",
    )
    for uuid_field in ("run_id", "session_id"):
        if not isinstance(value[uuid_field], str) or not value[uuid_field]:
            raise CursorProtocolError(
                f"usage_record.{uuid_field} must be a string"
            )
    if "registration_id" in value and not isinstance(
        value["registration_id"], str
    ):
        raise CursorProtocolError("usage_record.registration_id must be a string")
    if "connection_id" in value and not isinstance(value["connection_id"], str):
        raise CursorProtocolError("usage_record.connection_id must be a string")
    provider_model_id = value.get("provider_model_id")
    if "provider_model_id" in value and not isinstance(provider_model_id, str):
        raise CursorProtocolError(
            "usage_record.provider_model_id must be a string"
        )
    if not isinstance(value["observed_at"], str) or not value["observed_at"]:
        raise CursorProtocolError("usage_record.observed_at must be a string")
    units = value["units"]
    if (
        not isinstance(units, (int, float))
        or isinstance(units, bool)
        or units < 0
    ):
        raise CursorProtocolError(
            "usage_record.units must be a non-negative number"
        )
    unit_kind = value["unit_kind"]
    if (
        not isinstance(unit_kind, str)
        or unit_kind not in _ALLOWED_USAGE_UNIT_KINDS
    ):
        raise CursorProtocolError(
            f"usage_record.unit_kind must be one of {sorted(_ALLOWED_USAGE_UNIT_KINDS)}"
        )
    for optional_number in ("settled_amount", "estimate_amount"):
        if optional_number in value:
            amount = value[optional_number]
            if amount is not None and (
                not isinstance(amount, (int, float))
                or isinstance(amount, bool)
            ):
                raise CursorProtocolError(
                    f"usage_record.{optional_number} must be a number or null"
                )
    if "currency" in value:
        currency = value["currency"]
        if currency is not None and not isinstance(currency, str):
            raise CursorProtocolError(
                "usage_record.currency must be a string or null"
            )

    for field in ("run_id", "session_id", "registration_id", "connection_id"):
        if field in value:
            try:
                uuid.UUID(value[field])
                if not re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value[field]):
                    raise ValueError()
            except ValueError as exc:
                raise CursorProtocolError(f"usage_record.{field} must be a UUID") from exc
    timestamp = value["observed_at"]
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)", timestamp):
            raise ValueError()
        datetime.fromisoformat(timestamp.upper().replace("Z", "+00:00"))
    except ValueError as exc:
        raise CursorProtocolError("usage_record.observed_at must be a timestamp") from exc
    for field, maximum in (("provider_model_id", 256), ("currency", 8)):
        if value.get(field) is not None and len(value[field]) > maximum:
            raise CursorProtocolError(f"usage_record.{field} exceeds {maximum} characters")


def _validate_event_payload(kind: str, payload: Any) -> str | None:
    if kind == "run.started" or kind == "run.cancelling":
        if payload is None:
            return None
        if isinstance(payload, Mapping) and len(payload) == 0:
            return None
        raise CursorProtocolError(f"{kind} payload must be None or an empty object")
    if kind == "content.delta":
        if not isinstance(payload, Mapping):
            raise CursorProtocolError("content.delta payload must be an object")
        _reject_unknown_keys(
            payload, frozenset({"channel", "delta"}), context="content.delta"
        )
        channel = payload.get("channel")
        delta = payload.get("delta")
        if (
            not isinstance(channel, str)
            or channel not in _ALLOWED_DELTA_CHANNELS
        ):
            raise CursorProtocolError(
                f"content.delta.channel must be one of {sorted(_ALLOWED_DELTA_CHANNELS)}"
            )
        if not isinstance(delta, str) or len(delta) > 65536:
            raise CursorProtocolError(
                "content.delta requires a delta string of at most 65536 characters"
            )
        return None
    if kind == "tool.requested":
        if not isinstance(payload, Mapping):
            raise CursorProtocolError("tool.requested payload must be an object")
        _reject_unknown_keys(
            payload, frozenset({"tool_call"}), context="tool.requested"
        )
        tool_call = payload.get("tool_call")
        if not isinstance(tool_call, Mapping):
            raise CursorProtocolError("tool.requested payload requires tool_call")
        _reject_unknown_keys(
            tool_call,
            frozenset({"call_id", "tool_name", "arguments"}),
            context="tool.requested tool_call",
        )
        call_id = tool_call.get("call_id")
        tool_name = tool_call.get("tool_name")
        if not isinstance(call_id, str) or not call_id:
            raise CursorProtocolError(
                "tool.requested requires a non-empty call_id"
            )
        if not isinstance(tool_name, str) or not tool_name:
            raise CursorProtocolError(
                "tool.requested requires a non-empty tool_name"
            )
        if len(call_id) > 128 or len(tool_name) > 128:
            raise CursorProtocolError("tool call identifiers exceed 128 characters")
        if "arguments" not in tool_call:
            raise CursorProtocolError("tool.requested requires tool_call.arguments")
        return call_id
    if kind == "usage.observed":
        if not isinstance(payload, Mapping):
            raise CursorProtocolError("usage.observed payload must be an object")
        _reject_unknown_keys(
            payload, frozenset({"usage"}), context="usage.observed"
        )
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            raise CursorProtocolError("usage.observed requires a usage object")
        _validate_usage_record(usage)
        return None
    if not isinstance(payload, Mapping):
        raise CursorProtocolError(f"{kind} payload must be an object")
    _reject_unknown_keys(payload, frozenset({"terminal_result"}), context=kind)
    terminal = payload.get("terminal_result")
    if not isinstance(terminal, Mapping):
        raise CursorProtocolError(f"{kind} payload must carry terminal_result")
    _reject_unknown_keys(
        terminal,
        frozenset({"outcome", "error"}),
        context=f"{kind} terminal_result",
    )
    expected_outcome = _TERMINAL_OUTCOME_BY_KIND[kind]
    if terminal.get("outcome") != expected_outcome:
        raise CursorProtocolError(
            f"{kind} terminal_result.outcome must be {expected_outcome!r}"
        )
    if "error" in terminal:
        _validate_terminal_error(
            terminal["error"], context=f"{kind} terminal_result.error"
        )
    return None


@dataclass(frozen=True, slots=True)
class CursorStartRequest:
    run_id: str
    session_id: str
    connection_id: str
    provider_model_id: str
    input_messages: tuple[Any, ...] = ()
    tools: tuple[Any, ...] = ()
    continuation_handle: str | None = None


@dataclass(frozen=True, slots=True)
class CursorSdkEvent:
    kind: str
    payload: Any = None


@runtime_checkable
class CursorSdkSessionPort(Protocol):
    def submit_tool_result(
        self, call_id: str, result: Any
    ) -> SubmitToolResultProviderResult: ...

    def request_cancel(self, *, deadline: str) -> CancelProviderRunResult: ...

    def close(self) -> None: ...


@runtime_checkable
class CursorSdkRuntimePort(Protocol):
    def start(
        self,
        request: CursorStartRequest,
        on_event: Callable[[CursorSdkEvent], None],
    ) -> CursorSdkSessionPort: ...


class CursorProviderRunHandle:
    """Per-run state with ordered, lock-free sink delivery.

    Concurrent and reentrant callbacks enqueue behind the current publisher.
    That publisher drains the queue and reports sink failures after draining;
    state is never rolled back after delivery failures.
    """

    def __init__(
        self,
        *,
        run_id: str,
        sink: ProviderRunEventSink,
        now: Callable[[], str],
    ) -> None:
        self._run_id = run_id
        self._sink = sink
        self._now = now
        self._lock = threading.RLock()
        self._session: CursorSdkSessionPort | None = None
        self._started = False
        self._terminal_kind: str | None = None
        self._outstanding_call_id: str | None = None
        self._forward_pending: str | None = None
        self._tool_receipts: dict[str, Any] = {}
        self._session_closed = False
        self._close_ready = False
        self._close_dispatched = False
        self._active_operations = 0
        self._publication_queue: deque[ProviderRunEvent] = deque()
        self._publishing = False

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def is_terminal(self) -> bool:
        with self._lock:
            return self._terminal_kind is not None

    @property
    def outstanding_call_id(self) -> str | None:
        with self._lock:
            return self._outstanding_call_id

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._session_closed

    def bind(self, session: CursorSdkSessionPort) -> None:
        with self._lock:
            if self._session is not None:
                raise CursorProtocolError("sdk session is already bound")
            self._session = session
            session_to_close = self._take_close_locked()
        if session_to_close is not None:
            session_to_close.close()

    def on_sdk_event(self, event: CursorSdkEvent) -> None:
        if not isinstance(event, CursorSdkEvent):
            raise CursorProtocolError("sdk event must be a CursorSdkEvent")
        kind = event.kind
        if not isinstance(kind, str) or kind not in _ALLOWED_SDK_EVENT_KINDS:
            raise CursorProtocolError(f"unknown sdk event kind: {kind!r}")
        detached = _detached_json(event.payload)

        with self._lock:
            if self._terminal_kind is not None:
                raise CursorProtocolError(
                    "sdk event arrived after terminal event"
                )
            if not self._started:
                if kind != "run.started":
                    raise CursorProtocolError(
                        "first sdk event must be run.started"
                    )
            elif kind == "run.started":
                raise CursorProtocolError("duplicate run.started event")
            call_id = _validate_event_payload(kind, detached)
            if kind == "tool.requested" and call_id in self._tool_receipts:
                raise CursorProtocolError("tool call id was already completed")
            if self._outstanding_call_id is not None:
                if kind == "tool.requested":
                    raise CursorProtocolError(
                        "tool call is already outstanding"
                    )
                if kind == "run.completed":
                    raise CursorProtocolError(
                        "run.completed arrived with a tool call outstanding"
                    )
            if kind == "run.started":
                self._started = True
            elif kind == "tool.requested":
                self._outstanding_call_id = call_id
            elif kind in _TERMINAL_SDK_EVENT_KINDS:
                self._terminal_kind = kind
                self._outstanding_call_id = None
                self._forward_pending = None
            if kind in _TERMINAL_SDK_EVENT_KINDS:
                self._session_closed = True
            provider_event = ProviderRunEvent(
                kind=kind,
                run_id=self._run_id,
                observed_at=self._now(),
                payload=detached,
            )
            self._publication_queue.append(provider_event)
            if self._publishing:
                return
            self._publishing = True

        first_error: BaseException | None = None
        while True:
            with self._lock:
                if not self._publication_queue:
                    self._publishing = False
                    break
                queued = self._publication_queue.popleft()
            try:
                self._sink.publish_provider_event(queued)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            finally:
                if queued.kind in _TERMINAL_SDK_EVENT_KINDS:
                    try:
                        self.close_session()
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
        if first_error is not None:
            raise first_error

    def submit_tool_result(
        self, call_id: str, result: Any
    ) -> SubmitToolResultProviderResult:
        forwarded_to_sdk: Any = None
        receipt: Any = None
        session: CursorSdkSessionPort | None = None
        sdk_outcome: SubmitToolResultProviderResult | None = None
        sdk_error: BaseException | None = None
        try:
            with self._lock:
                if (
                    self._terminal_kind is not None
                    or self._session_closed
                ):
                    return SubmitToolResultProviderResult(
                        outcome=SubmitToolResultProviderOutcome.REJECTED
                    )
                if call_id in self._tool_receipts:
                    if self._tool_receipts[call_id] == result:
                        return SubmitToolResultProviderResult(
                            outcome=SubmitToolResultProviderOutcome.ACCEPTED
                        )
                    return SubmitToolResultProviderResult(
                        outcome=SubmitToolResultProviderOutcome.REJECTED
                    )
                if self._forward_pending is not None:
                    return SubmitToolResultProviderResult(
                        outcome=SubmitToolResultProviderOutcome.REJECTED
                    )
                if (
                    self._session is None
                    or self._outstanding_call_id is None
                    or call_id != self._outstanding_call_id
                ):
                    return SubmitToolResultProviderResult(
                        outcome=SubmitToolResultProviderOutcome.REJECTED
                    )
                # Deep detach the receipt before any SDK call so the SDK
                # cannot mutate the value later compared for replay.
                forwarded_to_sdk = copy.deepcopy(result)
                receipt = copy.deepcopy(result)
                self._forward_pending = call_id
                session = self._session
                self._active_operations += 1

            assert session is not None
            sdk_outcome = session.submit_tool_result(call_id, forwarded_to_sdk)
        except BaseException as exc:
            sdk_error = exc

        with self._lock:
            self._forward_pending = None
            if session is not None:
                self._active_operations -= 1
            if sdk_error is None and sdk_outcome.outcome == SubmitToolResultProviderOutcome.ACCEPTED:
                self._outstanding_call_id = None
                self._tool_receipts[call_id] = receipt
            session_to_close = self._take_close_locked()
        if session_to_close is not None:
            session_to_close.close()
        if sdk_error is not None:
            return SubmitToolResultProviderResult(
                outcome=SubmitToolResultProviderOutcome.REJECTED
            )
        return sdk_outcome

    def request_cancel(self, *, deadline: str) -> CancelProviderRunResult:
        with self._lock:
            if self._terminal_kind is not None or self._session_closed or self._session is None:
                return CancelProviderRunResult(
                    request_accepted=False,
                    termination_status=ProviderCancelTerminationStatus.UNKNOWN,
                )
            session = self._session
            self._active_operations += 1
        try:
            return session.request_cancel(deadline=deadline)
        finally:
            with self._lock:
                self._active_operations -= 1
                session_to_close = self._take_close_locked()
            if session_to_close is not None:
                session_to_close.close()

    def close_session(self) -> None:
        with self._lock:
            self._session_closed = True
            self._close_ready = True
            session = self._take_close_locked()
        if session is not None:
            session.close()

    def _take_close_locked(self) -> CursorSdkSessionPort | None:
        if (not self._close_ready or self._close_dispatched
                or self._active_operations or self._session is None):
            return None
        self._close_dispatched = True
        return self._session


class CursorExecutionCoordinator:
    """Provider execution port for the cursor SDK.

    Owns active ``CursorProviderRunHandle`` instances for in-flight runs and a
    bounded-replay set of terminalized handles so that ``get_handle`` keeps
    returning the same handle after it has left the active list, for the
    duration of ``replay_retention_seconds``. ``open_run_ids`` reports only
    the active set; the replay set is internal.
    """

    DEFAULT_REPLAY_RETENTION_SECONDS = 300.0

    def __init__(
        self,
        runtime: CursorSdkRuntimePort,
        *,
        now: Callable[[], str] | None = None,
        replay_retention_seconds: float | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._runtime = runtime
        self._now = now or _utc_now
        self._lock = threading.RLock()
        self._handles: dict[str, CursorProviderRunHandle] = {}
        self._terminal_handles: dict[
            str, tuple[CursorProviderRunHandle, float]
        ] = {}
        self._closed = False
        self._replay_retention_seconds = (
            replay_retention_seconds
            if replay_retention_seconds is not None
            else self.DEFAULT_REPLAY_RETENTION_SECONDS
        )
        if self._replay_retention_seconds < 0:
            raise ValueError("replay_retention_seconds must be non-negative")
        self._monotonic = monotonic or time.monotonic

    @property
    def open_run_ids(self) -> tuple[str, ...]:
        with self._lock:
            self._harvest_terminal_handles_locked()
            return tuple(self._handles)

    def get_handle(self, run_id: str) -> CursorProviderRunHandle | None:
        with self._lock:
            self._harvest_terminal_handles_locked()
            handle = self._handles.get(run_id)
            if handle is not None:
                return handle
            entry = self._terminal_handles.get(run_id)
            return entry[0] if entry is not None else None

    def start(
        self, request: RunRequest, sink: ProviderRunEventSink
    ) -> ProviderRunHandle:
        snapshot = request.route_snapshot
        if snapshot.provider_id != CURSOR_PROVIDER_ID:
            raise CursorProtocolError(
                f"route provider_id {snapshot.provider_id!r} is not the cursor provider"
            )
        adapter_request = CursorStartRequest(
            run_id=request.run_id,
            session_id=request.session_id,
            connection_id=snapshot.connection_id,
            provider_model_id=snapshot.provider_model_id,
            input_messages=tuple(
                copy.deepcopy(message) for message in request.input.messages
            ),
            tools=tuple(copy.deepcopy(tool) for tool in request.tools),
            continuation_handle=None,
        )
        handle = CursorProviderRunHandle(
            run_id=request.run_id, sink=sink, now=self._now
        )
        with self._lock:
            if self._closed:
                raise CursorCoordinatorClosedError("coordinator is closed")
            self._harvest_terminal_handles_locked()
            if request.run_id in self._handles or request.run_id in self._terminal_handles:
                raise CursorDuplicateStartError(request.run_id)
            self._handles[request.run_id] = handle
        try:
            session = self._runtime.start(adapter_request, handle.on_sdk_event)
        except Exception:
            with self._lock:
                if self._handles.get(request.run_id) is handle:
                    self._handles.pop(request.run_id, None)
                retained = self._terminal_handles.get(request.run_id)
                if retained is not None and retained[0] is handle:
                    self._terminal_handles.pop(request.run_id, None)
            raise
        handle.bind(session)
        return handle

    def close(self) -> None:
        with self._lock:
            self._closed = True
            handles = list(self._handles.values())
            self._handles.clear()
        first_error: BaseException | None = None
        for handle in handles:
            try:
                handle.close_session()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _harvest_terminal_handles_locked(self) -> None:
        """Move terminalized handles out of ``_handles`` into the replay set
        and prune expired entries.

        The harvest happens lazily on read so the hot run path does not need
        a coordinator-wide hook for every terminal event; terminalization
        is detected by reading each handle under its own state lock. This
        method runs with the coordinator lock held; handle code must never
        call external code while retaining its state lock.
        """
        if not self._handles and not self._terminal_handles:
            return
        now_monotonic = self._monotonic()
        if self._handles:
            moved: list[tuple[str, CursorProviderRunHandle]] = []
            for run_id, handle in self._handles.items():
                if handle.is_terminal or handle.is_closed:
                    moved.append((run_id, handle))
            for run_id, handle in moved:
                self._handles.pop(run_id, None)
                self._terminal_handles[run_id] = (
                    handle,
                    now_monotonic + self._replay_retention_seconds,
                )
        if self._terminal_handles:
            expired = [
                run_id
                for run_id, (_, expires_at) in self._terminal_handles.items()
                if expires_at <= now_monotonic
            ]
            for run_id in expired:
                self._terminal_handles.pop(run_id, None)
