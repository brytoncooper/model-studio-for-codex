"""Provider execution composition for OpenAI-compatible HTTP endpoints."""

from __future__ import annotations

import json
import sys
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol

from model_deck.engine.runs.input_codec import (
    normalized_messages_to_wire,
    parse_normalized_messages,
)
from model_deck.engine.runs.ports import (
    CancelProviderRunResult,
    ProviderCancelTerminationStatus,
    ProviderRunEvent,
    ProviderRunEventSink,
    RunRequest,
    SubmitToolResultProviderOutcome,
    SubmitToolResultProviderResult,
)

from model_deck.engine.routing.ports import ContinuationScope, ExecutionMode

from ..continuation import compaction as _compaction
from ..continuation.store import (
    ContinuationError,
    ContinuationRecord,
    ContinuationRouteScope,
)

from .chat_stream import ChatStreamTranslator
from .events import ResponsesEventTranslator, RunIdentity
from .http_transport import DEFAULT_TIMEOUT, post_stream as default_post_stream
from .request_mapping import (
    apply_continuation_items,
    build_chat_request,
    build_responses_request,
)
from .sse import (
    ProviderEventTerminalValidator,
    SegmentTermination,
    SseDecoder,
    decode_json_object,
)

__all__ = [
    "ContinuationStoreProtocol",
    "CredentialResolver",
    "EndpointResolver",
    "OpenAICompatibleEndpointConfig",
    "OpenAICompatibleExecutionError",
    "OpenAICompatibleExecutionPort",
    "OpenAICompatibleRunHandle",
    "RunIdentity",
    "WireMode",
]


class ContinuationStoreProtocol(Protocol):
    """Minimal surface this port uses from the durable continuation store.

    The full store API is owned by the sibling continuation package; this
       protocol only names the methods the port actually invokes so tests can
       supply a stub without re-implementing the schema. The companion
       helper ``_validate_continuation_scope`` enforces the trust contract on
    every load and save.
    """

    def save_response(
        self,
        scope: ContinuationRouteScope,
        response_id: str,
        items: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]],
    ) -> None: ...

    def load_all(self, scope: ContinuationRouteScope) -> list[ContinuationRecord]: ...

    def clear(self, scope: ContinuationRouteScope) -> None: ...

    def prepare_session_reset(
        self, session_id: str, continuation_handle: str
    ) -> str | None: ...

    def commit_session_reset(self, reset_token: str) -> None: ...

    def rollback_session_reset(self, reset_token: str) -> None: ...


def _scope_string(value: Any) -> str:
    if hasattr(value, "value") and isinstance(getattr(value, "value", None), str):
        return value.value
    if isinstance(value, str) and value:
        return value
    _reject()


_FAILURE_MESSAGE = "The provider execution failed."
_ERROR_MESSAGE = "openai-compatible execution failed"
_FALLBACK_STATUSES = frozenset({404, 405, 501})
_READ_BYTES = 64 * 1024


class WireMode(str, Enum):
    AUTO = "auto"
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"


@dataclass(frozen=True, slots=True)
class OpenAICompatibleEndpointConfig:
    """Resolved, non-secret endpoint coordinates for one connection revision."""

    host: str
    port: int
    secure: bool
    path_prefix: str = "/v1"
    wire_mode: WireMode = WireMode.AUTO
    vendor_id: str | None = None


class EndpointResolver(Protocol):
    def __call__(
        self,
        endpoint_config_ref: str,
        connection_revision: int,
    ) -> OpenAICompatibleEndpointConfig: ...


class CredentialResolver(Protocol):
    def __call__(self, credential_ref: str) -> str: ...


class OpenAICompatibleExecutionError(ValueError):
    """A run cannot be safely dispatched by this adapter."""


class _CancellationObserved(Exception):
    pass


