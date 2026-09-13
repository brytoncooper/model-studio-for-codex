"""Focused roundtrip / detachment / rejection tests for the run options codec.

The codec lives in ``model_deck.engine.runs.options`` and is the single
boundary between JSON-shaped wire values and the typed dataclasses in
``ports``. These tests pin its observable behavior; the existing schema
contract test in ``tests/contracts/test_run_options_contract.py`` continues to
own the underlying JSON Schema, so any change to bounds in the canonical
schema will surface here as a roundtrip mismatch rather than as a hidden
default drift.
"""

from __future__ import annotations

import copy
import traceback
import unittest
from types import MappingProxyType
from typing import Any

from model_deck.engine.runs.options import (
    RUN_OPTIONS_REF,
    RunOptionsValidationError,
    parse_run_options,
    run_options_to_wire,
)
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


def _wire(options: RunOptions) -> dict[str, Any]:
    return run_options_to_wire(options)


_FULL_OPTIONS: dict[str, Any] = {
    "instructions": "be concise",
    "reasoning_effort": "xhigh",
    "service_tier": "priority",
    "max_output_tokens": 4096,
    "parallel_tool_calls": True,
    "output_format": {
        "type": "json_schema",
        "name": "answer",
        "description": "structured answer",
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
        "strict": True,
    },
    "tool_choice": {"type": "named", "tool_name": "lookup"},
}


class RunOptionsRefTests(unittest.TestCase):
    def test_ref_points_at_canonical_schema(self) -> None:
        self.assertEqual(
            RUN_OPTIONS_REF,
            "contracts/engine.v1/vocabulary.schema.json#/definitions/run_options",
        )


class OmissionAndDefaultsTests(unittest.TestCase):
    def test_none_and_empty_dict_yield_default_run_options(self) -> None:
        for value in (None, {}):
            with self.subTest(value=value):
                options = parse_run_options(value)
                self.assertEqual(options, RunOptions())
                self.assertEqual(_wire(options), {})

    def test_unknown_top_level_key_is_rejected(self) -> None:
        # The shared ``run_options`` schema sets ``additionalProperties: false``,
        # so unknown top-level keys must be rejected.
        with self.assertRaises(RunOptionsValidationError):
            parse_run_options({"instructions": "ok", "unknown": True})

    def test_parsed_empty_options_serialize_to_empty_object(self) -> None:
        self.assertEqual(_wire(RunOptions()), {})

    def test_empty_instructions_false_parallel_tool_calls_and_standard_are_retained(
        self,
    ) -> None:
        explicit = RunOptions(
            instructions="",
            parallel_tool_calls=False,
            service_tier=RunServiceTier.STANDARD,
        )
        self.assertEqual(
            _wire(explicit),
            {
                "instructions": "",
                "parallel_tool_calls": False,
                "service_tier": "standard",
            },
        )
        self.assertEqual(parse_run_options(_wire(explicit)), explicit)

    def test_parse_rejects_non_object_non_none_inputs(self) -> None:
        for bad in ("string", 0, 1.5, True, False, ["a"], ("a",)):
            with self.subTest(value=bad), self.assertRaises(RunOptionsValidationError):
                parse_run_options(bad)


class OutputFormatTagTests(unittest.TestCase):
    def test_text_variant_roundtrips(self) -> None:
        parsed = parse_run_options({"output_format": {"type": "text"}})
        self.assertEqual(parsed.output_format, RunTextOutputFormat())
        self.assertEqual(_wire(parsed), {"output_format": {"type": "text"}})

    def test_json_object_variant_roundtrips(self) -> None:
        parsed = parse_run_options({"output_format": {"type": "json_object"}})
        self.assertEqual(parsed.output_format, RunJsonObjectOutputFormat())
        self.assertEqual(_wire(parsed), {"output_format": {"type": "json_object"}})

    def test_json_schema_minimal_variant_roundtrips(self) -> None:
        wire = {"type": "json_schema", "name": "minimal", "schema": {}}
        parsed = parse_run_options({"output_format": wire})
        self.assertEqual(
            parsed.output_format,
            RunJsonSchemaOutputFormat(name="minimal", schema={}),
        )
        self.assertEqual(_wire(parsed), {"output_format": wire})

    def test_json_schema_full_variant_roundtrips(self) -> None:
        wire = {
            "type": "json_schema",
            "name": "answer",
            "description": "structured",
            "schema": {"type": "object", "maxProperties": 0},
            "strict": False,
        }
        parsed = parse_run_options({"output_format": wire})
        self.assertEqual(
            parsed.output_format,
            RunJsonSchemaOutputFormat(
                name="answer",
                description="structured",
                schema={"type": "object", "maxProperties": 0},
                strict=False,
            ),
        )
        self.assertEqual(_wire(parsed), {"output_format": wire})


