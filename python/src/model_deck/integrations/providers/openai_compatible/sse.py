"""Strict SSE decoder and provider-lifecycle validator for OpenAI-compatible streams.

This module is adapter-local: it imports only the B12 ``ProviderRunEvent`` port
and standard-library types. It performs no I/O, no HTTP, no credential lookup,
and no host conversion. Wire translation lives in the next slice.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from model_deck.engine.runs.ports import ProviderRunEvent


DEFAULT_MAX_EVENT_BYTES = 1_048_576


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class SseError(Exception):
    """Base error for the OpenAI-compatible SSE adapter."""


class SseProtocolError(SseError):
    """The peer violated the SSE protocol (malformed fields or bad state)."""


class SseByteLimitError(SseError):
    """Buffered bytes exceeded the per-event byte budget."""


class SseTruncatedError(SseError):
    """The stream ended with an unterminated event or invalid UTF-8."""


class SseJsonError(SseError):
    """The JSON envelope was rejected; messages never echo the payload."""


class ProviderValidatorError(Exception):
    """Base error for the provider segment-terminal validator."""


class ProviderValidatorProtocolError(ProviderValidatorError):
    """The provider emitted events out of order or with a bad payload."""


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SseMessage:
    """One dispatched SSE event.

    ``data`` is the concatenation of every ``data:`` field for the event,
    joined by a single LF. ``event`` and ``event_id`` are the last values seen
    for the matching fields within this event (or ``None``). ``done`` is
    ``True`` only for the OpenAI-style ``[DONE]`` sentinel event.
    """

    data: str
    event: str | None = None
    event_id: str | None = None
    done: bool = False


class SegmentTermination(str, Enum):
    """How a provider run segment finished."""

    TERMINAL = "terminal"
    TOOL_SUSPENDED = "tool_suspended"


# ---------------------------------------------------------------------------
# SSE decoder
# ---------------------------------------------------------------------------


# Line-break bytes used to delimit SSE field lines.
_LF = 0x0A
_CR = 0x0D
_BOM = b"\xef\xbb\xbf"


def _find_line_break(buffer: bytes, *, final: bool = False) -> tuple[int, int]:
    """Return ``(index, length)`` of the next line break, or ``(-1, 0)``."""

    for index, byte in enumerate(buffer):
        if byte == _LF:
            return index, 1
        if byte == _CR:
            if index + 1 == len(buffer) and not final:
                return -1, 0
            length = 2 if index + 1 < len(buffer) and buffer[index + 1] == _LF else 1
            return index, length
    return -1, 0


class SseDecoder:
    """Incremental SSE decoder for OpenAI-compatible streaming responses.

    The decoder accepts arbitrary byte chunks, tolerates LF, CRLF, and bare CR
    line endings, ignores comments, joins multi-line ``data`` fields with LF,
    and dispatches one ``SseMessage`` per blank line. The OpenAI-style
    ``[DONE]`` sentinel is exposed via ``SseMessage.done=True`` and any field
    that follows it is a protocol error.
    """

    def __init__(self, *, max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES) -> None:
        if not isinstance(max_event_bytes, int) or max_event_bytes <= 0:
            raise ValueError("max_event_bytes must be a positive int")
        self._max_event_bytes = max_event_bytes
        self._buffer = bytearray()
        self._finished = False
        self._last_event_id: str | None = None
        self._data_parts: list[str] = []
        self._event_type: str | None = None
        self._event_byte_count = 0
        self._done_seen = False
        self._bom_decided = False
        self._had_pending_event = False  # set when a partial event was buffered

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def last_event_id(self) -> str | None:
        return self._last_event_id

    def feed(self, chunk: bytes) -> tuple[SseMessage, ...]:
        if self._finished:
            raise SseProtocolError("feed called after finish()")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise SseProtocolError("feed requires a bytes-like object")
        if isinstance(chunk, (bytearray, memoryview)):
            chunk = bytes(chunk)

        self._buffer.extend(chunk)
        if not self._prepare_initial_bytes():
            return ()

        messages: list[SseMessage] = []
        while True:
            buffer_view = bytes(self._buffer)
            index, break_len = _find_line_break(buffer_view)
            if index < 0:
                if self._event_byte_count + len(self._buffer) > self._max_event_bytes:
                    raise SseByteLimitError(
                        "buffered bytes exceed max_event_bytes"
                    )
                break
            raw = buffer_view[:index]
            del self._buffer[: index + break_len]
            self._handle_line(raw, break_len, messages)
        return tuple(messages)

    def finish(self) -> tuple[SseMessage, ...]:
        """Close the decoder and return any final messages.

        Raises :class:`SseTruncatedError` if the stream ended with an
        unterminated non-comment line, an event with pending ``data``/``event``/
        ``id`` fields that never dispatched, or invalid UTF-8 in the residual
        buffer.
        """

        if self._finished:
            return ()

        messages: list[SseMessage] = []
        self._prepare_initial_bytes(final=True)
        while self._buffer:
            buffer_view = bytes(self._buffer)
            index, break_len = _find_line_break(buffer_view, final=True)
            if index < 0:
                break
            raw = buffer_view[:index]
            del self._buffer[: index + break_len]
            self._handle_line(raw, break_len, messages)

        if self._buffer:
            residual = bytes(self._buffer)
            self._buffer = bytearray()
            # Validate UTF-8 strictly before parsing the residual.
            try:
                residual.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SseTruncatedError(
                    "stream ended with invalid UTF-8"
                ) from exc
            if residual:
                self._handle_line(residual, 0, messages)

        if self._pending_event_fields():
            raise SseTruncatedError(
                "stream ended with unterminated event"
            )

        self._finished = True
        return tuple(messages)

    # -- internal helpers ------------------------------------------------

    def _prepare_initial_bytes(self, *, final: bool = False) -> bool:
        if self._bom_decided:
            return True
        prefix = bytes(self._buffer)
        if not prefix:
            return final
        if len(prefix) < len(_BOM) and _BOM.startswith(prefix):
            if not final:
                return False
        elif prefix.startswith(_BOM):
            del self._buffer[: len(_BOM)]
        self._bom_decided = True
        return True

    def _pending_event_fields(self) -> bool:
        return self._had_pending_event

    def _handle_line(
        self,
        raw: bytes,
        break_len: int,
        messages: list[SseMessage],
    ) -> None:
        # Track bytes consumed for this event (raw line + line break).
        line_bytes = len(raw) + break_len
        if self._event_byte_count + line_bytes > self._max_event_bytes:
            raise SseByteLimitError("event bytes exceed max_event_bytes")

        # Blank line: dispatch.
        if not raw:
            self._dispatch_event(messages)
            return

        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SseProtocolError("line is not valid UTF-8") from exc

        # Comment lines are ignored, even after [DONE].
        if raw[:1] == b":":
            self._event_byte_count += line_bytes
            return

        if self._done_seen:
            raise SseProtocolError("field received after [DONE] marker")
        if self._finished:
            raise SseProtocolError("line parsed after finish()")

        # Field parsing. ``field:value`` or ``field: value``.
        colon = raw.find(b":")
        if colon < 0:
            try:
                field_name = raw.decode("ascii")
            except UnicodeDecodeError as exc:
                raise SseProtocolError("field name is not ASCII") from exc
            value_bytes = b""
        else:
            try:
                field_name = raw[:colon].decode("ascii")
            except UnicodeDecodeError as exc:
                raise SseProtocolError("field name is not ASCII") from exc
            value_bytes = raw[colon + 1:]
            if value_bytes[:1] == b" ":
                value_bytes = value_bytes[1:]

        try:
            value = value_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SseProtocolError("field value is not valid UTF-8") from exc

        self._event_byte_count += line_bytes

        if field_name == "data":
            self._data_parts.append(value)
            self._had_pending_event = True
        elif field_name == "event":
            self._event_type = value
            self._had_pending_event = True
        elif field_name == "id":
            # WHATWG: an ``id`` field containing U+0000 is ignored.
            if "\x00" not in value:
                self._last_event_id = value
                self._had_pending_event = True
        # Unknown fields are intentionally ignored.

    def _dispatch_event(self, messages: list[SseMessage]) -> None:
        # Per the WHATWG spec, an event with no field lines is a no-op
        # dispatch (only the last event ID carries forward). Avoid emitting
        # noise messages, but still reset state.
        if not self._data_parts and self._event_type is None:
            self._reset_event_state()
            return
        data = "\n".join(self._data_parts) if self._data_parts else ""
        done = data == "[DONE]"
        messages.append(
            SseMessage(
                data=data,
                event=self._event_type,
                event_id=self._last_event_id,
                done=done,
            )
        )
        if done:
            self._done_seen = True
        self._reset_event_state()

    def _reset_event_state(self) -> None:
        self._data_parts = []
        self._event_type = None
        self._event_byte_count = 0
        self._had_pending_event = False


# ---------------------------------------------------------------------------
# JSON envelope helper
# ---------------------------------------------------------------------------


def _reject_constant(value: str) -> None:
    raise SseJsonError("non-standard JSON constant rejected")


def _reject_non_finite_numbers(value: Any) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SseJsonError("non-finite JSON number rejected")
        return
    if isinstance(value, list):
        for item in value:
            _reject_non_finite_numbers(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_finite_numbers(item)


def decode_json_object(message: SseMessage) -> dict[str, Any]:
    """Parse an SSE message's ``data`` as a JSON object envelope.

    Rejects done markers, malformed JSON, NaN/Infinity tokens, and any
    top-level value that is not a JSON object. Returns a detached shallow
    copy of the parsed object so callers cannot mutate the decoder's view
    of the message.
    """

    if not isinstance(message, SseMessage):
        raise SseJsonError("decode_json_object requires an SseMessage")
    if message.done:
        raise SseJsonError("cannot decode done marker as JSON")
    try:
        parsed = json.loads(message.data, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise SseJsonError("malformed JSON envelope") from exc
    if not isinstance(parsed, dict):
        raise SseJsonError("JSON envelope is not an object")
    _reject_non_finite_numbers(parsed)
    return parsed


# ---------------------------------------------------------------------------
# Provider lifecycle validator
# ---------------------------------------------------------------------------


_ALLOWED_KINDS: frozenset[str] = frozenset(
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

_NON_TERMINAL_KINDS: frozenset[str] = frozenset(
    {
        "run.started",
        "content.delta",
        "tool.requested",
        "usage.observed",
        "run.cancelling",
    }
)

_TERMINAL_KINDS: frozenset[str] = frozenset(
    {
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.interrupted",
    }
)


def _coerce_call_id(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        raise ProviderValidatorProtocolError(
            "tool.requested payload must be a mapping"
        )
    tool_call = payload.get("tool_call")
    if not isinstance(tool_call, Mapping):
        raise ProviderValidatorProtocolError(
            "tool.requested missing tool_call mapping"
        )
    call_id = tool_call.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise ProviderValidatorProtocolError(
            "tool.requested tool_call.call_id must be a non-empty string"
        )
    return call_id


class ProviderEventTerminalValidator:
    """Validate a single provider-run segment against the B12 lifecycle.

    The validator consumes existing ``ProviderRunEvent`` objects (the B12 port)
    and enforces the lifecycle invariants a future wire translation must
    preserve:

    - exactly one ``run.started`` before any output;
    - matching ``run_id`` on every event;
    - at most one terminal event;
    - no event after a terminal event;
    - one outstanding tool call at a time, cleared only by ``mark_tool_result``
      with the matching ``call_id``;
    - a completed terminal is rejected if a tool call is still outstanding.
    """

    def __init__(self, run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise ProviderValidatorError("run_id must be a non-empty string")
        self._run_id = run_id
        self._started = False
        self._terminal_kind: str | None = None
        self._outstanding_call_id: str | None = None

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def terminal_kind(self) -> str | None:
        return self._terminal_kind

    @property
    def outstanding_call_id(self) -> str | None:
        return self._outstanding_call_id

    def submit(self, event: ProviderRunEvent) -> None:
        if not isinstance(event, ProviderRunEvent):
            raise ProviderValidatorProtocolError(
                "submit requires a ProviderRunEvent"
            )
        if event.run_id != self._run_id:
            raise ProviderValidatorProtocolError(
                "event.run_id does not match validator run_id"
            )
        if event.kind not in _ALLOWED_KINDS:
            raise ProviderValidatorProtocolError(
                f"unknown provider event kind: {event.kind!r}"
            )
        if self._terminal_kind is not None:
            raise ProviderValidatorProtocolError(
                "event received after terminal"
            )

        if event.kind == "run.started":
            if self._started:
                raise ProviderValidatorProtocolError(
                    "duplicate run.started event"
                )
            self._started = True
            return

        if not self._started:
            raise ProviderValidatorProtocolError(
                "event received before run.started"
            )

        if event.kind == "tool.requested":
            call_id = _coerce_call_id(event.payload)
            if self._outstanding_call_id is not None:
                raise ProviderValidatorProtocolError(
                    "tool.requested while another tool call is outstanding"
                )
            self._outstanding_call_id = call_id
            return

        if event.kind in _TERMINAL_KINDS:
            if event.kind == "run.completed" and self._outstanding_call_id is not None:
                raise ProviderValidatorProtocolError(
                    "run.completed while a tool call is outstanding"
                )
            self._terminal_kind = event.kind
            return

        # content.delta / usage.observed / other allowed non-terminal kinds:
        # accepted once started, do not change lifecycle state.
        if event.kind not in _NON_TERMINAL_KINDS:
            raise ProviderValidatorProtocolError(
                f"unsupported provider event kind: {event.kind!r}"
            )

    def mark_tool_result(self, call_id: str) -> None:
        if not isinstance(call_id, str) or not call_id:
            raise ProviderValidatorError(
                "call_id must be a non-empty string"
            )
        if self._outstanding_call_id is None:
            raise ProviderValidatorProtocolError(
                "mark_tool_result called with no outstanding tool call"
            )
        if self._outstanding_call_id != call_id:
            raise ProviderValidatorProtocolError(
                "mark_tool_result call_id does not match outstanding tool call"
            )
        self._outstanding_call_id = None

    def finish_segment(self) -> SegmentTermination:
        """Return how the segment ended.

        - :class:`SegmentTermination.TERMINAL` if a terminal event was observed;
        - :class:`SegmentTermination.TOOL_SUSPENDED` if exactly one tool call is
          outstanding and the segment ended without a terminal event;
        - raises :class:`ProviderValidatorProtocolError` otherwise (no terminal
          and no outstanding tool call means the segment ended prematurely).
        """

        if self._terminal_kind is not None:
            return SegmentTermination.TERMINAL
        if self._outstanding_call_id is not None:
            return SegmentTermination.TOOL_SUSPENDED
        raise ProviderValidatorProtocolError(
            "segment ended without terminal or outstanding tool call"
        )
