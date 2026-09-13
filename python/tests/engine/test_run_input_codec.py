from __future__ import annotations

import json
import traceback
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from model_deck.engine.runs.input_codec import (
    NormalizedInputValidationError,
    normalized_messages_to_wire,
    parse_normalized_messages,
)
from model_deck.engine.runs.ports import NormalizedRunInput
from model_deck_contracts.validator import reset_registry_cache


REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_ROOT = REPO_ROOT / "contracts"
FIXED_ERROR = "normalized messages are invalid"


def _canonical_validator() -> Draft202012Validator:
    resources: dict[str, Resource] = {}
    for path in CONTRACTS_ROOT.rglob("*.schema.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        schema_id = document.get("$id")
        if isinstance(schema_id, str):
            resources[schema_id] = Resource.from_contents(document)
    registry = Registry().with_resources(resources.items())
    return Draft202012Validator(
        {
            "$ref": (
                "contracts/engine.v1/vocabulary.schema.json"
                "#/definitions/normalized_input_item"
            )
        },
        registry=registry,
    )


def _message(role: str = "user") -> dict:
    part_type = "output_text" if role == "assistant" else "input_text"
    return {
        "type": "message",
        "role": role,
        "content": [{"type": part_type, "text": "hello"}],
    }


def _tool_history() -> list[dict]:
    return [
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "weather",
            "arguments": '{"city":"Oslo"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": [
                {"type": "input_text", "text": "sunny"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,AAAA",
                    "detail": "high",
                },
            ],
        },
    ]


def _assert_sanitized_error(
    case: unittest.TestCase,
    operation,
) -> None:
    try:
        operation()
    except NormalizedInputValidationError as exc:
        case.assertEqual(str(exc), FIXED_ERROR)
        case.assertIsNone(exc.__cause__)
        case.assertIsNone(exc.__context__)
        case.assertNotIn("PRIVATE_SENTINEL", "".join(traceback.format_exception(exc)))
    else:
        case.fail("expected NormalizedInputValidationError")


class NormalizedInputSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = _canonical_validator()

    def test_accepts_role_appropriate_messages_and_tool_history(self) -> None:
        for role in ("system", "developer", "user", "assistant"):
            self.validator.validate(_message(role))
        for item in _tool_history():
            self.validator.validate(item)

    def test_rejects_unknown_fields_and_role_inappropriate_parts(self) -> None:
        unknown = _message()
        unknown["id"] = "host-message-id"
        assistant_input = _message("assistant")
        assistant_input["content"][0]["type"] = "input_text"
        developer_image = _message("developer")
        developer_image["content"] = [
            {"type": "input_image", "image_url": "https://example.invalid/image.png"}
        ]

        for item in (unknown, assistant_input, developer_image):
            self.assertFalse(self.validator.is_valid(item))


class NormalizedInputCodecTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_registry_cache()
        self.bundle_patch = mock.patch(
            "model_deck_contracts.validator.schemas_root",
            return_value=REPO_ROOT,
        )
        self.bundle_patch.start()

    def tearDown(self) -> None:
        self.bundle_patch.stop()
        reset_registry_cache()

    def test_parse_and_serialize_are_ordered_detached_inverses(self) -> None:
        source = [_message("user"), *_tool_history(), _message("assistant")]
        parsed = parse_normalized_messages(source)
        source[0]["content"][0]["text"] = "changed"

        wire = normalized_messages_to_wire(parsed)
        self.assertIsInstance(parsed, NormalizedRunInput)
        self.assertEqual(wire[0]["content"][0]["text"], "hello")
        self.assertEqual([item["type"] for item in wire], [
            "message", "function_call", "function_call_output", "message"
        ])
        wire[0]["content"][0]["text"] = "also changed"
        self.assertEqual(parsed.messages[0]["content"][0]["text"], "hello")

    def test_prior_batch_tool_result_is_valid(self) -> None:
        parsed = parse_normalized_messages(
            [{"type": "function_call_output", "call_id": "older-call", "output": "ok"}]
        )
        self.assertEqual(parsed.messages[0]["call_id"], "older-call")

    def test_requires_wire_list_and_closed_known_parts(self) -> None:
        invalid_values = (
            tuple([_message()]),
            [{"type": "message", "role": "user", "content": [{"type": "audio", "data": "x"}]}],
            [{"type": "message", "role": "user", "content": [], "id": "host-id"}],
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaisesRegex(
                NormalizedInputValidationError, f"^{FIXED_ERROR}$"
            ):
                parse_normalized_messages(value)

    def test_rejects_invalid_function_argument_json(self) -> None:
        arguments = (
            "[]",
            "not-json",
            '{"a":1,"a":2}',
            '{"number":NaN}',
            json.dumps({"values": [0] * 4097}),
        )
        for value in arguments:
            item = _tool_history()[0]
            item["arguments"] = value
            with self.subTest(arguments=value), self.assertRaisesRegex(
                NormalizedInputValidationError, f"^{FIXED_ERROR}$"
            ):
                parse_normalized_messages([item])

    def test_enforces_item_and_total_byte_bounds(self) -> None:
        with self.assertRaises(NormalizedInputValidationError):
            parse_normalized_messages([_message()] * 257)
        oversized = [_message(), _message()]
        oversized[0]["content"][0]["text"] = "a" * 600_000
        oversized[1]["content"][0]["text"] = "b" * 600_000
        with self.assertRaises(NormalizedInputValidationError):
            parse_normalized_messages(oversized)

    def test_serializer_revalidates_typed_container(self) -> None:
        with self.assertRaisesRegex(
            NormalizedInputValidationError, f"^{FIXED_ERROR}$"
        ):
            normalized_messages_to_wire(
                NormalizedRunInput(messages=("legacy scalar",))
            )

    def test_failures_are_sanitized_in_both_codec_directions(self) -> None:
        invalid_messages = (
            [{"type": "PRIVATE_SENTINEL"}],
            [{
                "type": "function_call",
                "call_id": "call-1",
                "name": "tool",
                "arguments": "PRIVATE_SENTINEL",
            }],
            [{
                "type": "function_call",
                "call_id": "call-1",
                "name": "tool",
                "arguments": r'{"\ud800":"value"}',
            }],
            [{
                "type": "function_call",
                "call_id": "call-1",
                "name": "tool",
                "arguments": r'{"value":"\ud800"}',
            }],
            [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "\ud800"}],
            }],
        )
        for index, messages in enumerate(invalid_messages):
            with self.subTest(index=index, direction="parse"):
                _assert_sanitized_error(
                    self, lambda messages=messages: parse_normalized_messages(messages)
                )
            with self.subTest(index=index, direction="serialize"):
                typed = NormalizedRunInput(messages=tuple(messages))
                _assert_sanitized_error(
                    self, lambda typed=typed: normalized_messages_to_wire(typed)
                )

        cyclic: list = []
        cyclic.append(cyclic)
        _assert_sanitized_error(self, lambda: parse_normalized_messages(cyclic))
        _assert_sanitized_error(
            self,
            lambda: normalized_messages_to_wire(
                NormalizedRunInput(messages=tuple(cyclic))
            ),
        )


if __name__ == "__main__":
    unittest.main()
