from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from model_deck.engine.routing.ports import (
    CapabilityTriState,
    ExecutionMode,
    RouteSnapshot,
)
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
    TerminalOutcome,
)

DETERMINISTIC_PROVIDER_ID = "com.modeldeck.provider.deterministic"
DEFAULT_OBSERVED_AT = "2026-01-01T00:00:00Z"

DETERMINISTIC_FAILED_ERROR: dict[str, Any] = {
    "code": "internal",
    "message": "Deterministic fixture failed.",
    "retryable": False,
}
DETERMINISTIC_INTERRUPTED_ERROR: dict[str, Any] = {
    "code": "interrupted",
    "message": "Deterministic fixture interrupted.",
    "retryable": False,
}
DETERMINISTIC_CRASH_FAILED_ERROR: dict[str, Any] = {
    "code": "internal",
    "message": "Deterministic fixture crash failed.",
    "retryable": False,
}
DETERMINISTIC_CRASH_INTERRUPTED_ERROR: dict[str, Any] = {
    "code": "interrupted",
    "message": "Deterministic fixture crash interrupted.",
    "retryable": False,
}


class DeterministicProviderError(Exception):
    pass


class DeterministicDuplicateStartError(DeterministicProviderError):
    pass


class DeterministicRouteMismatchError(DeterministicProviderError):
    pass


class DeterministicCapabilityRejectedError(DeterministicProviderError):
    pass


class DeterministicRunClosedError(DeterministicProviderError):
    pass


class DeterministicScriptExhaustedError(DeterministicProviderError):
    pass


@dataclass(frozen=True, slots=True)
class EmitStarted:
    observed_at: str = DEFAULT_OBSERVED_AT


@dataclass(frozen=True, slots=True)
class EmitContent:
    delta: str
    channel: str = "text"
    observed_at: str = DEFAULT_OBSERVED_AT


@dataclass(frozen=True, slots=True)
class EmitToolRequested:
    call_id: str
    tool_name: str
    arguments: Any = None
    observed_at: str = DEFAULT_OBSERVED_AT


@dataclass(frozen=True, slots=True)
class EmitUsage:
    usage: Any
    observed_at: str = DEFAULT_OBSERVED_AT


@dataclass(frozen=True, slots=True)
class EmitTerminalCompleted:
    error: Any = None
    observed_at: str = DEFAULT_OBSERVED_AT


@dataclass(frozen=True, slots=True)
class EmitTerminalFailed:
    error: Any = field(default_factory=lambda: dict(DETERMINISTIC_FAILED_ERROR))
    observed_at: str = DEFAULT_OBSERVED_AT


@dataclass(frozen=True, slots=True)
class EmitTerminalCancelled:
    error: Any = None
    observed_at: str = DEFAULT_OBSERVED_AT


@dataclass(frozen=True, slots=True)
class EmitTerminalInterrupted:
    error: Any = field(default_factory=lambda: dict(DETERMINISTIC_INTERRUPTED_ERROR))
    observed_at: str = DEFAULT_OBSERVED_AT


@dataclass(frozen=True, slots=True)
class CrashInterrupted:
    error: Any = field(default_factory=lambda: dict(DETERMINISTIC_CRASH_INTERRUPTED_ERROR))
    observed_at: str = DEFAULT_OBSERVED_AT


@dataclass(frozen=True, slots=True)
class CrashFailed:
    error: Any = field(default_factory=lambda: dict(DETERMINISTIC_CRASH_FAILED_ERROR))
    observed_at: str = DEFAULT_OBSERVED_AT


@dataclass(frozen=True, slots=True)
class HoldCancellation:
    pass


DeterministicScriptStep = (
    EmitStarted
    | EmitContent
    | EmitToolRequested
    | EmitUsage
    | EmitTerminalCompleted
    | EmitTerminalFailed
    | EmitTerminalCancelled
    | EmitTerminalInterrupted
    | CrashInterrupted
    | CrashFailed
    | HoldCancellation
)


def _capability_state(snapshot: RouteSnapshot, name: str) -> CapabilityTriState:
    if snapshot.capability_features is None:
        return CapabilityTriState.UNKNOWN
    for feature in snapshot.capability_features:
        if feature.name == name:
            return feature.state
    return CapabilityTriState.UNKNOWN


def _script_requires_tools(script: tuple[DeterministicScriptStep, ...]) -> bool:
    return any(isinstance(step, EmitToolRequested) for step in script)



