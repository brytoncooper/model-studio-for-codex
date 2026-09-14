"""Pure typed-run mapping to OpenAI Responses and chat request bodies."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence, Tuple

from model_deck.engine.routing.ports import RouteSnapshot
from model_deck.engine.runs.input_codec import normalized_messages_to_wire
from model_deck.engine.runs.options import run_options_to_wire
from model_deck.engine.runs.ports import RunRequest
from model_deck.engine.runs.tool_definitions import (
    parse_tool_definitions,
    tool_definitions_to_wire,
)

from .translation import ContinuationError, chat_request_from_responses
from ..continuation.translate import item_identity as _sibling_item_identity

__all__ = [
    "ApplyResult",
    "OpenAICompatibleRequestMappingError",
    "apply_continuation_items",
    "build_chat_request",
    "build_responses_request",
]


_ERROR_MESSAGE = "openai-compatible request mapping failed"


class OpenAICompatibleRequestMappingError(ValueError):
    """A typed request cannot be represented without loss before transport."""


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Outcome of one ``apply_continuation_items`` call.

    ``matched`` counts wire items whose visible identity matched a record and
    received its metadata. ``unmatched`` counts records that did not match
    any wire item and were therefore not installed — never prepended, never
    silently dropped from the matched set.
    """

    matched: int
    unmatched: int


WireKind = Literal["responses", "chat_completions"]


def _reject() -> None:
    raise OpenAICompatibleRequestMappingError(_ERROR_MESSAGE)


