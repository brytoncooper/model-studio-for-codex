"""Pure Codex Responses input normalization for engine run admission.

The host adapter owns recognition of Codex-native history.  This module emits
only the application-owned normalized input union and delegates opaque
continuation material to explicit collaborators supplied by the caller.
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Callable, Mapping
from typing import Any

from model_deck.engine.runs.input_codec import (
    NormalizedInputValidationError,
    parse_normalized_messages,
)
from model_deck.engine.runs.ports import NormalizedRunInput

__all__ = [
    "CodexInputNormalizationError",
    "normalize_codex_input",
]


DEFAULT_FUNCTION_NAMESPACE = "functions"
COMPACTION_SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary of its thinking "
    "process. You also have access to the state of the tools that were used by that language model. "
    "Use this to build on the work that has already been done and avoid duplicating work. Here is the "
    "summary produced by the other language model, use the information in this summary to assist with "
    "your own analysis:"
)

_INVALID_ERROR = "Codex input could not be normalized."
_CALLBACK_ERROR = "Codex continuation input could not be normalized."
_REASONING_ERROR = "Codex reasoning history requires a continuation adapter."
_COMPACTION_ERROR = "Codex compaction history could not be decoded."
_FREEFORM_ERROR = "Codex freeform tool history is unsupported."
_CONTINUATION_CONTROL_ERROR = "Codex continuation control input requires a continuation adapter."


class CodexInputNormalizationError(ValueError):
    """A display-safe failure to convert Codex-native input."""


def _reject(message: str = _INVALID_ERROR) -> None:
    raise CodexInputNormalizationError(message)


def _normalized_namespace(namespace: Any) -> Any:
    if namespace is not None and not isinstance(namespace, str):
        _reject()
    if namespace in (None, "", DEFAULT_FUNCTION_NAMESPACE):
        return None
    return namespace


def _content_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}] if content else []
    if type(content) is not list:
        _reject()

    parts: list[dict[str, Any]] = []
    for part in content:
        if type(part) is not dict:
            _reject()
        kind = part.get("type")
        if kind in ("input_text", "output_text"):
            text = part.get("text")
            if not isinstance(text, str):
                _reject()
            parts.append({"type": kind, "text": text})
            continue
        if kind == "input_image":
            image_url = part.get("image_url")
            if not isinstance(image_url, str) or not image_url:
                _reject()
            image = {"type": "input_image", "image_url": image_url}
            if "detail" in part:
                image["detail"] = part["detail"]
            parts.append(image)
            continue
        _reject()
    return parts


def _strict_json_value(value: Any, *, active: set[int], depth: int = 0) -> None:
    if depth > 64:
        _reject()
    if value is None or type(value) in (bool, str, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            _reject()
        return
    if type(value) not in (list, dict):
        _reject()

    identity = id(value)
    if identity in active:
        _reject()
    active.add(identity)
    try:
        if type(value) is list:
            for entry in value:
                _strict_json_value(entry, active=active, depth=depth + 1)
        else:
            for key, entry in value.items():
                if type(key) is not str:
                    _reject()
                _strict_json_value(entry, active=active, depth=depth + 1)
    finally:
        active.remove(identity)


def _strict_json_object(value: dict[str, Any]) -> str:
    _strict_json_value(value, active=set())
    failed = False
    try:
        encoded = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        failed = True
        encoded = ""
    if failed:
        _reject()
    return encoded


def _function_output(output: Any, *, depth: int = 0) -> Any:
    if depth > 16:
        _reject()
    if isinstance(output, str):
        return output
    if type(output) is list:
        parts = _content_parts(output)
        if all(part["type"] == "input_text" for part in parts):
            return "\n".join(part["text"] for part in parts)
        return parts
    if type(output) is dict and isinstance(output.get("content"), (str, list)):
        return _function_output(output["content"], depth=depth + 1)
    if output is None:
        return ""
    if type(output) is dict:
        return _strict_json_object(output)
    _reject()


def _agent_message(item: dict[str, Any]) -> dict[str, Any]:
    content = item.get("content")
    if type(content) is not list:
        _reject()
    text_parts: list[str] = []
    for part in content:
        if type(part) is not dict:
            _reject()
        if part.get("type") != "input_text" or not isinstance(part.get("text"), str):
            _reject()
        text_parts.append(part["text"])
    text = "".join(text_parts)
    if not text:
        _reject()
    author = item.get("author", "unknown")
    if not isinstance(author, str) or not author:
        author = "unknown"
    return {
        "type": "message",
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": f"Message from agent {author}:\n{text}",
            }
        ],
    }


def _compaction_message(summary: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": f"{COMPACTION_SUMMARY_PREFIX}\n{summary}",
            }
        ],
    }


def _safe_deepcopy(value: Any) -> Any:
    failed = False
    try:
        detached = copy.deepcopy(value)
    except Exception:
        failed = True
        detached = None
    if failed:
        _reject(_CALLBACK_ERROR)
    return detached


def _call_continuation_collaborator(
    collaborator: Callable[[dict[str, Any]], Any],
    item: dict[str, Any],
) -> Any:
    detached = _safe_deepcopy(item)
    failed = False
    try:
        result = collaborator(detached)
    except Exception:
        failed = True
        result = None
    if failed:
        _reject(_CALLBACK_ERROR)
    return result


def normalize_codex_input(
    items: Any,
    alias_map: Mapping[str, tuple[str | None, str]] | None = None,
    *,
    decode_compaction: Callable[[dict[str, Any]], str | None] | None = None,
    normalize_reasoning: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> NormalizedRunInput:
    """Convert Codex Responses history into detached normalized run input.

    ``alias_map`` is a trusted result of the separate host tool-definition
    conversion.  It restores the authorized normalized tool name for a Codex
    ``(namespace, name)`` pair; this function does not authorize tools.

    Opaque compaction and reasoning items require explicit collaborators.
    Collaborator failures are replaced with a fixed display-safe error so
    encrypted continuation material cannot appear in diagnostics.
    """

    if items is None:
        items = []
    if type(items) is not list:
        _reject()

    reverse_aliases = {
        identity: alias for alias, identity in (alias_map or {}).items()
    }
    normalized: list[dict[str, Any]] = []

    for source_item in items:
        if type(source_item) is not dict:
            _reject()
        kind = source_item.get("type")

        if kind == "message":
            role = source_item.get("role", "user")
            parts = _content_parts(source_item.get("content"))
            if isinstance(source_item.get("content"), str) and role == "assistant":
                parts = [{"type": "output_text", "text": part["text"]} for part in parts]
            normalized.append({"type": "message", "role": role, "content": parts})
            continue

        if kind == "function_call":
            name = source_item.get("name", "")
            if not isinstance(name, str) or not name:
                _reject()
            namespace = _normalized_namespace(source_item.get("namespace"))
            identity = (namespace, name)
            arguments = source_item["arguments"] if "arguments" in source_item else "{}"
            normalized.append(
                {
                    "type": "function_call",
                    "name": reverse_aliases.get(identity, name),
                    "arguments": arguments,
                    "call_id": source_item.get("call_id", ""),
                }
            )
            continue

        if kind == "function_call_output":
            normalized.append(
                {
                    "type": "function_call_output",
                    "call_id": source_item.get("call_id", ""),
                    "output": _function_output(source_item.get("output")),
                }
            )
            continue

        if kind == "agent_message":
            normalized.append(_agent_message(source_item))
            continue

        if kind == "compaction":
            if decode_compaction is None:
                _reject(_COMPACTION_ERROR)
            summary = _call_continuation_collaborator(
                decode_compaction,
                source_item,
            )
            if not isinstance(summary, str) or not summary:
                _reject(_COMPACTION_ERROR)
            normalized.append(_compaction_message(summary))
            continue

        if kind == "reasoning":
            if normalize_reasoning is None:
                _reject(_REASONING_ERROR)
            reasoning_items = _call_continuation_collaborator(
                normalize_reasoning,
                source_item,
            )
            if type(reasoning_items) is not list:
                _reject(_CALLBACK_ERROR)
            normalized.extend(_safe_deepcopy(reasoning_items))
            continue

        if kind in ("custom_tool_call", "custom_tool_call_output"):
            _reject(_FREEFORM_ERROR)
        if kind in ("compaction_trigger", "additional_tools"):
            _reject(_CONTINUATION_CONTROL_ERROR)
        _reject()

    failed = False
    try:
        return parse_normalized_messages(normalized)
    except NormalizedInputValidationError:
        failed = True
    if failed:
        _reject()
