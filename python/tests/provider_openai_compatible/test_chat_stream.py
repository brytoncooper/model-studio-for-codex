from __future__ import annotations

import copy
import unittest

from model_deck.integrations.providers.openai_compatible.chat_stream import (
    ChatStreamTranslator,
    ContinuationError,
)


FIXED_IDS = {
    "resp": "resp-fixed",
    "mdk-rs": "reasoning-fixed",
    "mdk-msg": "message-fixed",
    "mdk-fc": "function-fixed",
}


def _fixed_id(prefix):
    return FIXED_IDS[prefix]


TEXT_REASONING_USAGE_CHUNKS = (
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "reasoning_content": "think ",
                    "reasoning_details": [
                        {
                            "index": 0,
                            "type": "reasoning.text",
                            "text": "think ",
                        }
                    ],
                },
            }
        ]
    },
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "reasoning": "more",
                    "reasoning_details": [
                        {"index": 0, "text": "more", "signature": "sig"}
                    ],
                    "content": "Hel",
                },
            }
        ]
    },
    {
        "choices": [
            {
                "index": 0,
                "delta": {"content": "lo"},
                "finish_reason": "stop",
            }
        ]
    },
    {
        "choices": [],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 3,
            "total_tokens": 8,
            "prompt_tokens_details": {"cached_tokens": 2},
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
    },
)


EXPECTED_TEXT_REASONING_USAGE_EVENTS = [
    {"type": "response.created", "response": {"id": "resp-fixed"}},
    {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {
            "type": "reasoning",
            "id": "reasoning-fixed",
            "summary": [],
        },
    },
    {
        "type": "response.reasoning_summary_text.delta",
        "item_id": "reasoning-fixed",
        "output_index": 0,
        "summary_index": 0,
        "delta": "think ",
    },
    {
        "type": "response.reasoning_summary_text.delta",
        "item_id": "reasoning-fixed",
        "output_index": 0,
        "summary_index": 0,
        "delta": "more",
    },
    {
        "type": "response.output_item.added",
        "output_index": 1,
        "item": {
            "type": "message",
            "id": "message-fixed",
            "role": "assistant",
            "status": "in_progress",
            "content": [],
        },
    },
    {
        "type": "response.output_text.delta",
        "item_id": "message-fixed",
        "output_index": 1,
        "content_index": 0,
        "delta": "Hel",
    },
    {
        "type": "response.output_text.delta",
        "item_id": "message-fixed",
        "output_index": 1,
        "content_index": 0,
        "delta": "lo",
    },
    {
        "type": "response.reasoning_summary_text.done",
        "item_id": "reasoning-fixed",
        "output_index": 0,
        "summary_index": 0,
        "text": "think more",
    },
    {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "type": "reasoning",
            "id": "reasoning-fixed",
            "summary": [
                {"type": "summary_text", "text": "think more"}
            ],
        },
    },
    {
        "type": "response.output_text.done",
        "item_id": "message-fixed",
        "output_index": 1,
        "content_index": 0,
        "text": "Hello",
    },
    {
        "type": "response.output_item.done",
        "output_index": 1,
        "item": {
            "type": "message",
            "id": "message-fixed",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": "Hello",
                    "annotations": [],
                }
            ],
        },
    },
    {
        "type": "response.completed",
        "response": {
            "id": "resp-fixed",
            "usage": {
                "input_tokens": 5,
                "output_tokens": 3,
                "total_tokens": 8,
                "input_tokens_details": {"cached_tokens": 2},
                "output_tokens_details": {"reasoning_tokens": 1},
            },
            "status": "completed",
        },
    },
]


TOOL_CHUNKS = (
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "reasoning_details": [
                        {
                            "index": 0,
                            "type": "reasoning.encrypted",
                            "data": "a",
                            "signature": "sig",
                        }
                    ],
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "extra_content": {
                                "google": {"thought_signature": "abc"}
                            },
                            "function": {
                                "name": "look",
                                "arguments": '{"q":',
                            },
                        }
                    ],
                },
            }
        ]
    },
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "reasoning_details": [
                        {"index": 0, "data": "b", "signature": "sig"}
                    ],
                    "tool_calls": [
                        {
                            "index": 0,
                            "extra_content": {
                                "google": {"thought_signature": "abc"}
                            },
                            "function": {"name": "up", "arguments": '"x"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 4,
            "completion_tokens": 2,
            "prompt_cache_hit_tokens": 1,
        },
    },
)


