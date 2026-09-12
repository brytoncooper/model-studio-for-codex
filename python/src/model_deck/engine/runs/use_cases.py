from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from typing import Any

import math

from model_deck.engine.routing.ports import (
    CapabilityFeature,
    CapabilityTriState,
    RouteResolveRequest,
    RouteResolver,
)
from model_deck.engine.runs.ports import (
    ActiveRunState,
    AppendApplicationEventCommand,
    ApplicationEventPublisher,
    ApplicationRunEvent,
    CancelRunCommand,
    ClaimDispatchCommand,
    CompleteTerminalCommand,
    GetRunCommand,
    NormalizedRunInput,
    ProviderExecutionPort,
    ProviderRunEvent,
    ProviderRunEventSink,
    ProviderRunHandle,
    ProviderCancelTerminationStatus,
    RestartRecoveryResult,
    RunAdmissionKey,
    RunAdmissionRequestHashConflictError,
    RunAdmissionResult,
    RunRecord,
    RunRepository,
    RunRequest,
    RunState,
    RunStateConflictError,
    StartRunCommand,
    SubmitToolResultCommand,
    SubmitToolResultResult,
    SubmitToolResultProviderOutcome,
    TERMINAL_RUN_STATES,
    TerminalOutcome,
    TerminalResult,
    ToolCallDescriptor,
    ToolResultIdempotencyConflictError,
)
from model_deck.engine.sessions.ports import GetSessionCommand, SessionRepository

_RUNS_START_OPERATION = "engine.v1.runs.start"
_RUNS_CANCEL_OPERATION = "engine.v1.runs.cancel"
_EVENT_SCHEMA_VERSION = 1

_OPAQUE_REF_PATTERN = re.compile(r"^ref:[a-z][a-z0-9._-]{0,120}$")
_MAX_IDEMPOTENCY_KEY = 128
_MAX_CLIENT_REQUEST_ID = 64
_MAX_TOOL_FIELD = 128
_MAX_MESSAGES = 256
_MAX_TOOLS = 128

_MAX_JSON_STRING = 1 << 20
_MAX_JSON_ARRAY = 4096
_MAX_JSON_OBJECT = 1024
_MAX_JSON_DEPTH = 64
_MAX_RUN_INPUT_TOOLS_PAYLOAD = 1 << 20

