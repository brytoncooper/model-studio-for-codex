from __future__ import annotations

import copy
import math
import unittest

from model_deck.engine.runs.input_codec import normalized_messages_to_wire
from model_deck.integrations.hosts.codex.input_normalization import (
    COMPACTION_SUMMARY_PREFIX,
    CodexInputNormalizationError,
    normalize_codex_input,
)


class CodexInputNormalizationTests(unittest.TestCase):
    def test_real_codex_history_becomes_closed_normalized_items(self) -> None:
        source = [
            {
                "type": "message",
                "role": "developer",
                "id": "msg_1",
                "internal_chat_message_metadata_passthrough": {"turn_id": "t"},
                "content": [{"type": "input_text", "text": "Rules"}],
            },
            {
                "type": "message",
                "role": "user",
                "id": "msg_2",
                "content": [
                    {"type": "input_text", "text": "Inspect this"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,AAAA",
                        "detail": "high",
                    },
                ],
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "name": "spawn_agent",
                "namespace": "multi_agent_v1",
                "arguments": '{"task":"review"}',
                "call_id": "call_1",
                "encrypted_function_args": ["task"],
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [
                    {"type": "input_text", "text": "spawned"},
                    {"type": "input_text", "text": "ready"},
                ],
            },
            {
                "type": "agent_message",
                "id": "agent_1",
                "author": "/root/reviewer",
                "recipient": "/root",
                "content": [{"type": "input_text", "text": "Looks good"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "id": "msg_3",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Done"}],
            },
        ]
        original = copy.deepcopy(source)

        result = normalize_codex_input(
            source,
            {"agents_spawn": ("multi_agent_v1", "spawn_agent")},
        )

        self.assertEqual(source, original)
        self.assertEqual(
            normalized_messages_to_wire(result),
            [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "Rules"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Inspect this"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,AAAA",
                            "detail": "high",
                        },
                    ],
                },
                {
                    "type": "function_call",
                    "name": "agents_spawn",
                    "arguments": '{"task":"review"}',
                    "call_id": "call_1",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "spawned\nready",
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Message from agent /root/reviewer:\nLooks good",
                        }
                    ],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done"}],
                },
            ],
        )

    def test_decoded_compaction_and_explicit_reasoning_collaborator(self) -> None:
        seen_reasoning: list[dict] = []

        def normalize_reasoning(item: dict) -> list[dict]:
            seen_reasoning.append(item)
            return []

        result = normalize_codex_input(
            [
                {
                    "type": "reasoning",
                    "id": "rs_opaque",
                    "encrypted_content": "do-not-log",
                    "summary": [],
                },
                {
                    "type": "compaction",
                    "id": "cmp_1",
                    "encrypted_content": "opaque-summary",
                },
            ],
            decode_compaction=lambda item: (
                "Half done; next: tests."
                if item.get("encrypted_content") == "opaque-summary"
                else None
            ),
            normalize_reasoning=normalize_reasoning,
        )

        self.assertEqual(seen_reasoning[0]["encrypted_content"], "do-not-log")
        self.assertEqual(
            normalized_messages_to_wire(result),
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                f"{COMPACTION_SUMMARY_PREFIX}\n"
                                "Half done; next: tests."
                            ),
                        }
                    ],
                }
            ],
        )

    def test_unknown_material_and_freeform_history_fail_instead_of_dropping(self) -> None:
        cases = (
            (["raw item"], "could not be normalized"),
            (
                [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_audio", "audio": "opaque"}],
                    }
                ],
                "could not be normalized",
            ),
            ([{"type": "additional_tools", "tools": []}], "continuation control"),
            ([{"type": "custom_tool_call", "name": "shell"}], "freeform tool history"),
            (
                [
                    {
                        "type": "agent_message",
                        "content": [
                            {
                                "type": "encrypted_content",
                                "encrypted_content": "secret assignment",
                            }
                        ],
                    }
                ],
                "could not be normalized",
            ),
        )
        for value, message in cases:
            with self.subTest(value=value), self.assertRaisesRegex(
                CodexInputNormalizationError,
                message,
            ) as raised:
                normalize_codex_input(value)
            self.assertNotIn("secret assignment", str(raised.exception))

    def test_opaque_continuation_requires_collaborator_and_hides_callback_error(self) -> None:
        reasoning = [
            {
                "type": "reasoning",
                "id": "rs_1",
                "encrypted_content": "private-reasoning",
            }
        ]
        with self.assertRaisesRegex(
            CodexInputNormalizationError,
            "reasoning history requires a continuation adapter",
        ):
            normalize_codex_input(reasoning)

        compaction = [
            {
                "type": "compaction",
                "encrypted_content": "private-compaction",
            }
        ]
        with self.assertRaisesRegex(
            CodexInputNormalizationError,
            "compaction history could not be decoded",
        ):
            normalize_codex_input(compaction)

        def failing_decoder(_: dict) -> str:
            raise RuntimeError("private-compaction")

        with self.assertRaisesRegex(
            CodexInputNormalizationError,
            "continuation input could not be normalized",
        ) as raised:
            normalize_codex_input(compaction, decode_compaction=failing_decoder)
        self.assertNotIn("private-compaction", str(raised.exception))

    def test_function_output_images_and_invalid_role_shapes_use_engine_contract(self) -> None:
        result = normalize_codex_input(
            [
                {
                    "type": "function_call_output",
                    "call_id": "call-image",
                    "output": {
                        "content": [
                            {"type": "input_text", "text": "result"},
                            {
                                "type": "input_image",
                                "image_url": "https://example.invalid/result.png",
                                "detail": "low",
                            },
                        ]
                    },
                }
            ]
        )
        self.assertEqual(
            normalized_messages_to_wire(result)[0]["output"][1],
            {
                "type": "input_image",
                "image_url": "https://example.invalid/result.png",
                "detail": "low",
            },
        )

        invalid_messages = (
            {
                "type": "message",
                "role": "developer",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": "https://example.invalid/private.png",
                    }
                ],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "input_text", "text": "wrong part"}],
            },
        )
        for item in invalid_messages:
            with self.subTest(item=item), self.assertRaisesRegex(
                CodexInputNormalizationError,
                "could not be normalized",
            ):
                normalize_codex_input([item])

    def test_function_arguments_default_only_when_absent(self) -> None:
        base = {
            "type": "function_call",
            "name": "lookup",
            "call_id": "call-1",
        }
        result = normalize_codex_input([base])
        self.assertEqual(result.messages[0]["arguments"], "{}")

        valid = {**base, "arguments": '{"query":"exact"}'}
        result = normalize_codex_input([valid])
        self.assertEqual(result.messages[0]["arguments"], '{"query":"exact"}')

        for supplied in ("", None, False, 0, []):
            with self.subTest(arguments=supplied), self.assertRaises(
                CodexInputNormalizationError
            ):
                normalize_codex_input([{**base, "arguments": supplied}])

    def test_function_identity_rejects_invalid_name_and_namespace(self) -> None:
        invalid = (
            {"name": "", "namespace": None},
            {"name": ["lookup"], "namespace": None},
            {"name": "lookup", "namespace": ["tools"]},
            {"name": "lookup", "namespace": {"name": "tools"}},
        )
        for identity in invalid:
            item = {
                "type": "function_call",
                "call_id": "call-1",
                "arguments": "{}",
                **identity,
            }
            with self.subTest(identity=identity), self.assertRaisesRegex(
                CodexInputNormalizationError,
                "could not be normalized",
            ):
                normalize_codex_input([item])

    def test_structured_function_output_requires_strict_json_object(self) -> None:
        base = {"type": "function_call_output", "call_id": "call-1"}
        structured = {
            "result": {
                "ok": True,
                "count": 2,
                "ratio": 0.5,
                "items": [None, "done"],
            }
        }
        result = normalize_codex_input([{**base, "output": structured}])
        self.assertEqual(
            result.messages[0]["output"],
            '{"result": {"ok": true, "count": 2, "ratio": 0.5, "items": [null, "done"]}}',
        )

        invalid = (
            {1: "coerced key"},
            {"value": math.nan},
            {"value": math.inf},
            42,
            False,
        )
        for output in invalid:
            with self.subTest(output=output), self.assertRaisesRegex(
                CodexInputNormalizationError,
                "could not be normalized",
            ):
                normalize_codex_input([{**base, "output": output}])

    def test_continuation_failures_have_no_exception_chain_or_private_value(self) -> None:
        class ExplodingCopy:
            def __deepcopy__(self, memo: dict) -> object:
                raise RuntimeError("PRIVATE_SENTINEL")

        inputs = [
            (
                [
                    {
                        "type": "compaction",
                        "encrypted_content": "PRIVATE_SENTINEL",
                    }
                ],
                {"decode_compaction": lambda _: (_ for _ in ()).throw(
                    RuntimeError("PRIVATE_SENTINEL")
                )},
            ),
            (
                [
                    {
                        "type": "compaction",
                        "encrypted_content": ExplodingCopy(),
                    }
                ],
                {"decode_compaction": lambda _: "unused"},
            ),
            (
                [{"type": "reasoning", "encrypted_content": "opaque"}],
                {
                    "normalize_reasoning": lambda _: [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": ExplodingCopy()}
                            ],
                        }
                    ]
                },
            ),
        ]
        for items, collaborators in inputs:
            with self.subTest(items=items), self.assertRaises(
                CodexInputNormalizationError
            ) as raised:
                normalize_codex_input(items, **collaborators)
            error = raised.exception
            self.assertEqual(str(error), "Codex continuation input could not be normalized.")
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
            self.assertNotIn("PRIVATE_SENTINEL", str(error))


if __name__ == "__main__":
    unittest.main()