def _validate_records(
    records: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> None:
    if not isinstance(records, (list, tuple)):
        _reject()
    for entry in records:
        if not isinstance(entry, (list, tuple)) or len(entry) not in (2, 3):
            _reject()
        visible_item, metadata = entry[:2]
        if not isinstance(visible_item, Mapping):
            _reject()
        if not isinstance(metadata, Mapping):
            _reject()


def _kind_for_wire(wire: WireKind) -> str:
    if not isinstance(wire, str):
        _reject()
    if wire == "responses":
        return "responses"
    if wire == "chat_completions":
        return "chat_completions"
    _reject()


def _metadata_fields(metadata: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """Return provider fields saved by older and current continuation writers."""
    raw_item = metadata.get("raw_item")
    if raw_item is not None:
        if not isinstance(raw_item, Mapping) or raw_item.get("type") != kind:
            _reject()
        visible_keys = {
            "type", "id", "role", "content", "call_id", "name", "arguments",
        }
        return {
            key: value for key, value in raw_item.items()
            if key not in visible_keys
        }
    key = "assistant_fields" if kind == "message" else "call_fields"
    value = metadata.get(key, {})
    if not isinstance(value, Mapping):
        _reject()
    return dict(value)


def _apply_metadata(item: dict[str, Any], metadata: Mapping[str, Any], kind: str) -> None:
    raw_item = metadata.get("raw_item")
    if raw_item is not None:
        if not isinstance(raw_item, Mapping) or raw_item.get("type") != kind:
            _reject()
        # The visible identity was validated before this call. Restore the
        # complete provider item, including its native id and opaque fields.
        item.clear()
        item.update(copy.deepcopy(dict(raw_item)))
        return
    key = "assistant_fields" if kind == "message" else "call_fields"
    if key not in metadata:
        _reject()
    value = metadata[key]
    if not isinstance(value, Mapping) or not value:
        _reject()
    for sub_key, sub_value in value.items():
        if not isinstance(sub_key, str) or not sub_key:
            _reject()
        item[sub_key] = sub_value


def _chat_message_text(item: Mapping[str, Any]) -> str | None:
    if item.get("role") != "assistant":
        return None
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        joined = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "output_text"
        )
        if joined:
            return joined
    return None


def _responses_input_text(item: Mapping[str, Any]) -> str | None:
    if item.get("type") != "message" or item.get("role") != "assistant":
        return None
    content = item.get("content") or []
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for part in content:
        if (
            isinstance(part, Mapping)
            and part.get("type") == "output_text"
            and isinstance(part.get("text"), str)
        ):
            parts.append(part["text"])
    if not parts:
        return None
    return "".join(parts)


def _match_responses_item(
    wire_item: Mapping[str, Any],
    record_visible: Mapping[str, Any],
    record_kind: str,
) -> bool:
    wire_kind = wire_item.get("type")
    if record_kind == "reasoning":
        if wire_kind != "reasoning":
            return False
        try:
            wire_identity = _sibling_item_identity(dict(wire_item))
        except ContinuationError:
            return False
        return wire_identity == _sibling_item_identity(dict(record_visible))
    if record_kind == "function_call":
        if wire_kind != "function_call":
            return False
        try:
            wire_identity = _sibling_item_identity(dict(wire_item))
        except ContinuationError:
            return False
        return wire_identity == _sibling_item_identity(dict(record_visible))
    if record_kind == "message":
        if wire_kind != "message":
            return False
        wire_text = _responses_input_text(wire_item)
        record_identity = _sibling_item_identity(dict(record_visible))
        record_text = "".join(
            part.get("text", "")
            for part in record_identity.get("content", [])
            if isinstance(part, dict) and part.get("type") == "output_text"
        )
        return wire_text is not None and wire_text == record_text
    return False


def _match_chat_message(
    wire_item: Mapping[str, Any],
    record_visible: Mapping[str, Any],
) -> bool:
    wire_text = _chat_message_text(wire_item)
    if wire_text is None:
        return False
    record_identity = _sibling_item_identity(dict(record_visible))
    record_text = "".join(
        part.get("text", "")
        for part in record_identity.get("content", [])
        if isinstance(part, dict) and part.get("type") == "output_text"
    )
    return wire_text == record_text


def apply_continuation_items(
    body: dict[str, Any],
    records: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any]]],
    wire: WireKind,
) -> ApplyResult:
    """Apply provider-continuation records to a wire request body.

    Matching is done by visible identity inside the trusted wire body — never
    by item_ref, because Codex/engine normalization strips host/provider
    item IDs. Records that match a wire item are merged in place; records
    that do not match are counted as ``unmatched`` and **never** prepended
    to the body, because doing so would duplicate replayed history.

    Required metadata per item kind:

    - ``message``: ``assistant_fields`` must be a non-empty mapping.
    - ``function_call``: ``call_fields`` must be a non-empty mapping.
    - ``reasoning``: ``call_fields`` must be a non-empty mapping so
      provider-private fields like ``encrypted_content`` and
      ``reasoning_details`` round-trip into the wire.

    A recognized continuation that is missing the required metadata raises
    before any network call.
    """
    if not isinstance(body, dict):
        _reject()
    _validate_records(records)
    kind = _kind_for_wire(wire)

    normalized_records: list[tuple[Mapping[str, Any], Mapping[str, Any], str | None]] = []
    for entry in records:
        if len(entry) == 2:
            visible_item, metadata = entry
            response_id = None
        elif len(entry) == 3:
            visible_item, metadata, response_id = entry
            if response_id is not None and not isinstance(response_id, str):
                _reject()
        else:
            _reject()
        normalized_records.append((visible_item, metadata, response_id))

    if kind == "responses":
        items = body.get("input")
        if not isinstance(items, list):
            _reject()
        matched = 0
        unmatched = 0
        matched_response_ids: set[str] = set()
        matched_positions: dict[str, int] = {}
        used_positions: set[int] = set()
        missing_reasoning: list[tuple[Mapping[str, Any], Mapping[str, Any], str | None]] = []
        for visible_item, metadata, response_id in normalized_records:
            try:
                record_identity = _sibling_item_identity(dict(visible_item))
            except ContinuationError:
                _reject()
            record_kind = record_identity.get("type")
            if record_kind not in {"message", "function_call", "reasoning"}:
                _reject()
            applied = False
            for position, wire_item in enumerate(items):
                if not isinstance(wire_item, dict):
                    continue
                if position in used_positions:
                    continue
                if _match_responses_item(wire_item, visible_item, record_kind):
                    _apply_metadata(wire_item, metadata, record_kind)
                    matched += 1
                    used_positions.add(position)
                    if response_id is not None:
                        matched_response_ids.add(response_id)
                        matched_positions.setdefault(response_id, position)
                    applied = True
                    break
            if not applied:
                if record_kind == "reasoning":
                    missing_reasoning.append((visible_item, metadata, response_id))
                else:
                    unmatched += 1
        # The normalized engine history intentionally omits reasoning items.
        # Reinsert an ordered group only beside a visible item from the same
        # response; never prepend opaque reasoning globally.
        reasoning_groups: dict[str, list[dict[str, Any]]] = {}
        for _visible_item, metadata, response_id in missing_reasoning:
            if response_id is None or response_id not in matched_response_ids:
                unmatched += 1
                continue
            raw_item = metadata.get("raw_item")
            if not isinstance(raw_item, Mapping):
                _reject()
            reasoning_groups.setdefault(response_id, []).append(
                copy.deepcopy(dict(raw_item))
            )
        for response_id in sorted(
            reasoning_groups,
            key=lambda value: matched_positions[value],
            reverse=True,
        ):
            insert_at = matched_positions[response_id]
            group = reasoning_groups[response_id]
            items[insert_at:insert_at] = group
            matched += len(group)
        return ApplyResult(matched=matched, unmatched=unmatched)

    messages = body.get("messages")
    if not isinstance(messages, list):
        _reject()
    matched = 0
    unmatched = 0
    used_positions: set[int] = set()
    for visible_item, metadata, response_id in normalized_records:
        try:
            record_identity = _sibling_item_identity(dict(visible_item))
        except ContinuationError:
            _reject()
        record_kind = record_identity.get("type")
        if record_kind == "reasoning":
            # Chat wire has no native reasoning slot; provider-private state
            # for reasoning is therefore not replayable here.
            unmatched += 1
            continue
        if record_kind not in {"message", "function_call"}:
            _reject()
        if record_kind == "function_call":
            # Chat wire has no native function_call slot either; chat-tool
            # records carry no replayable metadata, so we count and skip.
            unmatched += 1
            continue
        applied = False
        for position, wire_item in enumerate(messages):
            if not isinstance(wire_item, dict):
                continue
            if position in used_positions:
                continue
            if _match_chat_message(wire_item, visible_item):
                _apply_metadata(wire_item, metadata, "message")
                matched += 1
                used_positions.add(position)
                applied = True
                break
        if not applied:
            unmatched += 1
    return ApplyResult(matched=matched, unmatched=unmatched)


