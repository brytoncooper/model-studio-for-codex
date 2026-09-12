from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import threading
from typing import Any, Callable

from model_deck.engine.runs.ports import (
    CancelProviderRunResult, ProviderCancelTerminationStatus, ProviderRunEvent,
    ProviderRunEventSink, RunRequest, SubmitToolResultProviderOutcome,
    SubmitToolResultProviderResult,
)
from model_deck.plugins.process_runtime.channel import ProviderChannel, ProviderMethod
from model_deck_contracts import validate_schema_ref, validate_tool_input_schema

MAX_EVENTS = 256
MAX_BYTES = 1_048_576
TERMINALS = {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}
ENVELOPE = {"kind", "run_id", "session_id", "sequence", "event_schema_version", "observed_at"}


class ProviderProxyError(RuntimeError):
    """Fixed, payload-free diagnostic suitable for an application boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _document(value: Any) -> tuple[Any, int]:
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > MAX_BYTES:
            raise ProviderProxyError("queue_limit")
        return json.loads(encoded), len(encoded)
    except (ValueError, TypeError, RecursionError):
        raise ProviderProxyError("invalid_json") from None


def _validate(method: ProviderMethod | str, direction: str, value: Any) -> None:
    name = str(method.value if isinstance(method, ProviderMethod) else method).rsplit(".", 1)[-1]
    try:
        validate_schema_ref(f"contracts/plugin.v1/provider/{name}.{direction}.schema.json", value)
    except Exception:
        raise ProviderProxyError("invalid_provider_schema") from None


def _start_params(request: RunRequest) -> dict[str, Any]:
    route = request.route_snapshot
    if route.endpoint_config_ref is None:
        raise ProviderProxyError("unsupported_capability")
    tools = []
    names = set()
    for tool in request.tools:
        if not tool.name or tool.name in names:
            raise ProviderProxyError("invalid_tool_definition")
        names.add(tool.name)
        try:
            schema = validate_tool_input_schema(tool.input_schema)
        except Exception:
            raise ProviderProxyError("invalid_tool_definition") from None
        item = {"name": tool.name, "input_schema": schema,
                "host_execution_required": tool.host_execution_required}
        if tool.description is not None:
            item["description"] = tool.description
        tools.append(item)
    params = {
        "run_request": {"run_id": request.run_id, "session_id": request.session_id,
                        "client_request_id": request.client_request_id,
                        "idempotency_key": request.idempotency_key,
                        "registration_id": route.registration_id,
                        "input": {"messages": list(request.input.messages)}, "tools": tools},
        "route_snapshot": {"registration_id": route.registration_id,
                           "connection_id": route.connection_id, "provider_id": route.provider_id,
                           "provider_model_id": route.provider_model_id,
                           "execution_mode": route.execution_mode.value,
                           "endpoint_config_ref": route.endpoint_config_ref,
                           "capabilities": {"features": {feature.name: feature.state.value
                                              for feature in route.capability_features or ()}}},
    }
    params, _ = _document(params)
    _validate(ProviderMethod.START, "params", params)
    return params


@dataclass
class _Run:
    request: RunRequest
    sink: ProviderRunEventSink
    handle: str | None = None
    next_sequence: int = 0
    terminal: bool = False
    outstanding: set[str] = field(default_factory=set)
    seen_calls: set[str] = field(default_factory=set)
    queued: deque = field(default_factory=deque)
    forwarding: set[str] = field(default_factory=set)
    interruption: ProviderRunEvent | None = None


class ExternalProviderExecution:
    """One proxy, one authenticated activation, one event reader.

    The injected clock returns an aware datetime. At most 256 run identities are
    admitted per proxy lifetime; tombstones prevent worker handle reuse. Create a
    fresh proxy only for a fresh activation, never to replay billed work.
    """

    def __init__(self, channel: ProviderChannel, *, provider_id: str,
                 clock: Callable[[], datetime], request_timeout_s: float = 5.0):
        if not math.isfinite(request_timeout_s) or not 0 < request_timeout_s <= 60:
            raise ValueError("request_timeout_s must be in (0, 60]")
        self._channel = channel
        self._activation = channel.activation_id
        self._provider_id = provider_id
        self._clock = clock
        self._timeout = request_timeout_s
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._runs: dict[str, _Run] = {}
        self._handles: dict[str, _Run] = {}
        self._queued_events = 0
        self._queued_bytes = 0
        self._failure: str | None = None
        self._stop = threading.Event()
        self._pump = threading.Thread(target=self._receive, name="provider-event-pump", daemon=True)
        self._pump.start()

    def _check(self) -> None:
        if self._failure is not None or self._stop.is_set():
            raise ProviderProxyError(self._failure or "proxy_closed")
        if self._channel.activation_id != self._activation:
            raise ProviderProxyError("activation_changed")

    def _request(self, method: ProviderMethod, params: dict, *, timeout_s: float | None = None) -> dict:
        self._check()
        params, _ = _document(params)
        _validate(method, "params", params)
        try:
            result = self._channel.request(method, params, timeout_s=timeout_s or self._timeout)
            result, _ = _document(result)
            _validate(method, "result", result)
            self._check()
            return result
        except Exception:
            self._interrupt("provider_channel_failure")
            raise ProviderProxyError("provider_channel_failure") from None

    def start(self, request: RunRequest, sink: ProviderRunEventSink) -> _Handle:
        params = _start_params(request)
        with self._lock:
            self._check()
            if request.route_snapshot.provider_id != self._provider_id:
                raise ProviderProxyError("foreign_provider")
            if request.run_id in self._runs:
                raise ProviderProxyError("duplicate_run")
            if len(self._runs) >= MAX_EVENTS:
                raise ProviderProxyError("activation_run_limit")
            run = _Run(request, sink)
            self._runs[request.run_id] = run
            # A bounded identity count also bounds delivery workers. User callbacks
            # may block; separate run workers preserve unrelated-run progress.
            threading.Thread(target=self._deliver_run, args=(run,),
                             name="provider-run-delivery", daemon=True).start()
        # Reserve the engine identity before dispatch: notifications may precede
        # the response and several starts may be in flight simultaneously.
        result = self._request(ProviderMethod.START, params)
        with self._lock:
            self._check()
            handle = result["adapter_run_handle"]
            if handle in self._handles or any(item[0]["adapter_run_handle"] != handle for item in run.queued):
                self._interrupt("foreign_handle")
                raise ProviderProxyError("foreign_handle")
            run.handle = handle
            self._handles[handle] = run
            self._condition.notify_all()
        return _Handle(self, run)

    def _receive(self) -> None:
        try:
            while not self._stop.is_set():
                self._check()
                params = self._channel.receive_event(timeout_s=0.05)
                with self._lock:
                    if self._stop.is_set():
                        return
                    if params is not None:
                        params, size = _document(params)
                        _validate("plugin.v1.provider.event", "params", params)
                        event = params["event"]
                        run = self._runs.get(event["run_id"])
                        if run is None or event["session_id"] != run.request.session_id:
                            raise ProviderProxyError("foreign_run")
                        owner = self._handles.get(params["adapter_run_handle"])
                        if owner is not None and owner is not run:
                            raise ProviderProxyError("foreign_handle")
                        if run.handle is not None and run.handle != params["adapter_run_handle"]:
                            raise ProviderProxyError("foreign_handle")
                        if self._queued_events >= MAX_EVENTS or self._queued_bytes + size > MAX_BYTES:
                            raise ProviderProxyError("queue_limit")
                        run.queued.append((params, size))
                        self._queued_events += 1
                        self._queued_bytes += size
                    self._condition.notify_all()
        except Exception:
            self._interrupt("provider_channel_failure")

    def _deliver_run(self, run: _Run) -> None:
        while True:
            size = 0
            try:
                with self._condition:
                    self._condition.wait_for(lambda: run.interruption is not None
                        or self._stop.is_set()
                        or (run.handle is not None and bool(run.queued) and not run.forwarding))
                    if run.interruption is not None:
                        publication = run.interruption
                        run.interruption = None
                        pending = None
                    elif self._stop.is_set():
                        return
                    else:
                        pending, size = run.queued.popleft()
                        publication = self._reserve_publication(run, pending)
                # Never invoke callbacks or wait on the channel under the proxy lock.
                run.sink.publish_provider_event(publication)
                if pending is not None and not self._stop.is_set():
                    self._request(ProviderMethod.ACK, {"adapter_run_handle": run.handle,
                                                      "sequence": pending["sequence"]})
            except Exception:
                self._interrupt("provider_channel_failure")
            finally:
                if size:
                    with self._condition:
                        # Include in-flight callback/ACK payloads in aggregate limits.
                        self._queued_events -= 1
                        self._queued_bytes -= size

    def _reserve_publication(self, run: _Run, params: dict) -> ProviderRunEvent:
        event = params["event"]
        kind = event["kind"]
        if (run.terminal or params["sequence"] != run.next_sequence
                or event["sequence"] != params["sequence"] or event["event_schema_version"] != 1
                or kind == "run.accepted"):
            raise ProviderProxyError("invalid_event_transition")
        if kind == "tool.requested":
            call = event["tool_call"]
            if (not call["call_id"] or call["call_id"] in run.seen_calls
                    or len(run.seen_calls) >= MAX_EVENTS
                    or call["tool_name"] not in {tool.name for tool in run.request.tools}):
                raise ProviderProxyError("invalid_tool_call")
            run.outstanding.add(call["call_id"])
            run.seen_calls.add(call["call_id"])
        if kind in TERMINALS:
            if event["terminal_result"].get("outcome") != kind.removeprefix("run."):
                raise ProviderProxyError("invalid_terminal")
            if kind == "run.completed" and run.outstanding:
                raise ProviderProxyError("unresolved_tool")
        # The wire event nests its call descriptor. The engine provider port
        # carries that descriptor directly as the tool.requested payload.
        payload = (dict(event["tool_call"]) if kind == "tool.requested" else
                   {key: value for key, value in event.items() if key not in ENVELOPE})
        # Reserve order and terminal ownership before allowing callback reentry.
        run.next_sequence += 1
        run.terminal = kind in TERMINALS
        return ProviderRunEvent(kind, run.request.run_id, event["observed_at"], payload or None)

    def _interrupt(self, code: str) -> None:
        with self._lock:
            if self._failure is not None:
                return
            self._failure = code
            self._stop.set()
            observed_at = self._clock().astimezone(timezone.utc).isoformat()
            for run in tuple(self._runs.values()):
                while run.queued:
                    _, size = run.queued.popleft()
                    self._queued_events -= 1
                    self._queued_bytes -= size
                if run.terminal:
                    continue
                run.terminal = True
                run.interruption = ProviderRunEvent(
                    "run.interrupted", run.request.run_id, observed_at,
                    {"terminal_result": {"outcome": "interrupted"}})
            self._condition.notify_all()

    def close(self) -> None:
        """Stop this reader, interrupt active runs; never close/kill the runtime."""
        self._interrupt("proxy_closed")
        if threading.current_thread() is not self._pump:
            self._pump.join(timeout=self._timeout + 0.1)


class _Handle:
    def __init__(self, proxy: ExternalProviderExecution, run: _Run):
        self._proxy = proxy
        self._run = run

    def submit_tool_result(self, call_id: str, result: Any) -> SubmitToolResultProviderResult:
        proxy, run = self._proxy, self._run
        with proxy._lock:
            proxy._check()
            if run.terminal or call_id not in run.outstanding or call_id in run.forwarding:
                raise ProviderProxyError("tool_not_outstanding")
            run.forwarding.add(call_id)
        try:
            response = proxy._request(ProviderMethod.SUBMIT_TOOL_RESULT,
                                     {"adapter_run_handle": run.handle, "call_id": call_id, "result": result})
            with proxy._lock:
                if response["accepted"]:
                    run.outstanding.discard(call_id)
            return SubmitToolResultProviderResult(SubmitToolResultProviderOutcome.ACCEPTED
                    if response["accepted"] else SubmitToolResultProviderOutcome.REJECTED)
        finally:
            with proxy._condition:
                run.forwarding.discard(call_id)
                proxy._condition.notify_all()

    def request_cancel(self, *, deadline: str) -> CancelProviderRunResult:
        proxy, run = self._proxy, self._run
        try:
            target = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            now = proxy._clock()
            if target.tzinfo is None or now.tzinfo is None:
                raise ValueError()
            remaining = (target - now).total_seconds()
        except (ValueError, TypeError, AttributeError):
            raise ProviderProxyError("invalid_deadline") from None
        if remaining <= 0:
            return CancelProviderRunResult(False, ProviderCancelTerminationStatus.UNKNOWN)
        if not proxy._lock.acquire(timeout=remaining):
            return CancelProviderRunResult(False, ProviderCancelTerminationStatus.UNKNOWN)
        try:
            proxy._check()
            if run.terminal:
                return CancelProviderRunResult(False, ProviderCancelTerminationStatus.UNKNOWN)
        finally:
            proxy._lock.release()
        # Lock contention consumes the caller's deadline, not a fresh budget.
        remaining = (target - proxy._clock()).total_seconds()
        if remaining <= 0:
            return CancelProviderRunResult(False, ProviderCancelTerminationStatus.UNKNOWN)
        milliseconds = min(60000, max(1, math.ceil(remaining * 1000)))
        response = proxy._request(ProviderMethod.CANCEL,
                                 {"adapter_run_handle": run.handle, "deadline_ms": milliseconds},
                                 timeout_s=min(proxy._timeout, remaining))
        status = ProviderCancelTerminationStatus.UNKNOWN
        if "confirmed" in response:
            status = (ProviderCancelTerminationStatus.CONFIRMED if response["confirmed"]
                      else ProviderCancelTerminationStatus.UNCONFIRMED)
        return CancelProviderRunResult(response["accepted"], status)