class ToolChoiceTagTests(unittest.TestCase):
    def test_all_tool_choice_tags_roundtrip(self) -> None:
        cases = (
            ({"type": "auto"}, RunAutomaticToolChoice()),
            ({"type": "none"}, RunNoToolChoice()),
            ({"type": "required"}, RunRequiredToolChoice()),
            ({"type": "named", "tool_name": "lookup"}, RunNamedToolChoice(tool_name="lookup")),
        )
        for wire, expected in cases:
            with self.subTest(wire=wire):
                parsed = parse_run_options({"tool_choice": wire})
                self.assertEqual(parsed.tool_choice, expected)
                self.assertEqual(_wire(parsed), {"tool_choice": wire})


class FullOptionsTests(unittest.TestCase):
    def test_full_options_roundtrip_byte_equals(self) -> None:
        parsed = parse_run_options(_FULL_OPTIONS)
        wire = _wire(parsed)
        self.assertEqual(wire, _FULL_OPTIONS)
        self.assertEqual(parse_run_options(wire), parsed)


class InvalidInputRejectionTests(unittest.TestCase):
    def test_schema_rejections_are_translated_to_validation_error(self) -> None:
        invalid: tuple[tuple[str, Any], ...] = (
            ("null instructions", {"instructions": None}),
            ("oversize instructions", {"instructions": "x" * 65_537}),
            ("empty reasoning_effort", {"reasoning_effort": ""}),
            ("oversize reasoning_effort", {"reasoning_effort": "x" * 65}),
            ("unknown service_tier", {"service_tier": "fast"}),
            ("zero max_output_tokens", {"max_output_tokens": 0}),
            ("oversize max_output_tokens", {"max_output_tokens": 10_000_001}),
            ("bool max_output_tokens", {"max_output_tokens": True}),
            ("null parallel_tool_calls", {"parallel_tool_calls": None}),
            ("null output_format", {"output_format": None}),
            ("unknown output_format.type", {"output_format": {"type": "yaml"}}),
            (
                "extra field on text variant",
                {"output_format": {"type": "text", "strict": True}},
            ),
            (
                "json_schema without schema",
                {"output_format": {"type": "json_schema", "name": "answer"}},
            ),
            (
                "json_schema oversize name",
                {"output_format": {"type": "json_schema", "name": "x" * 65, "schema": {}}},
            ),
            (
                "json_schema oversize description",
                {
                    "output_format": {
                        "type": "json_schema",
                        "name": "ok",
                        "description": "x" * 1025,
                        "schema": {},
                    }
                },
            ),
            (
                "json_schema schema not an object",
                {"output_format": {"type": "json_schema", "name": "ok", "schema": []}},
            ),
            (
                "json_schema unknown extra field",
                {
                    "output_format": {
                        "type": "json_schema",
                        "name": "ok",
                        "schema": {},
                        "extra": "x",
                    }
                },
            ),
            ("null tool_choice", {"tool_choice": None}),
            ("unknown tool_choice.type", {"tool_choice": {"type": "sometimes"}}),
            (
                "auto variant with tool_name",
                {"tool_choice": {"type": "auto", "tool_name": "lookup"}},
            ),
            (
                "named variant missing tool_name",
                {"tool_choice": {"type": "named"}},
            ),
            (
                "named oversize tool_name",
                {"tool_choice": {"type": "named", "tool_name": "x" * 129}},
            ),
            (
                "named with extra unknown field",
                {"tool_choice": {"type": "named", "tool_name": "ok", "surprise": True}},
            ),
        )
        for label, payload in invalid:
            with self.subTest(case=label), self.assertRaises(RunOptionsValidationError):
                parse_run_options(payload)

    def test_constructed_invalid_typed_values_rejected_on_serialization(self) -> None:
        # Bool passed as max_output_tokens — annotation is ``int | None`` so
        # typecheck-level linters miss it, but the codec must reject.
        with self.assertRaises(RunOptionsValidationError):
            _wire(RunOptions(max_output_tokens=True))
        # Oversized json_schema name.
        with self.assertRaises(RunOptionsValidationError):
            _wire(
                RunOptions(
                    output_format=RunJsonSchemaOutputFormat(name="x" * 65, schema={})
                )
            )
        # Non-RunServiceTier enum-like string slipped past typing.
        with self.assertRaises(RunOptionsValidationError):
            _wire(RunOptions(service_tier="premium"))  # type: ignore[arg-type]
        # Non-bool passed as parallel_tool_calls.
        with self.assertRaises(RunOptionsValidationError):
            _wire(RunOptions(parallel_tool_calls="yes"))  # type: ignore[arg-type]

    def test_json_schema_payload_requires_strict_json_object_in_both_directions(
        self,
    ) -> None:
        invalid_schemas: tuple[tuple[str, Any], ...] = (
            ("list", []),
            ("tuple", (("type", "object"),)),
            ("scalar", "object"),
            ("integer key", {1: "value"}),
            ("surrogate key", {"\ud800": "value"}),
            ("surrogate value", {"description": "\ud800"}),
            ("nested mapping", {"nested": MappingProxyType({"type": "string"})}),
        )
        for label, schema in invalid_schemas:
            with self.subTest(direction="parse", case=label):
                with self.assertRaises(RunOptionsValidationError) as caught:
                    parse_run_options(
                        {
                            "output_format": {
                                "type": "json_schema",
                                "name": "answer",
                                "schema": schema,
                            }
                        }
                    )
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
            with self.subTest(direction="serialize", case=label):
                with self.assertRaises(RunOptionsValidationError) as caught:
                    _wire(
                        RunOptions(
                            output_format=RunJsonSchemaOutputFormat(
                                name="answer",
                                schema=schema,  # type: ignore[arg-type]
                            )
                        )
                    )
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)