def _responses_tools(request: RunRequest) -> list[dict[str, Any]]:
    definitions = tool_definitions_to_wire(request.tools)
    parse_tool_definitions(definitions)
    tools: list[dict[str, Any]] = []
    for definition in definitions:
        tool = {
            "type": "function",
            "name": definition["name"],
            "parameters": definition["input_schema"],
        }
        if "description" in definition:
            tool["description"] = definition["description"]
        tools.append(tool)
    return tools


def _apply_options(payload: dict[str, Any], request: RunRequest) -> None:
    options = run_options_to_wire(request.options)
    for name in (
        "instructions",
        "service_tier",
        "max_output_tokens",
        "parallel_tool_calls",
    ):
        if name in options:
            payload[name] = options[name]
    if "reasoning_effort" in options:
        payload["reasoning"] = {"effort": options["reasoning_effort"]}
    if "output_format" in options:
        payload["text"] = {"format": options["output_format"]}
    if "tool_choice" in options:
        choice = options["tool_choice"]
        if choice["type"] == "named":
            payload["tool_choice"] = {
                "type": "function",
                "name": choice["tool_name"],
            }
        else:
            payload["tool_choice"] = choice["type"]


def build_responses_request(request: RunRequest) -> dict[str, Any]:
    """Build a detached OpenAI Responses body from one typed run request."""

    if (
        type(request) is not RunRequest
        or type(request.route_snapshot) is not RouteSnapshot
        or type(request.route_snapshot.provider_model_id) is not str
        or not request.route_snapshot.provider_model_id
        or type(request.tools) is not tuple
    ):
        _reject()

    payload: dict[str, Any] = {}
    failed = False
    try:
        normalized_input = normalized_messages_to_wire(request.input)
        tools = _responses_tools(request)
        payload = {
            "model": request.route_snapshot.provider_model_id,
            "input": normalized_input,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        _apply_options(payload, request)
    except Exception:
        failed = True
    if failed:
        _reject()
    return payload


def _has_image_tool_result(responses_request: dict[str, Any]) -> bool:
    for item in responses_request["input"]:
        if item["type"] != "function_call_output":
            continue
        output = item["output"]
        if type(output) is list and any(
            part["type"] == "input_image" for part in output
        ):
            return True
    return False


def build_chat_request(
    request: RunRequest,
    provider_id: str | None = None,
    continuation_records: Sequence[tuple[Mapping[str, Any], Mapping[str, Any], str | None]] | None = None,
) -> dict[str, Any]:
    """Build chat.completions via the existing Responses translation helper."""

    responses_request = build_responses_request(request)
    if _has_image_tool_result(responses_request):
        _reject()
    failed = False
    load_record = None
    if continuation_records:
        used_record_positions: set[int] = set()

        def load_record(item: Mapping[str, Any], _index: int) -> dict[str, Any] | None:
            try:
                identity = _sibling_item_identity(dict(item))
            except ContinuationError:
                _reject()
            for position, (visible_item, metadata, response_id) in enumerate(
                continuation_records
            ):
                if position in used_record_positions:
                    continue
                try:
                    if _sibling_item_identity(dict(visible_item)) == identity:
                        used_record_positions.add(position)
                        translated_metadata = dict(metadata)
                        raw_item = metadata.get("raw_item")
                        if isinstance(raw_item, Mapping):
                            visible_keys = {
                                "type", "id", "role", "content", "call_id", "name", "arguments",
                            }
                            private_fields = {
                                key: value for key, value in raw_item.items()
                                if key not in visible_keys
                            }
                            if raw_item.get("type") == "message":
                                translated_metadata.setdefault("assistant_fields", private_fields)
                            elif raw_item.get("type") == "function_call":
                                translated_metadata.setdefault("call_fields", private_fields)
                                translated_metadata.setdefault("assistant_fields", {})
                        return {"metadata": translated_metadata, "response_id": response_id}
                except ContinuationError:
                    _reject()
            return None
    try:
        chat_request = chat_request_from_responses(
            responses_request,
            load_record=load_record,
            provider_id=provider_id,
        )
    except ContinuationError:
        failed = True
    if failed:
        _reject()
    if "service_tier" in responses_request:
        chat_request["service_tier"] = responses_request["service_tier"]
    return chat_request
