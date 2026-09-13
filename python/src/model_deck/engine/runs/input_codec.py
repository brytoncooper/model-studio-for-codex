"""Strict codec for provider-neutral normalized run-input items.

This preparatory codec does not accept Codex or other host-native history.
Host adapters normalize their native input into the canonical tagged item union
before calling :func:`parse_normalized_messages`. Provider adapters serialize
the resulting detached value with :func:`normalized_messages_to_wire`.

All validation failures use one fixed message and never echo input content.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from model_deck.engine.runs.ports import NormalizedRunInput

__all__ = [
    "NORMALIZED_INPUT_ITEM_REF",
    "NormalizedInputValidationError",
    "normalized_messages_to_wire",
    "parse_normalized_messages",
]


NORMALIZED_INPUT_ITEM_REF = (
    "contracts/engine.v1/vocabulary.schema.json"
    "#/definitions/normalized_input_item"
)
_JSON_VALUE_REF = "contracts/common/types.schema.json#/definitions/json_value"

_ERROR_MESSAGE = "normalized messages are invalid"
_MAX_MESSAGES = 256
_MAX_PAYLOAD_BYTES = 1 << 20


class NormalizedInputValidationError(ValueError):
    """A normalized input item or typed value violated the public contract."""


def _reject() -> None:
    raise NormalizedInputValidationError(_ERROR_MESSAGE)


def _encode_json(value: Any) -> bytes:
    encoded: bytes | None = None
    failed = False
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        failed = True
    if failed or encoded is None:
        _reject()
    return encoded


def _encode_bounded_messages(value: list[Any]) -> None:
    encoded = _encode_json({"messages": value})
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        _reject()


def _validate_against_schema(schema_ref: str, item: Any) -> None:
    from model_deck_contracts.validator import (
        SchemaValidationError,
        validate_schema_ref,
    )

    failed = False
    try:
        validate_schema_ref(schema_ref, item)
    except SchemaValidationError:
        failed = True
    if failed:
        _reject()


class _ArgumentDecodeError(ValueError):
    pass


def _reject_duplicate_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _ArgumentDecodeError
        result[key] = value
    return result


def _reject_non_finite(_: str) -> None:
    raise _ArgumentDecodeError


def _validate_function_arguments(item: dict[str, Any]) -> None:
    if item.get("type") != "function_call":
        return
    arguments: Any = None
    failed = False
    try:
        arguments = json.loads(
            item["arguments"],
            object_pairs_hook=_reject_duplicate_key,
            parse_constant=_reject_non_finite,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
        _ArgumentDecodeError,
    ):
        failed = True
    if failed:
        _reject()
    if type(arguments) is not dict:
        _reject()
    _encode_json(arguments)
    _validate_against_schema(_JSON_VALUE_REF, arguments)


def parse_normalized_messages(value: Any) -> NormalizedRunInput:
    """Validate a normalized wire list and return a detached frozen container.

    Tool results retain their ``call_id`` without requiring the matching call
    in this list because continuation history may carry the earlier item.
    """

    if type(value) is not list or len(value) > _MAX_MESSAGES:
        _reject()
    _encode_bounded_messages(value)
    detached: list[Any] | None = None
    failed = False
    try:
        detached = copy.deepcopy(value)
    except RecursionError:
        failed = True
    if failed or detached is None:
        _reject()
    for item in detached:
        _validate_against_schema(NORMALIZED_INPUT_ITEM_REF, item)
        _validate_function_arguments(item)
    return NormalizedRunInput(messages=tuple(detached))


def normalized_messages_to_wire(value: NormalizedRunInput) -> list[dict[str, Any]]:
    """Return a validated detached wire list for a normalized run input."""

    if not isinstance(value, NormalizedRunInput):
        _reject()
    messages: list[Any] | None = None
    failed = False
    try:
        messages = copy.deepcopy(list(value.messages))
    except (TypeError, RecursionError):
        failed = True
    if failed or messages is None:
        _reject()
    normalized = parse_normalized_messages(messages)
    return copy.deepcopy(list(normalized.messages))