def _coerce_terminal_error(error: Any) -> dict[str, Any] | None:
    if error is None:
        return None
    if not isinstance(error, dict):
        raise DeterministicProviderError("terminal error must be a structured object or None")
    code = error.get("code")
    retryable = error.get("retryable")
    if not isinstance(code, str) or not isinstance(retryable, bool):
        raise DeterministicProviderError("terminal error requires code and retryable")
    normalized: dict[str, Any] = {"code": code, "retryable": retryable}
    message = error.get("message")
    if message is not None:
        if not isinstance(message, str):
            raise DeterministicProviderError("terminal error message must be a string")
        normalized["message"] = message
    return normalized


def _terminal_result_payload(outcome_value: str, error: Any) -> dict[str, Any]:
    terminal_result: dict[str, Any] = {"outcome": outcome_value}
    coerced = _coerce_terminal_error(error)
    if coerced is not None:
        terminal_result["error"] = coerced
    return {"terminal_result": terminal_result}


def _terminal_outcome_for_kind(kind: str) -> TerminalOutcome:
    mapping = {
        "run.completed": TerminalOutcome.COMPLETED,
        "run.failed": TerminalOutcome.FAILED,
        "run.cancelled": TerminalOutcome.CANCELLED,
        "run.interrupted": TerminalOutcome.INTERRUPTED,
    }
    return mapping[kind]


class DeterministicProviderRunHandle:
    def __init__(
        self,
        request: RunRequest,
        sink: ProviderRunEventSink,
        script: tuple[DeterministicScriptStep, ...],
    ) -> None:
        self._request = request
        self._sink = sink
        self._script = script
        self._cursor = 0
        self._terminal_emitted = False
        self._outstanding_call_id: str | None = None
        self._tool_results: dict[str, Any] = {}
        self._cancel_requested = False
        self._cancel_hold = False
        self._cancel_deadline: str | None = None
        self._cancel_confirmed = False
        self._last_terminal_kind: str | None = None
        self._published_kinds: list[str] = []

    @property
    def request(self) -> RunRequest:
        return self._request

    @property
    def published_kinds(self) -> tuple[str, ...]:
        return tuple(self._published_kinds)

    @property
    def is_terminal(self) -> bool:
        return self._terminal_emitted

    @property
    def outstanding_call_id(self) -> str | None:
        return self._outstanding_call_id

    def emit_next(self) -> bool:
        if self._terminal_emitted:
            raise DeterministicRunClosedError("terminal event already emitted")
        if self._outstanding_call_id is not None:
            return False
        while self._cursor < len(self._script):
            step = self._script[self._cursor]
            self._cursor += 1
            if isinstance(step, HoldCancellation):
                self._cancel_hold = True
                continue
            if isinstance(step, CrashInterrupted):
                self._emit_terminal("run.interrupted", step.error, step.observed_at)
                return True
            if isinstance(step, CrashFailed):
                self._emit_terminal("run.failed", step.error, step.observed_at)
                return True
            if isinstance(step, EmitStarted):
                self._publish("run.started", None, step.observed_at)
                return True
            if isinstance(step, EmitContent):
                self._publish(
                    "content.delta",
                    {"delta": step.delta, "channel": step.channel},
                    step.observed_at,
                )
                return True
            if isinstance(step, EmitToolRequested):
                self._publish(
                    "tool.requested",
                    {
                        "tool_call": {
                            "call_id": step.call_id,
                            "tool_name": step.tool_name,
                            "arguments": step.arguments,
                        }
                    },
                    step.observed_at,
                )
                self._outstanding_call_id = step.call_id
                return True
            if isinstance(step, EmitUsage):
                self._publish("usage.observed", {"usage": step.usage}, step.observed_at)
                return True
            if isinstance(step, EmitTerminalCompleted):
                self._emit_terminal("run.completed", step.error, step.observed_at)
                return True
            if isinstance(step, EmitTerminalFailed):
                self._emit_terminal("run.failed", step.error, step.observed_at)
                return True
            if isinstance(step, EmitTerminalCancelled):
                self._emit_terminal("run.cancelled", step.error, step.observed_at)
                return True
            if isinstance(step, EmitTerminalInterrupted):
                self._emit_terminal("run.interrupted", step.error, step.observed_at)
                return True
        raise DeterministicScriptExhaustedError("script exhausted without terminal event")

    def advance(self) -> bool:
        return self.emit_next()

    def submit_tool_result(
        self, call_id: str, result: Any
    ) -> SubmitToolResultProviderResult:
        if self._terminal_emitted:
            return SubmitToolResultProviderResult(
                outcome=SubmitToolResultProviderOutcome.REJECTED
            )
        if call_id in self._tool_results:
            if self._tool_results[call_id] == result:
                return SubmitToolResultProviderResult(
                    outcome=SubmitToolResultProviderOutcome.ACCEPTED
                )
            return SubmitToolResultProviderResult(
                outcome=SubmitToolResultProviderOutcome.REJECTED
            )
        if self._outstanding_call_id is None or call_id != self._outstanding_call_id:
            return SubmitToolResultProviderResult(
                outcome=SubmitToolResultProviderOutcome.REJECTED
            )
        self._tool_results[call_id] = result
        self._outstanding_call_id = None
        return SubmitToolResultProviderResult(
            outcome=SubmitToolResultProviderOutcome.ACCEPTED
        )

    def request_cancel(self, *, deadline: str) -> CancelProviderRunResult:
        self._cancel_deadline = deadline
        if self._terminal_emitted:
            if self._last_terminal_kind == "run.cancelled":
                status = ProviderCancelTerminationStatus.CONFIRMED
            else:
                status = ProviderCancelTerminationStatus.UNKNOWN
            return CancelProviderRunResult(
                request_accepted=False,
                termination_status=status,
            )
        self._cancel_requested = True
        if self._cancel_hold:
            return CancelProviderRunResult(
                request_accepted=True,
                termination_status=ProviderCancelTerminationStatus.UNCONFIRMED,
            )
        self._cancel_confirmed = True
        self._emit_terminal("run.cancelled", None, deadline)
        return CancelProviderRunResult(
            request_accepted=True,
            termination_status=ProviderCancelTerminationStatus.CONFIRMED,
        )

    def _publish(self, kind: str, payload: Any, observed_at: str) -> None:
        if self._terminal_emitted:
            raise DeterministicRunClosedError("cannot publish after terminal event")
        event = ProviderRunEvent(
            kind=kind,
            run_id=self._request.run_id,
            observed_at=observed_at,
            payload=payload,
        )
        self._sink.publish_provider_event(event)
        self._published_kinds.append(kind)

    def _emit_terminal(self, kind: str, error: Any, observed_at: str) -> None:
        if self._terminal_emitted:
            raise DeterministicRunClosedError("terminal event already emitted")
        outcome = _terminal_outcome_for_kind(kind)
        payload = _terminal_result_payload(outcome.value, error)
        event = ProviderRunEvent(
            kind=kind,
            run_id=self._request.run_id,
            observed_at=observed_at,
            payload=payload,
        )
        self._sink.publish_provider_event(event)
        self._published_kinds.append(kind)
        self._terminal_emitted = True
        self._last_terminal_kind = kind
        self._outstanding_call_id = None
        if kind == "run.cancelled":
            self._cancel_confirmed = True


