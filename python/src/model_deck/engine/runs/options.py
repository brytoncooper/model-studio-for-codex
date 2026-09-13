"""Strict wire codec for :class:`RunOptions`.

The codec converts between external JSON-shaped ``options`` blocks and the
frozen typed dataclasses in :mod:`model_deck.engine.runs.ports`. Bounds, enum
membership, tagged exclusivity, and unknown-field rejection are delegated to
the bundled canonical ``run_options`` schema via
:func:`model_deck_contracts.validator.validate_schema_ref`, the same path
every other engine caller uses, so the wire shape stays in lockstep with the
contract.

Strict retention: explicit ``False`` ``parallel_tool_calls``, empty
``instructions``, and ``"standard"`` service tiers remain distinct values.
Empty :class:`RunOptions` serializes to ``{}``.

Detachment: callers may share mutable structures.
:func:`parse_run_options` deep-copies the input after JSON-shape and schema
validation; :func:`run_options_to_wire` rebuilds a fresh wire map from the
typed value, so neither the supplied input nor the returned wire map shares
identity with the typed value.

Errors: :class:`RunOptionsValidationError` is the single fixed-message
exception; supplied payload content is never echoed.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from model_deck.engine.runs.ports import (
    RunAutomaticToolChoice,
    RunJsonObjectOutputFormat,
    RunJsonSchemaOutputFormat,
    RunNamedToolChoice,
    RunNoToolChoice,
    RunOptions,
    RunRequiredToolChoice,
    RunServiceTier,
    RunTextOutputFormat,
)

__all__ = [
    "RUN_OPTIONS_REF",
    "RunOptionsValidationError",
    "parse_run_options",
    "run_options_to_wire",
]


RUN_OPTIONS_REF = (
    "contracts/engine.v1/vocabulary.schema.json#/definitions/run_options"
)


class RunOptionsValidationError(ValueError):
    """Raised for malformed JSON shape, schema failure, or invalid typed input.

    The message is fixed; the supplied payload is never echoed.
    """


def _reject(message: str) -> None:
    raise RunOptionsValidationError(message)


# JSON-shape validators ----------------------------------------------------

def _assert_json_shape(value: Any, *, depth: int = 0) -> None:
    """Reject non-JSON values, non-string keys, non-finite floats, cycles, etc.

    The schema enforces numeric ranges, string lengths, enum membership, and
    field-allow-lists; this walker enforces only the JSON-shape invariants the
    schema takes for granted (string keys, finite floats, decodeable UTF-8,
    bounded depth). Tuples, sets, byte strings, and ``int``-keyed dicts fail
    here so a single fixed error class describes both kinds of mismatch.
    """

    if depth > 64:
        _reject("run options exceed maximum depth")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        # ``True``/``False`` were filtered above, so any remaining ``int`` is
        # acceptable JSON. Range checks happen during schema validation.
        return
    if isinstance(value, float):
        import math

        if math.isnan(value) or math.isinf(value):
            _reject("run options contain non-finite numbers")
        return
    if isinstance(value, str):
        invalid_utf8 = False
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            invalid_utf8 = True
        if invalid_utf8:
            _reject("run options contain non-utf-8 strings")
        return
    if type(value) is dict:
        for key, sub in value.items():
            if not isinstance(key, str):
                _reject("run options only support string keys")
            _assert_json_shape(key, depth=depth + 1)
            _assert_json_shape(sub, depth=depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _assert_json_shape(item, depth=depth + 1)
        return
    _reject("run options contain non-json values")


def _detach(value: Any) -> Any:
    """Return a deep copy of ``value``. Cycles raise the codec's fixed error."""

    failed = False
    try:
        return copy.deepcopy(value)
    except RecursionError:
        failed = True
    if failed:
        raise RunOptionsValidationError("run options contain a cycle")


# Schema delegation --------------------------------------------------------

def _validate_against_schema(instance: Any) -> None:
    """Validate ``instance`` against the bundled canonical ``run_options`` schema."""

    from model_deck_contracts.validator import (
        SchemaValidationError,
        validate_schema_ref,
    )

    failed = False
    try:
        validate_schema_ref(RUN_OPTIONS_REF, instance)
    except SchemaValidationError:
        failed = True
    if failed:
        raise RunOptionsValidationError(
            "run options failed canonical schema validation"
        )


# Parse-side typed builders -----------------------------------------------