def _reject(message: str = _ERROR_MESSAGE) -> None:
    raise OpenAICompatibleExecutionError(message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_endpoint(value: Any) -> OpenAICompatibleEndpointConfig:
    if (
        type(value) is not OpenAICompatibleEndpointConfig
        or type(value.host) is not str
        or not value.host
        or len(value.host) > 253
        or any(character in value.host for character in "/\\\r\n")
        or type(value.port) is not int
        or not 1 <= value.port <= 65535
        or type(value.secure) is not bool
        or type(value.path_prefix) is not str
        or not value.path_prefix.startswith("/")
        or len(value.path_prefix) > 2048
        or any(character in value.path_prefix for character in "\\\r\n?#")
        or type(value.wire_mode) is not WireMode
        or (
            value.vendor_id is not None
            and (type(value.vendor_id) is not str or not value.vendor_id)
        )
    ):
        _reject()
    return value


def _request_path(config: OpenAICompatibleEndpointConfig, wire: WireMode) -> str:
    suffix = "responses" if wire is WireMode.RESPONSES else "chat/completions"
    prefix = config.path_prefix.rstrip("/")
    return f"{prefix}/{suffix}" if prefix else f"/{suffix}"


def _encode_tool_output(result: Any) -> str:
    if type(result) is str:
        return result
    encoded: str | None = None
    failed = False
    try:
        encoded = json.dumps(
            result,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        failed = True
    if failed or encoded is None:
        _reject()
    return encoded


def _validate_continuation_scope(
    request: RunRequest,
) -> ContinuationRouteScope:
    """Build the trusted nine-field route scope from RunRequest identity.

    The scope is constructed exclusively from session_id (RunRequest) and
    captured RouteSnapshot fields (connection_id/revision, provider_id,
    provider_model_id, execution_mode, endpoint_config_ref, credential_ref).
    Continuation handle is the engine-issued identifier from
    ``request.continuation_scope.handle``. Worker-supplied or body-supplied
    fields are NEVER trusted to build the scope.
    """
    if type(request.session_id) is not str or not request.session_id:
        _reject()
    route = request.route_snapshot
    if (
        type(route.connection_id) is not str
        or not route.connection_id
        or type(route.connection_revision) is not int
        or isinstance(route.connection_revision, bool)
        or type(route.provider_model_id) is not str
        or not route.provider_model_id
        or type(route.provider_id) not in (str,)
        or not route.provider_id
        or type(route.execution_mode) is not ExecutionMode
    ):
        _reject()
    if not isinstance(route.endpoint_config_ref, str) or not route.endpoint_config_ref:
        _reject()
    if not isinstance(route.credential_ref, str) or not route.credential_ref:
        _reject()
    scope_obj = request.continuation_scope
    if scope_obj is None:
        _reject()
    if not isinstance(scope_obj, ContinuationScope):
        _reject()
    if (
        scope_obj.connection_id != route.connection_id
        or scope_obj.provider_model_id != route.provider_model_id
        or _scope_string(scope_obj.provider_id) != _scope_string(route.provider_id)
        or _scope_string(scope_obj.execution_mode) != _scope_string(route.execution_mode)
    ):
        _reject()
    handle = scope_obj.handle
    if not isinstance(handle, str) or not handle:
        _reject()
    return ContinuationRouteScope(
        session_id=request.session_id,
        connection_id=route.connection_id,
        connection_revision=route.connection_revision,
        provider_id=_scope_string(route.provider_id),
        provider_model_id=route.provider_model_id,
        execution_mode=_scope_string(route.execution_mode),
        endpoint_config_ref=route.endpoint_config_ref,
        credential_ref=route.credential_ref,
        continuation_handle=handle,
    )


def _input_contains_compaction_barrier(input_items: Any) -> bool:
    """True when this run carries a router-made compaction item.

    The summary-generation run may consume scoped provider state but must not
    install its generated summary as normal provider continuation. A later
    request containing a Model Deck decoded compaction checkpoint must not
    auto-inject pre-compaction opaque history; we clear/invalidate scoped
    provider records at that barrier before transport.
    """
    if not isinstance(input_items, list):
        return False
    for item in input_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        for part in item.get("content") or []:
            if (
                isinstance(part, dict)
                and isinstance(part.get("text"), str)
                and part["text"].startswith(_compaction.SUMMARY_PREFIX + "\n")
            ):
                return True
    return False


def _is_compaction_summary_request(input_items: Any) -> bool:
    if not isinstance(input_items, list) or not input_items:
        return False
    item = input_items[-1]
    if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "user":
        return False
    return any(
        isinstance(part, dict) and part.get("text") == _compaction.SUMMARIZATION_PROMPT
        for part in item.get("content") or []
    )


class OpenAICompatibleExecutionPort:
    """Execute typed runs through injected endpoint and credential resolvers."""

    def __init__(
        self,
        endpoint_resolver: EndpointResolver,
        credential_resolver: CredentialResolver,
        *,
        post_stream: Callable[..., Any] = default_post_stream,
        clock: Callable[[], str] = _utc_now,
        request_timeout: float = DEFAULT_TIMEOUT,
        continuation_store: ContinuationStoreProtocol | None = None,
    ) -> None:
        if (
            not callable(endpoint_resolver)
            or not callable(credential_resolver)
            or not callable(post_stream)
            or not callable(clock)
            or not isinstance(request_timeout, (int, float))
            or isinstance(request_timeout, bool)
            or request_timeout <= 0
            or request_timeout > 3600
            or not math.isfinite(float(request_timeout))
        ):
            _reject()
        if continuation_store is not None and any(
            not callable(getattr(continuation_store, name, None))
            for name in (
                "save_response",
                "load_all",
                "clear",
                "prepare_session_reset",
                "commit_session_reset",
                "rollback_session_reset",
            )
        ):
            _reject()
        self._endpoint_resolver = endpoint_resolver
        self._credential_resolver = credential_resolver
        self._post_stream = post_stream
        self._clock = clock
        self._request_timeout = float(request_timeout)
        self._continuation_store = continuation_store
        self._lock = threading.Lock()
        self._endpoint_cache: dict[
            tuple[str, int], OpenAICompatibleEndpointConfig
        ] = {}
        self._wire_cache: dict[tuple[str, int], WireMode] = {}
        self._handles: set[OpenAICompatibleRunHandle] = set()

    def prepare_session_continuation_reset(
        self, session_id: str, continuation_handle: str
    ) -> str | None:
        store = self._continuation_store
        if store is None:
            return None
        try:
            return store.prepare_session_reset(session_id, continuation_handle)
        except ContinuationError:
            raise OpenAICompatibleExecutionError(
                "The local continuation store could not be reset."
            ) from None

    def commit_session_continuation_reset(self, reset_token: str) -> None:
        store = self._continuation_store
        if store is None:
            return
        try:
            store.commit_session_reset(reset_token)
        except ContinuationError:
            raise OpenAICompatibleExecutionError(
                "The local continuation store could not be reset."
            ) from None

    def rollback_session_continuation_reset(self, reset_token: str) -> None:
        store = self._continuation_store
        if store is None:
            return
        try:
            store.rollback_session_reset(reset_token)
        except ContinuationError:
            raise OpenAICompatibleExecutionError(
                "The local continuation store could not be reset."
            ) from None

    def start(
        self,
        request: RunRequest,
        sink: ProviderRunEventSink,
    ) -> OpenAICompatibleRunHandle:
        if (
            type(request) is not RunRequest
            or not callable(getattr(sink, "publish_provider_event", None))
            or type(request.route_snapshot.endpoint_config_ref) is not str
            or not request.route_snapshot.endpoint_config_ref
            or type(request.route_snapshot.credential_ref) is not str
            or not request.route_snapshot.credential_ref
        ):
            _reject()
        if request.options.parallel_tool_calls is True:
            _reject(
                "openai-compatible parallel tool calls are not supported"
            )
        handle = OpenAICompatibleRunHandle(self, request, sink)
        with self._lock:
            self._handles.add(handle)
        handle._start()
        return handle

    def close(self) -> None:
        with self._lock:
            handles = tuple(self._handles)
        for handle in handles:
            handle.request_cancel(deadline=self._safe_now())

    def _safe_now(self) -> str:
        try:
            value = self._clock()
        except Exception:
            return "1970-01-01T00:00:00Z"
        return value if type(value) is str and value else "1970-01-01T00:00:00Z"

    def _safe_log(self, message: str) -> None:
        """Log a diagnostic without leaking any user/model content.

        The message must not include prompt or response payloads, provider
        secrets, or any field carried inside the continuation records. The
        logger is the standard library; callers do not see stack traces
        here (the originating error was swallowed by design).
        """
        import logging

        logging.getLogger(__name__).warning(message)

    def _release(self, handle: OpenAICompatibleRunHandle) -> None:
        with self._lock:
            self._handles.discard(handle)

    def _endpoint(
        self,
        endpoint_config_ref: str,
        connection_revision: int,
    ) -> tuple[tuple[str, int], OpenAICompatibleEndpointConfig]:
        key = (endpoint_config_ref, connection_revision)
        with self._lock:
            cached = self._endpoint_cache.get(key)
        if cached is not None:
            return key, cached
        resolved = _validate_endpoint(
            self._endpoint_resolver(endpoint_config_ref, connection_revision)
        )
        with self._lock:
            existing = self._endpoint_cache.setdefault(key, resolved)
        return key, existing

    def _initial_wire(
        self,
        key: tuple[str, int],
        config: OpenAICompatibleEndpointConfig,
    ) -> WireMode:
        if config.wire_mode is not WireMode.AUTO:
            return config.wire_mode
        with self._lock:
            return self._wire_cache.get(key, WireMode.RESPONSES)

    def _remember_wire(self, key: tuple[str, int], wire: WireMode) -> None:
        with self._lock:
            self._wire_cache[key] = wire


@dataclass(frozen=True, slots=True)
class _PendingToolResult:
    call_id: str
    output: str
    input_value: Any
    completed_item_count: int


class OpenAICompatibleRunHandle:
    """Asynchronous provider-run handle returned before HTTP stream reading."""

    def __init__(
        self,
        owner: OpenAICompatibleExecutionPort,
        request: RunRequest,
        sink: ProviderRunEventSink,
    ) -> None:
        self._owner = owner
        self._request = request
        self._sink = sink
        self._events = ResponsesEventTranslator(
            request.run_id,
            owner._clock,
            identity=RunIdentity(
                session_id=request.session_id,
                registration_id=request.route_snapshot.registration_id,
                connection_id=request.route_snapshot.connection_id,
                provider_model_id=request.route_snapshot.provider_model_id,
            ),
        )
        self._validator = ProviderEventTerminalValidator(request.run_id)
        self._event_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._history = normalized_messages_to_wire(request.input)
        self._completed_item_count = 0
        self._endpoint_key: tuple[str, int] | None = None
        self._config: OpenAICompatibleEndpointConfig | None = None
        self._credential: str | None = None
        self._response: Any = None
        self._segment_active = False
        self._suspended = False
        self._cancel_requested = False
        self._terminal_kind: str | None = None
        self._pending: _PendingToolResult | None = None
        self._submitted: dict[str, str] = {}
        self._threads: list[threading.Thread] = []
        # Continuation state (only used when owner._continuation_store is set).
        # _scope is the trusted nine-field route scope bound on the first
        # segment; _response_id is captured from response.created so save()
        # has a stable response identifier; _continuation_cleared is the
        # per-segment compaction barrier flag — once a router-made
        # compaction item appears in the input we do not auto-inject
        # pre-compaction opaque history and we have already cleared the
        # scoped records via store.clear() before transport.
        self._scope: ContinuationRouteScope | None = None
        self._response_id: str | None = None
        self._continuation_cleared: bool = False
        self._raw_provider_start = 0
        self._continuation_suppressed = False
        self._segment_continuation_records: list[
            tuple[Mapping[str, Any], Mapping[str, Any], str | None]
        ] = []

    def __repr__(self) -> str:
        with self._state_lock:
            terminal = self._terminal_kind is not None
            suspended = self._suspended
        return (
            f"{type(self).__name__}(run_id={self._request.run_id!r}, "
            f"terminal={terminal}, suspended={suspended})"
        )

    def _start(self) -> None:
        with self._state_lock:
            self._segment_active = True
        self._spawn_reader()

    def _spawn_reader(self) -> None:
        thread = threading.Thread(
            target=self._read_segment,
            name=f"openai-compatible-{self._request.run_id}",
            daemon=True,
        )
        with self._state_lock:
            self._threads.append(thread)
        thread.start()

    def submit_tool_result(
        self,
        call_id: str,
        result: Any,
    ) -> SubmitToolResultProviderResult:
        if type(call_id) is not str or not call_id:
            return self._submission(SubmitToolResultProviderOutcome.REJECTED)
        try:
            output = _encode_tool_output(result)
        except OpenAICompatibleExecutionError:
            return self._submission(SubmitToolResultProviderOutcome.REJECTED)

        with self._event_lock:
            outstanding = self._events.outstanding_call_id
            completed_items = self._events.completed_output_items
        with self._state_lock:
            previous = self._submitted.get(call_id)
            if previous is not None:
                outcome = (
                    SubmitToolResultProviderOutcome.ACCEPTED
                    if previous == output
                    else SubmitToolResultProviderOutcome.REJECTED
                )
                return self._submission(outcome)
            if (
                self._terminal_kind is not None
                or self._cancel_requested
                or outstanding != call_id
                or self._pending is not None
            ):
                return self._submission(SubmitToolResultProviderOutcome.REJECTED)
            new_items = completed_items[self._completed_item_count :]
            candidate_history = [*self._history, *new_items]
            candidate_history.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                }
            )
            try:
                input_value = parse_normalized_messages(candidate_history)
            except Exception:
                return self._submission(SubmitToolResultProviderOutcome.REJECTED)
            pending = _PendingToolResult(
                call_id=call_id,
                output=output,
                input_value=input_value,
                completed_item_count=len(completed_items),
            )
            self._pending = pending
            self._submitted[call_id] = output
            resume_now = self._suspended and not self._segment_active

        if resume_now:
            self._resume_pending()
        return self._submission(SubmitToolResultProviderOutcome.ACCEPTED)

    @staticmethod
    def _submission(
        outcome: SubmitToolResultProviderOutcome,
    ) -> SubmitToolResultProviderResult:
        return SubmitToolResultProviderResult(outcome=outcome)

    def request_cancel(self, *, deadline: str) -> CancelProviderRunResult:
        if type(deadline) is not str or not deadline:
            _reject()
        with self._event_lock:
            with self._state_lock:
                if self._terminal_kind is not None:
                    status = (
                        ProviderCancelTerminationStatus.CONFIRMED
                        if self._terminal_kind == "run.cancelled"
                        else ProviderCancelTerminationStatus.UNKNOWN
                    )
                    return CancelProviderRunResult(
                        request_accepted=False,
                        termination_status=status,
                    )
                self._cancel_requested = True
                response = self._response
                segment_active = self._segment_active
        if response is not None:
            self._close_response(response)
        if not segment_active:
            self._emit_cancel_terminal()
        return CancelProviderRunResult(
            request_accepted=True,
            termination_status=ProviderCancelTerminationStatus.UNCONFIRMED,
        )

    def _read_segment(self) -> None:
        response: Any = None
        stage = "runtime-resolution"
        try:
            key, config, credential = self._runtime_values()
            stage = "request-open"
            if self._is_cancel_requested():
                self._emit_cancel_terminal()
                return
            wire = self._owner._initial_wire(key, config)
            response = self._open_response(config, credential, wire)
            self._set_response(response)
            if self._is_cancel_requested():
                self._close_response(response)
                self._emit_cancel_terminal()
                return

            if (
                config.wire_mode is WireMode.AUTO
                and wire is WireMode.RESPONSES
                and response.status in _FALLBACK_STATUSES
            ):
                self._close_response(response)
                self._clear_response(response)
                if self._is_cancel_requested():
                    self._emit_cancel_terminal()
                    return
                wire = WireMode.CHAT_COMPLETIONS
                response = self._open_response(config, credential, wire)
                self._set_response(response)
                if self._is_cancel_requested():
                    self._close_response(response)
                    self._emit_cancel_terminal()
                    return
                self._owner._remember_wire(key, WireMode.CHAT_COMPLETIONS)
            elif config.wire_mode is WireMode.AUTO and response.status == 200:
                self._owner._remember_wire(key, wire)

            if response.status != 200:
                self._emit_failure("provider_http_error")
                return
            stage = "response-stream"
            self._consume_stream(response, wire)
        except Exception as error:
            print(
                f"OpenAI-compatible execution failed at {stage}: {type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            if self._is_cancel_requested():
                self._emit_cancel_terminal()
            else:
                self._emit_failure("provider_execution_failed")
        finally:
            if response is not None:
                self._close_response(response)
                self._clear_response(response)

    def _runtime_values(
        self,
    ) -> tuple[tuple[str, int], OpenAICompatibleEndpointConfig, str]:
        # Validate and load local continuation before endpoint configuration or
        # credentials are resolved. Missing, corrupt, or incompatible state is
        # a local refusal and must not engage secret-bearing collaborators.
        self._prepare_continuation_for_segment()
        continuation_records = self._load_continuation_records()
        self._validate_continuation_history(continuation_records)
        with self._state_lock:
            self._segment_continuation_records = continuation_records
            if (
                self._endpoint_key is not None
                and self._config is not None
                and self._credential is not None
            ):
                return self._endpoint_key, self._config, self._credential
        route = self._request.route_snapshot
        key, config = self._owner._endpoint(
            route.endpoint_config_ref,
            route.connection_revision,
        )
        credential = self._owner._credential_resolver(route.credential_ref)
        if (
            type(credential) is not str
            or not credential
            or "\r" in credential
            or "\n" in credential
        ):
            _reject()
        with self._state_lock:
            self._endpoint_key = key
            self._config = config
            self._credential = credential
        return key, config, credential

    def _validate_continuation_history(
        self,
        records: list[tuple[Mapping[str, Any], Mapping[str, Any], str | None]],
    ) -> None:
        if not records:
            return
        segment_request = replace(
            self._request,
            input=parse_normalized_messages(list(self._history)),
        )
        body = build_responses_request(segment_request)
        result = apply_continuation_items(body, records, wire="responses")
        if result.unmatched:
            _reject("Required continuation is missing or does not match this history.")

    def _prepare_continuation_for_segment(self) -> None:
        """Bind the trusted continuation scope and apply the compaction barrier.

        The scope is constructed exclusively from ``request.session_id`` and
        the captured ``RouteSnapshot`` plus the engine-issued continuation
        handle. Worker-supplied or body-supplied fields are NEVER trusted to
        build the scope. When the request input carries a router-made
        compaction item (the user's decoded summary checkpoint), the
        scoped records are cleared via ``store.clear(scope)`` before
        transport so pre-compaction opaque history cannot be auto-injected.
        A later same-scope run may install fresh records; this run's
        generated summary is NEVER installed as provider continuation.
        """
        store = self._owner._continuation_store
        if store is None:
            return
        with self._state_lock:
            already_bound = self._scope is not None
            already_cleared = self._continuation_cleared
        if not already_bound:
            try:
                scope = _validate_continuation_scope(self._request)
            except OpenAICompatibleExecutionError:
                raise
            except ContinuationError:
                raise OpenAICompatibleExecutionError(
                    "The local continuation store could not be read."
                ) from None
            with self._state_lock:
                self._scope = scope
        if already_cleared:
            return
        with self._state_lock:
            scope = self._scope
            input_items = self._history
            self._continuation_suppressed = _is_compaction_summary_request(input_items)
        if scope is None:
            return
        if _input_contains_compaction_barrier(input_items):
            try:
                store.clear(scope)
            except ContinuationError:
                raise OpenAICompatibleExecutionError(
                    "The local continuation store could not be read."
                ) from None
            with self._state_lock:
                self._continuation_cleared = True

    def _load_continuation_records(self) -> list[tuple[Mapping[str, Any], Mapping[str, Any], str | None]]:
        store = self._owner._continuation_store
        with self._state_lock:
            scope = self._scope
            history = list(self._history)
        if store is None or scope is None or self._continuation_cleared:
            return []
        try:
            stored = store.load_all(scope)
        except ContinuationError:
            raise OpenAICompatibleExecutionError(
                "The local continuation store could not be read."
            ) from None
        if not stored:
            requires_continuation = False
            for item in history:
                if not isinstance(item, dict):
                    continue
                if item.get("type") in {"function_call", "reasoning"}:
                    requires_continuation = True
                elif item.get("type") == "message" and item.get("role") == "assistant":
                    requires_continuation = True
            if requires_continuation:
                raise OpenAICompatibleExecutionError(
                    "Required continuation is missing or does not match this history."
                )
            return []
        records: list[tuple[Mapping[str, Any], Mapping[str, Any], str | None]] = []
        for record in stored:
            raw_item = record.metadata.get("raw_item")
            if not isinstance(raw_item, Mapping):
                raise OpenAICompatibleExecutionError(
                    "The local continuation store could not be read."
                )
            records.append((self._visible_item(raw_item), record.metadata, record.response_id))
        return records

    @staticmethod
    def _visible_item(raw_item: Mapping[str, Any]) -> dict[str, Any]:
        kind = raw_item.get("type")
        if kind == "message":
            return {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": part.get("text", "")}
                    for part in raw_item.get("content") or []
                    if isinstance(part, Mapping) and part.get("type") == "output_text"
                ],
            }
        if kind == "function_call":
            return {
                "type": "function_call",
                "call_id": raw_item.get("call_id"),
                "name": raw_item.get("name"),
                "arguments": raw_item.get("arguments"),
            }
        if kind == "reasoning":
            return {
                "type": "reasoning",
                "summary": list(raw_item.get("summary") or []),
            }
        raise OpenAICompatibleExecutionError(_ERROR_MESSAGE)

    def _save_responses_continuation(self) -> None:
        store = self._owner._continuation_store
        if store is None or self._continuation_suppressed:
            return
        with self._state_lock:
            scope = self._scope
            start = self._raw_provider_start
        response_id = self._events.response_id
        if scope is None or not response_id:
            return
        raw_items = self._events.raw_provider_items[start:]
        saved: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
        for index, raw_item in enumerate(raw_items):
            if raw_item.get("type") not in {"message", "function_call", "reasoning"}:
                continue
            visible = self._visible_item(raw_item)
            item_ref = f"{response_id}:{start + index}"
            saved.append((item_ref, visible, {"raw_item": raw_item}))
        if not saved:
            return
        try:
            store.save_response(scope, response_id, saved)
        except ContinuationError:
            # A local continuation-store write failure must not corrupt
            # run.completed. Log a safe diagnostic and continue so the engine
            # can advance; the in-flight run still terminates cleanly.
            self._owner._safe_log("continuation save failed (responses wire)")

    def _save_chat_continuation(self, chat: ChatStreamTranslator) -> None:
        store = self._owner._continuation_store
        if store is None or self._continuation_suppressed:
            return
        with self._state_lock:
            scope = self._scope
        if scope is None:
            return
        items = []
        for index, (visible, metadata) in enumerate(chat.continuation_records):
            item_ref = f"{chat.response_id}:{index}"
            raw_item = dict(visible)
            if metadata.get("assistant_fields"):
                raw_item.update(metadata["assistant_fields"])
            if metadata.get("call_fields"):
                raw_item.update(metadata["call_fields"])
            items.append((
                item_ref,
                visible,
                {
                    "raw_item": raw_item,
                    "assistant_fields": metadata.get("assistant_fields", {}),
                    "call_fields": metadata.get("call_fields", {}),
                },
            ))
        if not items:
            return
        try:
            store.save_response(scope, chat.response_id, items)
        except ContinuationError:
            # A local continuation-store write failure must not corrupt
            # run.completed; see _save_responses_continuation for rationale.
            self._owner._safe_log("continuation save failed (chat wire)")

    def _open_response(
        self,
        config: OpenAICompatibleEndpointConfig,
        credential: str,
        wire: WireMode,
    ) -> Any:
        with self._state_lock:
            records = list(self._segment_continuation_records)
        segment_request = replace(
            self._request,
            input=parse_normalized_messages(list(self._history)),
        )
        if wire is WireMode.RESPONSES:
            body = build_responses_request(segment_request)
            if records:
                result = apply_continuation_items(body, records, wire="responses")
                if result.unmatched:
                    _reject("Required continuation is missing or does not match this history.")
        else:
            responses_body = build_responses_request(segment_request)
            if records:
                result = apply_continuation_items(responses_body, records, wire="responses")
                if result.unmatched:
                    _reject("Required continuation is missing or does not match this history.")
            body = build_chat_request(
                segment_request,
                provider_id=config.vendor_id,
                continuation_records=records,
            )
        with self._event_lock:
            self._raw_provider_start = len(self._events.raw_provider_items)
        payload = json.dumps(
            body,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return self._owner._post_stream(
            host=config.host,
            port=config.port,
            secure=config.secure,
            path=_request_path(config, wire),
            payload=payload,
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            timeout=self._owner._request_timeout,
        )

    def _consume_stream(self, response: Any, wire: WireMode) -> None:
        decoder = SseDecoder()
        chat = ChatStreamTranslator() if wire is WireMode.CHAT_COMPLETIONS else None
        saw_wire_terminal = False
        while True:
            if self._is_cancel_requested():
                raise _CancellationObserved
            chunk = response.read1(_READ_BYTES)
            if self._is_cancel_requested():
                raise _CancellationObserved
            if not chunk:
                break
            for message in decoder.feed(chunk):
                if message.done:
                    continue
                envelope = decode_json_object(message)
                saw_wire_terminal = (
                    self._consume_envelope(envelope, chat)
                    or saw_wire_terminal
                )
        for message in decoder.finish():
            if message.done:
                continue
            envelope = decode_json_object(message)
            saw_wire_terminal = (
                self._consume_envelope(envelope, chat)
                or saw_wire_terminal
            )
        if chat is not None:
            for envelope in chat.finish():
                if envelope.get("type") == "response.completed":
                    self._save_chat_continuation(chat)
                saw_wire_terminal = (
                    self._translate_envelope(envelope) or saw_wire_terminal
                )
        if not saw_wire_terminal or not self._events.segment_ended:
            _reject()

        with self._event_lock:
            termination = self._validator.finish_segment()
        if termination is SegmentTermination.TERMINAL:
            with self._state_lock:
                self._segment_active = False
            return
        with self._state_lock:
            self._segment_active = False
            self._suspended = True
            resume = self._pending is not None and not self._cancel_requested
            cancel = self._cancel_requested
        if cancel:
            self._emit_cancel_terminal()
        elif resume:
            self._resume_pending()

    def _consume_envelope(
        self,
        envelope: dict[str, Any],
        chat: ChatStreamTranslator | None,
    ) -> bool:
        if chat is None:
            return self._translate_envelope(envelope)
        terminal = False
        for translated in chat.feed(envelope):
            terminal = self._translate_envelope(translated) or terminal
        return terminal

    def _translate_envelope(self, envelope: dict[str, Any]) -> bool:
        event_type = envelope.get("type")
        with self._event_lock:
            with self._state_lock:
                if self._cancel_requested:
                    raise _CancellationObserved
            try:
                translated = self._events.translate(envelope)
            except Exception as error:
                print(
                    f"OpenAI-compatible event rejected {event_type!r}: {type(error).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                raise
            if event_type == "response.completed":
                self._save_responses_continuation()
            for event in translated:
                self._publish_locked(event)
        return event_type in {
            "response.completed",
            "response.failed",
            "response.incomplete",
            "error",
        }

    def _publish_locked(self, event: ProviderRunEvent) -> None:
        with self._state_lock:
            if self._terminal_kind is not None:
                return
        self._validator.submit(event)
        if event.kind in {
            "run.completed",
            "run.failed",
            "run.cancelled",
            "run.interrupted",
        }:
            with self._state_lock:
                if self._terminal_kind is not None:
                    return
                self._terminal_kind = event.kind
                self._segment_active = False
                self._credential = None
        terminal = event.kind in {
            "run.completed",
            "run.failed",
            "run.cancelled",
            "run.interrupted",
        }
        try:
            self._sink.publish_provider_event(event)
        finally:
            if terminal:
                self._owner._release(self)

    def _emit_failure(self, code: str) -> None:
        event = ProviderRunEvent(
            kind="run.failed",
            run_id=self._request.run_id,
            observed_at=self._owner._safe_now(),
            payload={
                "terminal_result": {
                    "outcome": "failed",
                    "error": {"code": code, "message": _FAILURE_MESSAGE},
                }
            },
        )
        with self._event_lock:
            try:
                self._publish_locked(event)
            except Exception:
                return

    def _emit_cancel_terminal(self) -> None:
        with self._event_lock:
            kind = "run.cancelled" if self._events.run_started else "run.interrupted"
            event = ProviderRunEvent(
                kind=kind,
                run_id=self._request.run_id,
                observed_at=self._owner._safe_now(),
                payload={"terminal_result": {"outcome": kind.removeprefix("run.")}},
            )
            try:
                self._publish_locked(event)
            except Exception:
                return

    def _resume_pending(self) -> None:
        with self._event_lock:
            with self._state_lock:
                pending = self._pending
                if (
                    pending is None
                    or not self._suspended
                    or self._segment_active
                    or self._cancel_requested
                    or self._terminal_kind is not None
                ):
                    return
            self._events.mark_tool_result(pending.call_id)
            self._validator.mark_tool_result(pending.call_id)
            with self._state_lock:
                self._history = normalized_messages_to_wire(pending.input_value)
                self._completed_item_count = pending.completed_item_count
                self._pending = None
                self._suspended = False
                self._segment_active = True
        self._spawn_reader()

    def _set_response(self, response: Any) -> None:
        with self._state_lock:
            self._response = response

    def _clear_response(self, response: Any) -> None:
        with self._state_lock:
            if self._response is response:
                self._response = None

    @staticmethod
    def _close_response(response: Any) -> None:
        try:
            response.close()
        except Exception:
            pass

    def _is_cancel_requested(self) -> bool:
        with self._state_lock:
            return self._cancel_requested