class DeterministicProviderExecutionPort:
    def __init__(
        self,
        *,
        provider_id: str = DETERMINISTIC_PROVIDER_ID,
        execution_mode: ExecutionMode | None = ExecutionMode.CUSTOM,
        script: tuple[DeterministicScriptStep, ...] = (),
        auto_advance: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self.execution_mode = execution_mode
        self.script = script
        self.auto_advance = auto_advance
        self._started_run_ids: set[str] = set()
        self._recorded_requests: dict[str, RunRequest] = {}

    def start(self, request: RunRequest, sink: ProviderRunEventSink) -> ProviderRunHandle:
        self._validate_route(request.route_snapshot)
        self._validate_capabilities(request.route_snapshot, self.script)
        if request.run_id in self._started_run_ids:
            raise DeterministicDuplicateStartError(request.run_id)
        self._started_run_ids.add(request.run_id)
        self._recorded_requests[request.run_id] = request
        handle = DeterministicProviderRunHandle(request, sink, self.script)
        if self.auto_advance:
            while not handle.is_terminal:
                if not handle.advance():
                    break
        return handle

    def recorded_request(self, run_id: str) -> RunRequest:
        return self._recorded_requests[run_id]

    def _validate_route(self, snapshot: RouteSnapshot) -> None:
        if snapshot.provider_id != self.provider_id:
            raise DeterministicRouteMismatchError(snapshot.provider_id)
        if self.execution_mode is not None and snapshot.execution_mode != self.execution_mode:
            raise DeterministicRouteMismatchError(snapshot.execution_mode.value)

    def _validate_capabilities(
        self, snapshot: RouteSnapshot, script: tuple[DeterministicScriptStep, ...]
    ) -> None:
        if not _script_requires_tools(script):
            return
        tools_state = _capability_state(snapshot, "tools")
        if tools_state != CapabilityTriState.SUPPORTED:
            raise DeterministicCapabilityRejectedError("tools")