EXPECTED_TOOL_EVENTS = [
    {"type": "response.created", "response": {"id": "resp-fixed"}},
    {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {
            "type": "function_call",
            "id": "continued-item-fixed",
            "call_id": "call-1",
            "name": "look",
            "arguments": "",
            "status": "in_progress",
        },
    },
    {
        "type": "response.function_call_arguments.delta",
        "item_id": "continued-item-fixed",
        "output_index": 0,
        "delta": '{"q":',
    },
    {
        "type": "response.function_call_arguments.delta",
        "item_id": "continued-item-fixed",
        "output_index": 0,
        "delta": '"x"}',
    },
    {
        "type": "response.function_call_arguments.done",
        "item_id": "continued-item-fixed",
        "output_index": 0,
        "arguments": '{"q":"x"}',
    },
    {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "type": "function_call",
            "id": "continued-item-fixed",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": '{"q":"x"}',
            "status": "completed",
        },
    },
    {
        "type": "response.completed",
        "response": {
            "id": "resp-fixed",
            "usage": {
                "input_tokens": 4,
                "output_tokens": 2,
                "total_tokens": 6,
                "input_tokens_details": {"cached_tokens": 1},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
            "status": "completed",
        },
    },
]


class _RecordingContinuation:
    def __init__(self) -> None:
        self.saved = []

    def save(self, scope, response_id, records) -> None:
        self.saved.append((scope, response_id, copy.deepcopy(records)))


class ChatStreamTranslatorTests(unittest.TestCase):
    def test_fragmented_text_reasoning_and_usage_match_stable_fixture(self) -> None:
        chunks = copy.deepcopy(TEXT_REASONING_USAGE_CHUNKS)
        snapshot = copy.deepcopy(chunks)
        translator = ChatStreamTranslator(new_id=_fixed_id)
        events = []
        for chunk in chunks:
            events.extend(translator.feed(chunk))
        events.extend(translator.finish())

        self.assertEqual(events, EXPECTED_TEXT_REASONING_USAGE_EVENTS)
        self.assertEqual(chunks, snapshot)
        self.assertEqual(translator.finish(), [])

    def test_fragmented_tool_and_metadata_match_stable_fixture(self) -> None:
        continuation = _RecordingContinuation()
        translator = ChatStreamTranslator(
            continuation,
            "scope-fixed",
            new_id=_fixed_id,
            new_item_id=lambda scope: "continued-item-fixed",
        )
        events = []
        for chunk in copy.deepcopy(TOOL_CHUNKS):
            events.extend(translator.feed(chunk))
        events.extend(translator.finish())

        self.assertEqual(events, EXPECTED_TOOL_EVENTS)
        self.assertEqual(
            continuation.saved,
            [
                (
                    "scope-fixed",
                    "resp-fixed",
                    [
                        (
                            {
                                "type": "function_call",
                                "id": "continued-item-fixed",
                                "call_id": "call-1",
                                "name": "lookup",
                                "arguments": '{"q":"x"}',
                                "status": "completed",
                            },
                            {
                                "assistant_fields": {
                                    "reasoning_details": [
                                        {
                                            "index": 0,
                                            "type": "reasoning.encrypted",
                                            "data": "ab",
                                            "signature": "sig",
                                        }
                                    ]
                                },
                                "call_fields": {
                                    "extra_content": {
                                        "google": {"thought_signature": "abc"}
                                    }
                                },
                            },
                        )
                    ],
                )
            ],
        )

    def test_terminal_and_metadata_failures_keep_stable_messages(self) -> None:
        translator = ChatStreamTranslator(new_id=_fixed_id)
        translator.feed(
            {
                "choices": [
                    {
                        "index": 1,
                        "delta": {"content": "ignored"},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        failed = translator.finish()
        self.assertEqual(failed[-1]["type"], "response.failed")
        self.assertEqual(
            failed[-1]["response"]["error"]["message"],
            "The provider returned multiple choices for a native agent request.",
        )

        with self.assertRaisesRegex(
            ContinuationError,
            "invalid streamed reasoning metadata",
        ):
            ChatStreamTranslator(new_id=_fixed_id).feed(
                {
                    "choices": [
                        {
                            "delta": {
                                "reasoning_details": [
                                    {"index": 1025, "signature": "blocked"}
                                ]
                            }
                        }
                    ]
                }
            )

    def test_empty_stream_matches_stable_failed_fixture(self) -> None:
        translator = ChatStreamTranslator(new_id=_fixed_id)
        self.assertEqual(
            translator.finish(),
            [
                {"type": "response.created", "response": {"id": "resp-fixed"}},
                {
                    "type": "response.failed",
                    "response": {
                        "id": "resp-fixed",
                        "status": "failed",
                        "error": {
                            "code": "server_error",
                            "message": (
                                "The provider ended the stream without a successful "
                                "terminal event."
                            ),
                        },
                    },
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