def _coerce_service_tier(value: Any) -> RunServiceTier:
    """Resolve a service tier from a ``str`` or existing enum value.

    No fuzzy matching: only canonical ``"standard"``, ``"priority"``, and
    ``"economy"`` succeed. The schema has already constrained the string to
    those three values; this helper exists so the typed value is the enum
    (not the raw wire string).
    """

    if isinstance(value, RunServiceTier):
        return value
    if isinstance(value, str):
        for member in RunServiceTier:
            if member.value == value:
                return member
    _reject("service_tier must be a known tier")


def _build_output_format(detached: Mapping[str, Any]) -> Any:
    """Build a typed output format from a schema-validated mapping.

    The schema has already enforced the closed tag set, the required fields
    for ``json_schema``, the length bounds on ``name``/``description``, and
    ``additionalProperties: false`` on each variant. This builder only
    dispatches on the tag and constructs the right frozen dataclass.
    """

    type_value = detached["type"]
    if type_value == "text":
        return RunTextOutputFormat()
    if type_value == "json_object":
        return RunJsonObjectOutputFormat()
    if type_value == "json_schema":
        return RunJsonSchemaOutputFormat(
            name=detached["name"],
            schema=dict(detached["schema"]),
            description=detached.get("description"),
            strict=detached.get("strict"),
        )
    _reject("output_format.type must be a known tag")


def _build_tool_choice(detached: Mapping[str, Any]) -> Any:
    """Build a typed tool choice from a schema-validated mapping.

    The schema has already enforced the closed tag set, the required
    ``tool_name`` for the ``named`` variant, the length bound on
    ``tool_name``, and ``additionalProperties: false`` on each variant.
    """

    type_value = detached["type"]
    if type_value == "auto":
        return RunAutomaticToolChoice()
    if type_value == "none":
        return RunNoToolChoice()
    if type_value == "required":
        return RunRequiredToolChoice()
    if type_value == "named":
        return RunNamedToolChoice(tool_name=detached["tool_name"])
    _reject("tool_choice.type must be a known tag")


def parse_run_options(value: Any) -> RunOptions:
    """Parse a JSON-shaped ``options`` block into a :class:`RunOptions`.

    ``None`` and an empty mapping are equivalent and yield ``RunOptions()``.
    Any other mapping is shape-checked, detached, schema-validated, and built
    into the typed value with no caller references retained.

    Raises :class:`RunOptionsValidationError` when the value is malformed; the
    message is fixed and never echoes the supplied payload.
    """

    if value is None:
        return RunOptions()
    if not isinstance(value, Mapping):
        _reject("run options must be a JSON object or omitted")

    normalized: dict[Any, Any] = {}
    conversion_failed = False
    try:
        normalized = dict(value)
    except (TypeError, ValueError, RecursionError):
        conversion_failed = True
    if conversion_failed:
        _reject("run options must be a JSON object or omitted")
    _assert_json_shape(normalized)
    detached = _detach(normalized)
    _validate_against_schema(detached)

    raw_service_tier = detached.get("service_tier")
    service_tier = (
        _coerce_service_tier(raw_service_tier)
        if raw_service_tier is not None
        else None
    )
    raw_output_format = detached.get("output_format")
    output_format = (
        _build_output_format(raw_output_format)
        if raw_output_format is not None
        else None
    )
    raw_tool_choice = detached.get("tool_choice")
    tool_choice = (
        _build_tool_choice(raw_tool_choice) if raw_tool_choice is not None else None
    )

    return RunOptions(
        instructions=detached.get("instructions"),
        reasoning_effort=detached.get("reasoning_effort"),
        service_tier=service_tier,
        max_output_tokens=detached.get("max_output_tokens"),
        parallel_tool_calls=detached.get("parallel_tool_calls"),
        output_format=output_format,
        tool_choice=tool_choice,
    )


# Serialize-side typed guards and builder ---------------------------------