_ALLOWED_PROVIDER_EVENT_KINDS: frozenset[str] = frozenset(
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

_PROVIDER_TERMINAL_KINDS: frozenset[str] = frozenset(
    {
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.interrupted",
    }
)

_ACTIVE_STATE_BY_RUN_STATE: dict[RunState, ActiveRunState] = {
    RunState.ACCEPTED: ActiveRunState.ACCEPTED,
    RunState.RUNNING: ActiveRunState.RUNNING,
    RunState.WAITING_FOR_TOOL: ActiveRunState.WAITING_FOR_TOOL,
    RunState.CANCELLING: ActiveRunState.CANCELLING,
}


def _reject_unknown_keys(
    mapping: Mapping[str, Any],
    allowed: frozenset[str],
    *,
    context: str,
) -> None:
    unknown = set(mapping.keys()) - allowed
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unknown {context} field(s): {names}")


def _canonical_uuid_spelling(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed).casefold() == value.casefold()


def _validate_uuid_field(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if not _canonical_uuid_spelling(value):
        raise ValueError(f"{name} must be a UUID")
    return value


def _validate_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("idempotency_key must be a non-empty string")
    if len(value) > _MAX_IDEMPOTENCY_KEY:
        raise ValueError("idempotency_key must be at most 128 characters")
    return value


def _validate_client_request_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("client_request_id must be a non-empty string")
    if len(value) > _MAX_CLIENT_REQUEST_ID:
        raise ValueError("client_request_id must be at most 64 characters")
    return value


def _validate_bounded_string(name: str, value: Any, *, max_length: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > max_length:
        raise ValueError(f"{name} must be at most {max_length} characters")
    return value


def _validate_opaque_ref(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > 128:
        raise ValueError(f"{name} must be at most 128 characters")
    if _OPAQUE_REF_PATTERN.fullmatch(value):
        return value
    if _canonical_uuid_spelling(value):
        return value
    raise ValueError(f"{name} must be a UUID or ref: opaque reference")


def _validate_bounded_json_value(
    name: str,
    value: Any,
    *,
    depth: int = 0,
    containers: set[int] | None = None,
) -> Any:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError(f"{name} exceeds maximum nesting depth")
    if containers is None:
        containers = set()
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a JSON value")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_JSON_STRING:
            raise ValueError(f"{name} must be at most 1048576 characters")
        return value
    if isinstance(value, list):
        container_id = id(value)
        if container_id in containers:
            raise ValueError(f"{name} must not contain circular references")
        containers.add(container_id)
        if len(value) > _MAX_JSON_ARRAY:
            raise ValueError(f"{name} must contain at most 4096 items")
        normalized = [
            _validate_bounded_json_value(
                f"{name}[{index}]",
                item,
                depth=depth + 1,
                containers=containers,
            )
            for index, item in enumerate(value)
        ]
        containers.remove(container_id)
        return normalized
    if isinstance(value, dict):
        container_id = id(value)
        if container_id in containers:
            raise ValueError(f"{name} must not contain circular references")
        containers.add(container_id)
        if len(value) > _MAX_JSON_OBJECT:
            raise ValueError(f"{name} must contain at most 1024 properties")
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{name} must use string object keys")
            normalized[key] = _validate_bounded_json_value(
                f"{name}.{key}",
                item,
                depth=depth + 1,
                containers=containers,
            )
        containers.remove(container_id)
        return normalized
    raise ValueError(f"{name} must be a JSON value")


def _validate_json_value(name: str, value: Any) -> Any:
    return _validate_bounded_json_value(name, value)


def _validate_run_input_tools_payload_size(
    input_block: NormalizedRunInput,
    tools: tuple[ToolCallDescriptor, ...],
) -> None:
    payload = {
        "input": {"messages": list(input_block.messages)},
        "tools": [
            {
                "call_id": tool.call_id,
                "tool_name": tool.tool_name,
                "arguments": tool.arguments,
            }
            for tool in tools
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_RUN_INPUT_TOOLS_PAYLOAD:
        raise ValueError("run input and tools payload exceeds maximum size")


def _validate_input_block(value: Any) -> NormalizedRunInput:
    if not isinstance(value, dict):
        raise ValueError("input must be an object")
    _reject_unknown_keys(value, frozenset({"messages"}), context="input")
    if "messages" not in value:
        return NormalizedRunInput()
    messages = value["messages"]
    if not isinstance(messages, list):
        raise ValueError("input.messages must be an array")
    if len(messages) > _MAX_MESSAGES:
        raise ValueError("input.messages must contain at most 256 items")
    normalized = tuple(_validate_json_value("input.messages[]", item) for item in messages)
    return NormalizedRunInput(messages=normalized)


def _validate_tools_block(value: Any) -> tuple[ToolCallDescriptor, ...]:
    if not isinstance(value, list):
        raise ValueError("tools must be an array")
    if len(value) > _MAX_TOOLS:
        raise ValueError("tools must contain at most 128 items")
    tools: list[ToolCallDescriptor] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"tools[{index}] must be an object")
        _reject_unknown_keys(
            item,
            frozenset({"call_id", "tool_name", "arguments"}),
            context=f"tools[{index}]",
        )
        call_id = _validate_bounded_string(
            f"tools[{index}].call_id",
            item.get("call_id"),
            max_length=_MAX_TOOL_FIELD,
        )
        tool_name = _validate_bounded_string(
            f"tools[{index}].tool_name",
            item.get("tool_name"),
            max_length=_MAX_TOOL_FIELD,
        )
        if "arguments" not in item:
            raise ValueError(f"tools[{index}].arguments is required")
        arguments = _validate_json_value(f"tools[{index}].arguments", item["arguments"])
        tools.append(ToolCallDescriptor(call_id=call_id, tool_name=tool_name, arguments=arguments))
    return tuple(tools)


def _validate_start_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    params = dict(params or {})
    allowed = frozenset(
        {
            "session_id",
            "client_request_id",
            "idempotency_key",
            "registration_id",
            "input",
            "tools",
            "capability_snapshot_ref",
        }
    )
    _reject_unknown_keys(params, allowed, context="params")
    if "input" in params:
        if params["input"] is None:
            raise ValueError("input must be an object")
        input_block = _validate_input_block(params["input"])
    else:
        input_block = NormalizedRunInput()
    if "tools" in params:
        if params["tools"] is None:
            raise ValueError("tools must be an array")
        tools = _validate_tools_block(params["tools"])
    else:
        tools = ()
    _validate_run_input_tools_payload_size(input_block, tools)
    return {
        "session_id": _validate_uuid_field("session_id", params.get("session_id")),
        "client_request_id": _validate_client_request_id(params.get("client_request_id")),
        "idempotency_key": _validate_idempotency_key(params.get("idempotency_key")),
        "registration_id": _validate_uuid_field("registration_id", params.get("registration_id")),
        "input": input_block,
        "tools": tools,
        "capability_snapshot_ref": (
            _validate_opaque_ref("capability_snapshot_ref", params["capability_snapshot_ref"])
            if "capability_snapshot_ref" in params
            else None
        ),
    }


def _canonical_request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request_hash_from_start(validated: dict[str, Any]) -> str:
    payload: dict[str, Any] = {
        "session_id": validated["session_id"],
        "client_request_id": validated["client_request_id"],
        "idempotency_key": validated["idempotency_key"],
        "registration_id": validated["registration_id"],
        "input": {"messages": list(validated["input"].messages)},
        "tools": [
            {
                "call_id": tool.call_id,
                "tool_name": tool.tool_name,
                "arguments": tool.arguments,
            }
            for tool in validated["tools"]
        ],
    }
    if validated["capability_snapshot_ref"] is not None:
        payload["capability_snapshot_ref"] = validated["capability_snapshot_ref"]
    return _canonical_request_hash(payload)


def _run_summary(record: RunRecord) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "run_id": record.run_id,
        "session_id": record.session_id,
        "state": record.state.value,
    }
    if record.client_request_id:
        summary["client_request_id"] = record.client_request_id
    if record.terminal_result is not None:
        terminal: dict[str, Any] = {"outcome": record.terminal_result.outcome.value}
        if record.terminal_result.error is not None:
            terminal["error"] = record.terminal_result.error
        summary["terminal_result"] = terminal
    return summary


def _internal_error(message: str) -> dict[str, Any]:
    return {
        "code": "internal",
        "message": message,
        "retryable": False,
    }


def _active_state(record: RunRecord) -> ActiveRunState:
    mapped = _ACTIVE_STATE_BY_RUN_STATE.get(record.state)
    if mapped is None:
        raise RunStateConflictError(f"run {record.run_id} is not active")
    return mapped


def _terminal_outcome_for_kind(kind: str) -> TerminalOutcome:
    return {
        "run.completed": TerminalOutcome.COMPLETED,
        "run.failed": TerminalOutcome.FAILED,
        "run.cancelled": TerminalOutcome.CANCELLED,
        "run.interrupted": TerminalOutcome.INTERRUPTED,
    }[kind]


def _terminal_error_from_payload(payload: Any) -> Any | None:
    if isinstance(payload, dict):
        if "error" in payload:
            return payload["error"]
        terminal = payload.get("terminal_result")
        if isinstance(terminal, dict) and "error" in terminal:
            return terminal["error"]
    return None


class RunApplicationCoordinator:
    def __init__(
        self,
        run_repository: RunRepository,
        *,
        event_publisher: ApplicationEventPublisher | None = None,
    ) -> None:
        self._runs = run_repository
        self._publisher = event_publisher
        self._handles: dict[str, ProviderRunHandle] = {}

    def provider_sink(self) -> ProviderRunEventSink:
        return _CoordinatorProviderSink(self)

    def register_handle(self, run_id: str, handle: ProviderRunHandle) -> None:
        self._handles[run_id] = handle

    def pop_handle(self, run_id: str) -> ProviderRunHandle | None:
        return self._handles.pop(run_id, None)

    def get_handle(self, run_id: str) -> ProviderRunHandle | None:
        return self._handles.get(run_id)

    def recover_after_restart(
        self,
        provider: ProviderExecutionPort,
        *,
        observed_at: str,
    ) -> RestartRecoveryResult:
        """Recover durable runs without re-admission or route re-resolution."""

        recovery = self._runs.recover_after_restart(observed_at)
        for run_id in recovery.interrupted_run_ids:
            interrupted = self._runs.get(GetRunCommand(run_id=run_id))
            self._publish(
                ApplicationRunEvent(
                    kind="run.interrupted",
                    run_id=interrupted.run_id,
                    session_id=interrupted.session_id,
                    sequence=interrupted.last_sequence,
                    event_schema_version=_EVENT_SCHEMA_VERSION,
                    observed_at=recovery.observed_at,
                    payload={"terminal_result": {"outcome": "interrupted"}},
                )
            )
        for request in recovery.dispatchable_requests:
            self._start_recovered_request(provider, request, observed_at=observed_at)
        return recovery

    def _start_recovered_request(
        self,
        provider: ProviderExecutionPort,
        request: RunRequest,
        *,
        observed_at: str,
    ) -> RunRecord:
        claimed = self._runs.claim_dispatch(
            ClaimDispatchCommand(run_id=request.run_id, dispatch_token=request.run_id)
        )
        try:
            handle = provider.start(request, self.provider_sink())
        except Exception:
            refreshed = self._runs.get(GetRunCommand(run_id=claimed.run_id))
            if refreshed.state in TERMINAL_RUN_STATES:
                return refreshed
            error = _internal_error("provider recovery start failed")
            completion = self._runs.complete_terminal(
                CompleteTerminalCommand(
                    run_id=claimed.run_id,
                    expected_state=_active_state(refreshed),
                    terminal_result=TerminalResult(
                        outcome=TerminalOutcome.INTERRUPTED,
                        error=error,
                    ),
                    final_event_kind="run.interrupted",
                    final_event_payload={
                        "terminal_result": {
                            "outcome": "interrupted",
                            "error": error,
                        }
                    },
                    observed_at=observed_at,
                )
            )
            self._publish(completion.event)
            return completion.run
        refreshed = self._runs.get(GetRunCommand(run_id=claimed.run_id))
        if refreshed.state in TERMINAL_RUN_STATES:
            return refreshed
        self.register_handle(claimed.run_id, handle)
        return refreshed

    def _publish(self, event: ApplicationRunEvent) -> None:
        if self._publisher is not None:
            self._publisher.publish_application_event(event)


class _CoordinatorProviderSink:
    def __init__(self, coordinator: RunApplicationCoordinator) -> None:
        self._coordinator = coordinator

    def publish_provider_event(self, event: ProviderRunEvent) -> None:
        if event.kind not in _ALLOWED_PROVIDER_EVENT_KINDS:
            raise ValueError(f"unknown provider event kind: {event.kind}")
        run = self._coordinator._runs.get(GetRunCommand(run_id=event.run_id))
        if run.state in TERMINAL_RUN_STATES:
            return
        if event.kind in _PROVIDER_TERMINAL_KINDS:
            self._complete_terminal(run, event)
            return
        self._append_non_terminal(run, event)

    def _complete_terminal(self, run: RunRecord, event: ProviderRunEvent) -> None:
        expected = _active_state(run)
        terminal_result = TerminalResult(
            outcome=_terminal_outcome_for_kind(event.kind),
            error=_terminal_error_from_payload(event.payload),
        )
        completion = self._coordinator._runs.complete_terminal(
            CompleteTerminalCommand(
                run_id=run.run_id,
                expected_state=expected,
                terminal_result=terminal_result,
                final_event_kind=event.kind,
                final_event_payload=event.payload,
                observed_at=event.observed_at,
            )
        )
        self._coordinator.pop_handle(run.run_id)
        self._coordinator._publish(completion.event)

    def _append_non_terminal(self, run: RunRecord, event: ProviderRunEvent) -> None:
        expected = _active_state(run)
        new_state = expected
        if event.kind == "tool.requested":
            new_state = ActiveRunState.WAITING_FOR_TOOL
        elif event.kind == "run.cancelling":
            new_state = ActiveRunState.CANCELLING
        elif event.kind in {"content.delta", "usage.observed"}:
            new_state = expected
        elif event.kind == "run.started" and expected == ActiveRunState.ACCEPTED:
            new_state = ActiveRunState.RUNNING
        result = self._coordinator._runs.append_application_event(
            AppendApplicationEventCommand(
                run_id=run.run_id,
                expected_state=expected,
                new_state=new_state,
                kind=event.kind,
                payload=event.payload,
                observed_at=event.observed_at,
            )
        )
        self._coordinator._publish(result.event)


class StartRunUseCase:
    def __init__(
        self,
        run_repository: RunRepository,
        session_repository: SessionRepository,
        route_resolver: RouteResolver,
        provider: ProviderExecutionPort,
        coordinator: RunApplicationCoordinator,
    ) -> None:
        self._runs = run_repository
        self._sessions = session_repository
        self._routes = route_resolver
        self._provider = provider
        self._coordinator = coordinator

    def execute(
        self,
        params: Mapping[str, Any] | None,
        *,
        principal_id: str,
        authorized_host_context_ref: str | None = None,
    ) -> dict[str, Any]:
        validated = _validate_start_params(params)
        if not isinstance(principal_id, str) or not principal_id:
            raise ValueError("principal_id must be a non-empty string")
        admission_key = RunAdmissionKey(
            principal_id=principal_id,
            operation_id=_RUNS_START_OPERATION,
            idempotency_key=validated["idempotency_key"],
        )
        request_hash = _request_hash_from_start(validated)
        replay = self._runs.lookup_admission(admission_key, request_hash)
        if replay is not None:
            return {"run": _run_summary(replay.run)}
        session = self._sessions.get(
            GetSessionCommand(session_id=validated["session_id"])
        )
        if session.registration_id != validated["registration_id"]:
            raise ValueError(
                "registration_id does not match the session selected registration"
            )
        if session.host_context_ref != authorized_host_context_ref:
            raise ValueError(
                "authorized host context does not match session host context"
            )
        capability_requirements = None
        if validated["tools"]:
            capability_requirements = (
                CapabilityFeature("tools", CapabilityTriState.SUPPORTED),
            )
        route = self._routes.resolve_active_registration(
            RouteResolveRequest(
                registration_id=validated["registration_id"],
                capability_snapshot_ref=validated["capability_snapshot_ref"],
                capability_requirements=capability_requirements,
            )
        )
        command = StartRunCommand(
            admission_key=admission_key,
            request_hash=request_hash,
            session_id=validated["session_id"],
            client_request_id=validated["client_request_id"],
            registration_id=validated["registration_id"],
            route_snapshot=route,
            input=validated["input"],
            tools=validated["tools"],
            authorized_host_context_ref=authorized_host_context_ref,
        )
        admission = self._runs.admit(command)
        if not admission.dispatch_required:
            return {"run": _run_summary(admission.run)}
        run = self._dispatch_new_run(
            admission,
            idempotency_key=validated["idempotency_key"],
            normalized_input=validated["input"],
            tools=validated["tools"],
        )
        return {"run": _run_summary(run)}

    def _dispatch_new_run(
        self,
        admission: RunAdmissionResult,
        *,
        idempotency_key: str,
        normalized_input: NormalizedRunInput,
        tools: tuple[ToolCallDescriptor, ...],
    ) -> RunRecord:
        run = admission.run
        claimed = self._runs.claim_dispatch(
            ClaimDispatchCommand(run_id=run.run_id, dispatch_token=run.run_id)
        )
        request = RunRequest(
            run_id=claimed.run_id,
            session_id=claimed.session_id,
            client_request_id=claimed.client_request_id,
            idempotency_key=idempotency_key,
            route_snapshot=claimed.route_snapshot,
            input=normalized_input,
            tools=tools,
        )
        try:
            handle = self._provider.start(request, self._coordinator.provider_sink())
        except Exception:
            refreshed = self._runs.get(GetRunCommand(run_id=claimed.run_id))
            if refreshed.state in TERMINAL_RUN_STATES:
                return refreshed
            expected = _active_state(refreshed)
            completion = self._runs.complete_terminal(
                CompleteTerminalCommand(
                    run_id=claimed.run_id,
                    expected_state=expected,
                    terminal_result=TerminalResult(
                        outcome=TerminalOutcome.INTERRUPTED,
                        error=_internal_error("provider start failed"),
                    ),
                    final_event_kind="run.interrupted",
                    final_event_payload={
                        "terminal_result": {
                            "outcome": "interrupted",
                            "error": _internal_error("provider start failed"),
                        }
                    },
                )
            )
            self._coordinator._publish(completion.event)
            return completion.run
        refreshed = self._runs.get(GetRunCommand(run_id=claimed.run_id))
        if refreshed.state in TERMINAL_RUN_STATES:
            return refreshed
        self._coordinator.register_handle(claimed.run_id, handle)
        return refreshed


class GetRunUseCase:
    def __init__(self, run_repository: RunRepository) -> None:
        self._runs = run_repository

    def execute(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        params = dict(params or {})
        _reject_unknown_keys(params, frozenset({"run_id"}), context="params")
        run_id = _validate_uuid_field("run_id", params.get("run_id"))
        record = self._runs.get(GetRunCommand(run_id=run_id))
        return {"run": _run_summary(record)}


class CancelRunUseCase:
    def __init__(
        self,
        run_repository: RunRepository,
        coordinator: RunApplicationCoordinator,
        *,
        cancel_deadline: str,
    ) -> None:
        self._runs = run_repository
        self._coordinator = coordinator
        self._cancel_deadline = cancel_deadline

    def execute(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        params = dict(params or {})
        _reject_unknown_keys(
            params, frozenset({"run_id", "idempotency_key"}), context="params"
        )
        run_id = _validate_uuid_field("run_id", params.get("run_id"))
        idempotency_key = _validate_idempotency_key(params.get("idempotency_key"))
        cancellation = self._runs.request_cancel(
            CancelRunCommand(run_id=run_id, idempotency_key=idempotency_key)
        )
        if cancellation.event is not None:
            self._coordinator._publish(cancellation.event)
        record = cancellation.run
        if record.state in TERMINAL_RUN_STATES:
            accepted = record.state == RunState.CANCELLED
            return {"accepted": accepted, "state": record.state.value}
        handle = self._coordinator.get_handle(run_id)
        if handle is not None:
            cancel_result = handle.request_cancel(deadline=self._cancel_deadline)
            if (
                cancel_result.termination_status
                == ProviderCancelTerminationStatus.CONFIRMED
            ):
                refreshed = self._runs.get(GetRunCommand(run_id=run_id))
                if refreshed.state not in TERMINAL_RUN_STATES:
                    expected = _active_state(refreshed)
                    completion = self._runs.complete_terminal(
                        CompleteTerminalCommand(
                            run_id=run_id,
                            expected_state=expected,
                            terminal_result=TerminalResult(
                                outcome=TerminalOutcome.CANCELLED,
                                error=None,
                            ),
                            final_event_kind="run.cancelled",
                            final_event_payload={
                                "terminal_result": {"outcome": "cancelled"}
                            },
                            observed_at=self._cancel_deadline,
                        )
                    )
                    self._coordinator._publish(completion.event)
                    self._coordinator.pop_handle(run_id)
        refreshed = self._runs.get(GetRunCommand(run_id=run_id))
        return {"accepted": True, "state": refreshed.state.value}


class SubmitToolResultUseCase:
    def __init__(
        self,
        run_repository: RunRepository,
        coordinator: RunApplicationCoordinator,
    ) -> None:
        self._runs = run_repository
        self._coordinator = coordinator

    def execute(
        self,
        params: Mapping[str, Any] | None,
        *,
        principal_id: str,
        authorized_host_context_ref: str | None = None,
    ) -> dict[str, Any]:
        params = dict(params or {})
        _reject_unknown_keys(
            params,
            frozenset({"run_id", "call_id", "result", "idempotency_key"}),
            context="params",
        )
        if not isinstance(principal_id, str) or not principal_id:
            raise ValueError("principal_id must be a non-empty string")
        run_id = _validate_uuid_field("run_id", params.get("run_id"))
        call_id = _validate_bounded_string(
            "call_id", params.get("call_id"), max_length=_MAX_TOOL_FIELD
        )
        idempotency_key = _validate_idempotency_key(params.get("idempotency_key"))
        if "result" not in params:
            raise ValueError("result is required")
        result = _validate_json_value("result", params["result"])
        submission = self._runs.submit_tool_result(
            SubmitToolResultCommand(
                run_id=run_id,
                call_id=call_id,
                idempotency_key=idempotency_key,
                result=result,
                principal_id=principal_id,
                host_context_ref=authorized_host_context_ref,
            )
        )
        handle = self._coordinator.get_handle(run_id)
        if submission.provider_submission_required:
            if handle is None:
                refreshed = self._runs.get(GetRunCommand(run_id=run_id))
                expected = _active_state(refreshed)
                error = _internal_error(
                    "no active provider handle for tool result submission"
                )
                completion = self._runs.complete_terminal(
                    CompleteTerminalCommand(
                        run_id=run_id,
                        expected_state=expected,
                        terminal_result=TerminalResult(
                            outcome=TerminalOutcome.INTERRUPTED,
                            error=error,
                        ),
                        final_event_kind="run.interrupted",
                        final_event_payload={
                            "terminal_result": {
                                "outcome": "interrupted",
                                "error": error,
                            }
                        },
                    )
                )
                self._coordinator._publish(completion.event)
                self._coordinator.pop_handle(run_id)
                raise RunStateConflictError(
                    "no active provider handle for tool result submission"
                )
            provider_outcome = handle.submit_tool_result(call_id, result)
            if provider_outcome.outcome == SubmitToolResultProviderOutcome.REJECTED:
                refreshed = self._runs.get(GetRunCommand(run_id=run_id))
                expected = _active_state(refreshed)
                error = _internal_error("provider rejected tool result submission")
                completion = self._runs.complete_terminal(
                    CompleteTerminalCommand(
                        run_id=run_id,
                        expected_state=expected,
                        terminal_result=TerminalResult(
                            outcome=TerminalOutcome.INTERRUPTED,
                            error=error,
                        ),
                        final_event_kind="run.interrupted",
                        final_event_payload={
                            "terminal_result": {
                                "outcome": "interrupted",
                                "error": error,
                            }
                        },
                    )
                )
                self._coordinator._publish(completion.event)
                self._coordinator.pop_handle(run_id)
                raise RunStateConflictError(
                    "provider rejected tool result submission"
                )
        return {"accepted": True}
