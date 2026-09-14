"""Responses stream envelopes to provider-neutral run events."""

from __future__ import annotations

import copy
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from model_deck.engine.runs.input_codec import (
    normalized_messages_to_wire,
    parse_normalized_messages,
)
from model_deck.engine.runs.ports import ProviderRunEvent

__all__ = [
    "OpenAICompatibleEventError",
    "ResponsesEventTranslator",
    "RunIdentity",
]


_ERROR_MESSAGE = "openai-compatible event translation failed"


def _strip_for_raw(item: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-copy a wire item for the raw-provider accumulator.

    We keep the provider-private fields the canonical view strips so that
    a later same-scope continuation record can replay them back to the
    provider. ``_id`` and other host-tagged identifiers are preserved as-is.
    """
    if not isinstance(item, Mapping):
        _reject()
    return copy.deepcopy(dict(item))


_IGNORED_EVENT_TYPES = frozenset(
    {
        "response.in_progress",
        "response.queued",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_item.added",
        "response.output_text.done",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.done",
        "response.reasoning_text.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
    }
)


class OpenAICompatibleEventError(ValueError):
    """A provider envelope cannot be normalized without loss."""


def _reject() -> None:
    raise OpenAICompatibleEventError(_ERROR_MESSAGE)


def _decode_arguments(value: Any) -> dict[str, Any]:
    if type(value) is not str:
        _reject()
    decoded: Any = None
    failed = False
    try:
        decoded = json.loads(value, parse_constant=lambda _value: _reject())
    except (TypeError, ValueError, RecursionError, UnicodeError):
        failed = True
    if failed or type(decoded) is not dict:
        _reject()
    return decoded


def _validate_history_item(value: dict[str, Any]) -> dict[str, Any]:
    wire: list[dict[str, Any]] | None = None
    failed = False
    try:
        normalized = parse_normalized_messages([value])
        wire = normalized_messages_to_wire(normalized)
    except Exception:
        failed = True
    if failed or wire is None:
        _reject()
    return wire[0]


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """Identity carried by a translator to populate usage event payloads."""

    session_id: str
    registration_id: str
    connection_id: str
    provider_model_id: str


def _validate_identity(value: Any) -> RunIdentity | None:
    if value is None:
        return None
    if not isinstance(value, RunIdentity):
        _reject()
    for field_name in (
        "session_id",
        "registration_id",
        "connection_id",
        "provider_model_id",
    ):
        field_value = getattr(value, field_name)
        if (
            type(field_value) is not str
            or not field_value
            or "\r" in field_value
            or "\n" in field_value
        ):
            _reject()
    return value


class ResponsesEventTranslator:
    """Normalize Responses-style stream envelopes for one provider run.

    One instance spans every HTTP segment in a tool-using run. A later
    ``response.created`` begins the next segment without emitting a duplicate
    ``run.started`` event. Completed canonical output items are retained as
    detached history for the next request segment.
    """

    def __init__(
        self,
        run_id: str,
        clock: Callable[[], str],
        identity: RunIdentity | None = None,
    ) -> None:
        if type(run_id) is not str or not run_id or not callable(clock):
            _reject()
        self._run_id = run_id
        self._clock = clock
        self._identity = _validate_identity(identity)
        self._run_started = False
        self._segment_started = False
        self._segment_ended = False
        self._terminal = False
        self._outstanding_call_id: str | None = None
        self._completed_output_items: list[dict[str, Any]] = []
        self._raw_provider_items: list[dict[str, Any]] = []
        self._response_id: str | None = None

    @property
    def outstanding_call_id(self) -> str | None:
        return self._outstanding_call_id

    @property
    def run_started(self) -> bool:
        return self._run_started

    @property
    def segment_ended(self) -> bool:
        return self._segment_ended

    @property
    def completed_output_items(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(self._completed_output_items))

    @property
    def raw_provider_items(self) -> tuple[dict[str, Any], ...]:
        """Provider output items with provider-private fields preserved.

        The canonical ``completed_output_items`` view strips fields like
        ``encrypted_content``, ``reasoning_details`` and
        ``encrypted_function_args`` so engine events never see opaque provider
        state. The raw view keeps those fields so a later same-scope
        continuation record can round-trip them back to the provider. Snapshots
        are detached; mutating one does not affect later reads.
        """
        return tuple(copy.deepcopy(self._raw_provider_items))

    @property
    def response_id(self) -> str | None:
        """The provider response id for the currently active segment."""
        return self._response_id

    def mark_tool_result(self, call_id: str) -> None:
        if (
            type(call_id) is not str
            or call_id != self._outstanding_call_id
            or not self._segment_ended
            or self._terminal
        ):
            _reject()
        self._outstanding_call_id = None

    def translate(self, envelope: Mapping[str, Any]) -> tuple[ProviderRunEvent, ...]:
        if not isinstance(envelope, Mapping) or self._terminal:
            _reject()
        event_type = envelope.get("type")
        if type(event_type) is not str:
            _reject()

        if event_type == "response.created":
            if self._segment_started and not self._segment_ended:
                _reject()
            if self._outstanding_call_id is not None:
                _reject()
            self._segment_started = True
            self._segment_ended = False
            response = envelope.get("response")
            if isinstance(response, Mapping) and isinstance(response.get("id"), str) and response["id"]:
                self._response_id = response["id"]
            if self._run_started:
                return ()
            self._run_started = True
            return (self._event("run.started"),)

        if event_type in {"response.failed", "response.incomplete", "error"}:
            return self._failure_events(event_type, envelope)

        if not self._segment_started or self._segment_ended:
            _reject()

        if event_type == "response.output_text.delta":
            return (self._content_delta(envelope, "text"),)
        if event_type in {
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        }:
            return (self._content_delta(envelope, "reasoning"),)
        if event_type == "response.output_item.done":
            return self._output_item_done(envelope)
        if event_type == "response.completed":
            return self._completed(envelope)
        if event_type in _IGNORED_EVENT_TYPES:
            return ()
        _reject()

    def _now(self) -> str:
        observed_at: Any = None
        failed = False
        try:
            observed_at = self._clock()
        except Exception:
            failed = True
        if failed or type(observed_at) is not str or not observed_at:
            _reject()
        return observed_at

    def _event(self, kind: str, payload: Any = None) -> ProviderRunEvent:
        return ProviderRunEvent(
            kind=kind,
            run_id=self._run_id,
            observed_at=self._now(),
            payload=copy.deepcopy(payload),
        )

    def _content_delta(
        self,
        envelope: Mapping[str, Any],
        channel: str,
    ) -> ProviderRunEvent:
        delta = envelope.get("delta")
        if type(delta) is not str or not delta:
            _reject()
        return self._event(
            "content.delta",
            {"delta": delta, "channel": channel},
        )

    def _output_item_done(
        self,
        envelope: Mapping[str, Any],
    ) -> tuple[ProviderRunEvent, ...]:
        item = envelope.get("item")
        if not isinstance(item, Mapping):
            _reject()
        item_type = item.get("type")
        if item_type == "message":
            normalized = self._normalize_message(item)
            self._raw_provider_items.append(_strip_for_raw(item))
            if normalized is not None:
                self._completed_output_items.append(normalized)
            return ()
        if item_type == "reasoning":
            self._raw_provider_items.append(_strip_for_raw(item))
            return ()
        if item_type != "function_call" or self._outstanding_call_id is not None:
            print(
                f"OpenAI-compatible output item rejected: type={item_type!r}, outstanding={self._outstanding_call_id is not None}",
                file=sys.stderr,
                flush=True,
            )
            _reject()
        call_id = item.get("call_id")
        tool_name = item.get("name")
        arguments_text = item.get("arguments")
        if (
            type(call_id) is not str
            or not call_id
            or type(tool_name) is not str
            or not tool_name
        ):
            print(
                "OpenAI-compatible function call identity is invalid",
                file=sys.stderr,
                flush=True,
            )
            _reject()
        try:
            arguments = _decode_arguments(arguments_text)
        except OpenAICompatibleEventError:
            print(
                f"OpenAI-compatible function arguments type is {type(arguments_text).__name__}",
                file=sys.stderr,
                flush=True,
            )
            raise
        self._outstanding_call_id = call_id
        self._raw_provider_items.append(_strip_for_raw(item))
        self._completed_output_items.append(
            _validate_history_item(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": tool_name,
                    "arguments": arguments_text,
                }
            )
        )
        return (
            self._event(
                "tool.requested",
                {
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                },
            ),
        )

    @staticmethod
    def _normalize_message(item: Mapping[str, Any]) -> dict[str, Any] | None:
        content = item.get("content")
        if type(content) is not list:
            _reject()
        parts: list[dict[str, str]] = []
        for part in content:
            if not isinstance(part, Mapping) or part.get("type") != "output_text":
                _reject()
            text = part.get("text")
            if type(text) is not str:
                _reject()
            parts.append({"type": "output_text", "text": text})
        if not parts:
            return None
        return _validate_history_item(
            {"type": "message", "role": "assistant", "content": parts}
        )

    def _usage_events(
        self,
        envelope: Mapping[str, Any],
    ) -> tuple[ProviderRunEvent, ...]:
        if self._identity is None:
            _reject()
        response = envelope.get("response")
        if not isinstance(response, Mapping) or "usage" not in response:
            return ()
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            _reject()
        details: Any = usage.get("input_tokens_details")
        cached_tokens = None
        if isinstance(details, Mapping):
            cached_tokens = details.get("cached_tokens")
        observed_at = self._now()
        identity = self._identity
        events: list[ProviderRunEvent] = []
        for unit_kind, value_key in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cached_tokens", None),
        ):
            if unit_kind == "cached_tokens":
                value = cached_tokens
            else:
                value = usage.get(value_key)
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            if value < 0:
                _reject()
            payload_usage: dict[str, Any] = {
                "run_id": self._run_id,
                "session_id": identity.session_id,
                "registration_id": identity.registration_id,
                "connection_id": identity.connection_id,
                "provider_model_id": identity.provider_model_id,
                "observed_at": observed_at,
                "units": float(value),
                "unit_kind": unit_kind,
            }
            events.append(
                self._event("usage.observed", {"usage": payload_usage})
            )
        return tuple(events)

    def _completed(
        self,
        envelope: Mapping[str, Any],
    ) -> tuple[ProviderRunEvent, ...]:
        self._segment_ended = True
        events = list(self._usage_events(envelope))
        if self._outstanding_call_id is None:
            self._terminal = True
            events.append(
                self._event(
                    "run.completed",
                    {"terminal_result": {"outcome": "completed"}},
                )
            )
        return tuple(events)

    def _failure_events(
        self,
        event_type: str,
        envelope: Mapping[str, Any],
    ) -> tuple[ProviderRunEvent, ...]:
        if self._segment_ended:
            _reject()
        self._segment_ended = True
        self._terminal = True
        events = list(self._usage_events(envelope))
        if event_type == "response.incomplete":
            code = "provider_response_incomplete"
            message = "The provider returned an incomplete response."
        else:
            code = "provider_stream_failed"
            message = "The provider reported a failed response."
        events.append(
            self._event(
                "run.failed",
                {
                    "terminal_result": {
                        "outcome": "failed",
                        "error": {"code": code, "message": message},
                    }
                },
            )
        )
        return tuple(events)