class DetachmentTests(unittest.TestCase):
    def test_caller_input_mutations_do_not_leak_into_parsed_options(self) -> None:
        schema_payload: dict[str, Any] = {"type": "object", "properties": {"x": {"type": "integer"}}}
        output_format = {"type": "json_schema", "name": "answer", "schema": schema_payload}
        tool_choice = {"type": "named", "tool_name": "lookup"}
        payload: dict[str, Any] = {
            "instructions": "be helpful",
            "output_format": output_format,
            "tool_choice": tool_choice,
        }

        options = parse_run_options(payload)

        # Mutate the supplied payload after parse.
        payload["instructions"] = "mutated"
        schema_payload["properties"]["x"]["type"] = "string"
        output_format["name"] = "mutated"
        tool_choice["tool_name"] = "mutated"

        self.assertEqual(options.instructions, "be helpful")
        self.assertEqual(options.output_format.name, "answer")
        self.assertEqual(
            options.output_format.schema,
            {"type": "object", "properties": {"x": {"type": "integer"}}},
        )
        self.assertEqual(options.tool_choice.tool_name, "lookup")

    def test_returned_wire_map_is_detached_from_typed_value(self) -> None:
        schema_obj = {"type": "object"}
        options = RunOptions(
            instructions="hello",
            service_tier=RunServiceTier.PRIORITY,
            max_output_tokens=16,
            parallel_tool_calls=True,
            output_format=RunJsonSchemaOutputFormat(name="answer", schema=schema_obj),
            tool_choice=RunNamedToolChoice(tool_name="lookup"),
        )
        wire = _wire(options)
        expected_wire = {
            "instructions": "hello",
            "service_tier": "priority",
            "max_output_tokens": 16,
            "parallel_tool_calls": True,
            "output_format": {"type": "json_schema", "name": "answer", "schema": {"type": "object"}},
            "tool_choice": {"type": "named", "tool_name": "lookup"},
        }
        self.assertEqual(wire, expected_wire)

        # Mutate the returned wire map and the schema dict inside it.
        wire["instructions"] = "MUTATED"
        wire["output_format"]["name"] = "MUTATED"
        wire["output_format"]["schema"]["type"] = "string"
        wire["tool_choice"]["tool_name"] = "MUTATED"
        wire["max_output_tokens"] = 0

        # The typed value is untouched.
        self.assertEqual(options.instructions, "hello")
        self.assertEqual(options.output_format.name, "answer")
        self.assertEqual(options.output_format.schema, {"type": "object"})
        self.assertEqual(options.tool_choice.tool_name, "lookup")
        self.assertEqual(options.max_output_tokens, 16)

        # A second serialization matches the original wire exactly.
        self.assertEqual(_wire(options), expected_wire)

    def test_parse_detaches_against_supplied_object_graph(self) -> None:
        nested_schema = {"type": "object", "props": {"a": 1, "b": 2}}
        payload = {
            "instructions": "do",
            "output_format": {
                "type": "json_schema",
                "name": "answer",
                "schema": nested_schema,
            },
            "tool_choice": {"type": "named", "tool_name": "lookup"},
        }
        options = parse_run_options(copy.deepcopy(payload))

        # Mutate the original supplied structures (a deep copy was passed in,
        # so this exercises the codec's detachment from the parsed graph).
        nested_schema["props"]["a"] = 999
        nested_schema["new"] = "added"

        self.assertEqual(options.output_format.name, "answer")
        self.assertEqual(
            options.output_format.schema,
            {"type": "object", "props": {"a": 1, "b": 2}},
        )

        # Build the wire first, then mutate the parsed typed value's
        # schema dict; the already-built wire must remain detached from
        # the typed value.
        serialized = _wire(options)
        options.output_format.schema["mutated"] = True
        options.output_format.schema["deep"] = {"key": "value"}
        self.assertNotIn("mutated", serialized["output_format"]["schema"])
        self.assertNotIn("deep", serialized["output_format"]["schema"])
        self.assertEqual(
            serialized["output_format"]["schema"],
            {"type": "object", "props": {"a": 1, "b": 2}},
        )
        self.assertEqual(serialized["tool_choice"]["tool_name"], "lookup")


class ErrorMessageSafetyTests(unittest.TestCase):
    def test_error_message_does_not_echo_supplied_payload(self) -> None:
        secret = "leak-me-please-very-unique-token"
        payload = {
            "instructions": secret,
            "service_tier": "ultra-vip-tier-with-" + secret,
        }
        with self.assertRaises(RunOptionsValidationError) as ctx:
            parse_run_options(payload)
        message = str(ctx.exception)
        self.assertNotIn(secret, message)
        self.assertNotIn(repr(payload), message)

    def test_schema_errors_do_not_retain_payload_in_exception_chain(self) -> None:
        secret = "schema-error-sensitive-sentinel"
        cases = (
            lambda: parse_run_options({"service_tier": secret}),
            lambda: _wire(
                RunOptions(
                    output_format=RunJsonSchemaOutputFormat(
                        name=secret * 3,
                        schema={},
                    )
                )
            ),
        )
        for call in cases:
            with self.subTest(call=call):
                with self.assertRaises(RunOptionsValidationError) as caught:
                    call()
                error = caught.exception
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)
                formatted = "".join(
                    traceback.format_exception(
                        type(error), error, error.__traceback__
                    )
                )
                self.assertNotIn(secret, formatted)


if __name__ == "__main__":
    unittest.main()