def _validate_typed_options_shape(options: RunOptions) -> None:
    """Reject typed values whose Python-level types cannot reach the wire.

    The canonical schema handles all wire-bound checks (numeric ranges, string
    lengths, enum membership, tagged exclusivity, unknown-field rejection) on
    serialize. This helper only catches Python-level type slips the dataclass
    annotations cannot enforce: ``True`` passed as ``max_output_tokens``, a
    free-form string sneaking past the ``RunServiceTier`` annotation, a
    tagged variant of the wrong concrete class.
    """

    if not isinstance(options, RunOptions):
        _reject("run options must be a RunOptions instance")

    if options.max_output_tokens is not None and (
        isinstance(options.max_output_tokens, bool)
        or not isinstance(options.max_output_tokens, int)
    ):
        _reject("max_output_tokens must be an integer")
    if options.service_tier is not None and not isinstance(
        options.service_tier, RunServiceTier
    ):
        _reject("service_tier must be a RunServiceTier value")
    if options.parallel_tool_calls is not None and not isinstance(
        options.parallel_tool_calls, bool
    ):
        _reject("parallel_tool_calls must be a boolean")
    if options.output_format is not None and not isinstance(
        options.output_format,
        (
            RunTextOutputFormat,
            RunJsonObjectOutputFormat,
            RunJsonSchemaOutputFormat,
        ),
    ):
        _reject("output_format has an unknown type")
    if isinstance(options.output_format, RunJsonSchemaOutputFormat) and type(
        options.output_format.schema
    ) is not dict:
        _reject("output_format.schema must be a JSON object")
    if options.tool_choice is not None and not isinstance(
        options.tool_choice,
        (
            RunAutomaticToolChoice,
            RunNoToolChoice,
            RunRequiredToolChoice,
            RunNamedToolChoice,
        ),
    ):
        _reject("tool_choice has an unknown type")


def _build_wire_payload(options: RunOptions) -> dict[str, Any]:
    """Build a fresh JSON-shaped wire map from a typed ``RunOptions``.

    ``None`` fields are dropped; explicit ``False``, ``""``, and ``STANDARD``
    are preserved. The mapping is built from scratch and re-wrapped in
    ``dict()`` so the returned object cannot share identity with any internal
    scratch container.
    """

    payload: dict[str, Any] = {}

    if options.instructions is not None:
        payload["instructions"] = options.instructions
    if options.reasoning_effort is not None:
        payload["reasoning_effort"] = options.reasoning_effort
    if options.service_tier is not None:
        payload["service_tier"] = options.service_tier.value
    if options.max_output_tokens is not None:
        payload["max_output_tokens"] = options.max_output_tokens
    if options.parallel_tool_calls is not None:
        payload["parallel_tool_calls"] = options.parallel_tool_calls

    output_format = options.output_format
    if isinstance(output_format, RunTextOutputFormat):
        payload["output_format"] = {"type": "text"}
    elif isinstance(output_format, RunJsonObjectOutputFormat):
        payload["output_format"] = {"type": "json_object"}
    elif isinstance(output_format, RunJsonSchemaOutputFormat):
        _assert_json_shape(output_format.schema)
        schema_payload: dict[str, Any] = {
            "type": "json_schema",
            "name": output_format.name,
            "schema": _detach(output_format.schema),
        }
        if output_format.description is not None:
            schema_payload["description"] = output_format.description
        if output_format.strict is not None:
            schema_payload["strict"] = output_format.strict
        payload["output_format"] = schema_payload

    tool_choice = options.tool_choice
    if isinstance(tool_choice, RunAutomaticToolChoice):
        payload["tool_choice"] = {"type": "auto"}
    elif isinstance(tool_choice, RunNoToolChoice):
        payload["tool_choice"] = {"type": "none"}
    elif isinstance(tool_choice, RunRequiredToolChoice):
        payload["tool_choice"] = {"type": "required"}
    elif isinstance(tool_choice, RunNamedToolChoice):
        payload["tool_choice"] = {
            "type": "named",
            "tool_name": tool_choice.tool_name,
        }

    return dict(payload)


def run_options_to_wire(options: RunOptions) -> dict[str, Any]:
    """Serialize a :class:`RunOptions` into its canonical JSON-shaped wire map.

    Empty :class:`RunOptions` (``RunOptions()``) serializes to ``{}``. Every
    other present field is included unconditionally. The returned mapping is
    detached from the typed value; the serialized payload is validated against
    the bundled canonical ``run_options`` schema, so constructed typed values
    that fall outside schema bounds (``max_output_tokens=True``, an overlong
    ``output_format.name``, a free-form service-tier string) raise
    :class:`RunOptionsValidationError` rather than leaking through.
    """

    _validate_typed_options_shape(options)
    payload = _build_wire_payload(options)
    _assert_json_shape(payload)
    _validate_against_schema(payload)
    return payload
