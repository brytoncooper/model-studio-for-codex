from __future__ import annotations

import unittest
import traceback

from model_deck.integrations.providers.openai_compatible.events import (
    OpenAICompatibleEventError,
    ResponsesEventTranslator,
    RunIdentity,
)


RUN_ID = "550e8400-e29b-41d4-a716-446655440003"
SESSION_ID = "550e8400-e29b-41d4-a716-446655440004"
REGISTRATION_ID = "550e8400-e29b-41d4-a716-446655440001"
CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
PROVIDER_MODEL_ID = "example/model-1"
NOW = "2026-09-12T12:00:00Z"


def _identity() -> RunIdentity:
    return RunIdentity(
        session_id=SESSION_ID,
        registration_id=REGISTRATION_ID,
        connection_id=CONNECTION_ID,
        provider_model_id=PROVIDER_MODEL_ID,
    )


def _translator(identity: RunIdentity | None = None) -> ResponsesEventTranslator:
    return ResponsesEventTranslator(RUN_ID, lambda: NOW, identity)


def _expected_usage(
    unit_kind: str,
    units: int,
) -> dict[str, object]:
    return {
        "run_id": RUN_ID,
        "session_id": SESSION_ID,
        "registration_id": REGISTRATION_ID,
        "connection_id": CONNECTION_ID,
        "provider_model_id": PROVIDER_MODEL_ID,
        "observed_at": NOW,
        "units": float(units),
        "unit_kind": unit_kind,
    }


class ResponsesEventTranslatorTests(unittest.TestCase):
    def test_text_usage_and_terminal_are_flat_provider_events(self) -> None:
        translator = _translator(_identity())

        started = translator.translate(
            {"type": "response.created", "response": {"id": "resp-1"}}
        )
        text = translator.translate(
            {"type": "response.output_text.delta", "delta": "hello"}
        )
        translator.translate(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "hello",
                            "annotations": [{"secret": "discarded"}],
                        }
                    ],
                },
            }
        )
        terminal = translator.translate(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-1",
                    "usage": {
                        "input_tokens": 4,
                        "output_tokens": 2,
                        "total_tokens": 6,
                    },
                },
            }
        )

        self.assertEqual([event.kind for event in started], ["run.started"])
        self.assertEqual(text[0].kind, "content.delta")
        self.assertEqual(text[0].payload, {"delta": "hello", "channel": "text"})
        self.assertEqual(
            [event.kind for event in terminal],
            ["usage.observed", "usage.observed", "run.completed"],
        )
        self.assertEqual(
            terminal[0].payload,
            {"usage": _expected_usage("input_tokens", 4)},
        )
        self.assertEqual(
            terminal[1].payload,
            {"usage": _expected_usage("output_tokens", 2)},
        )
        self.assertEqual(
            terminal[2].payload,
            {"terminal_result": {"outcome": "completed"}},
        )
        self.assertTrue(all(event.run_id == RUN_ID for event in (*started, *text, *terminal)))
        self.assertTrue(all(event.observed_at == NOW for event in (*started, *text, *terminal)))
        self.assertEqual(
            translator.completed_output_items,
            (
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello"}],
                },
            ),
        )

    def test_tool_completion_suspends_then_next_segment_terminalizes(self) -> None:
        translator = _translator(_identity())
        translator.translate({"type": "response.created", "response": {}})

        requested = translator.translate(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": '{"city":"Oslo"}',
                    "status": "completed",
                },
            }
        )
        suspended = translator.translate(
            {"type": "response.completed", "response": {"id": "resp-1"}}
        )

        self.assertEqual(len(requested), 1)
        self.assertEqual(requested[0].kind, "tool.requested")
        self.assertEqual(
            requested[0].payload,
            {
                "call_id": "call-1",
                "tool_name": "lookup",
                "arguments": {"city": "Oslo"},
            },
        )
        self.assertEqual(suspended, ())
        self.assertEqual(translator.outstanding_call_id, "call-1")
        self.assertTrue(translator.segment_ended)

        translator.mark_tool_result("call-1")
        self.assertEqual(
            translator.translate({"type": "response.created", "response": {}}),
            (),
        )
        completed = translator.translate(
            {"type": "response.completed", "response": {}}
        )
        self.assertEqual([event.kind for event in completed], ["run.completed"])
        self.assertEqual(
            translator.completed_output_items,
            (
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": '{"city":"Oslo"}',
                },
            ),
        )

    def test_parallel_or_malformed_tool_calls_fail_without_payload_echo(self) -> None:
        translator = _translator(_identity())
        secret = "secret-tool-payload"
        translator.translate({"type": "response.created", "response": {}})
        translator.translate(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": "{}",
                },
            }
        )

        with self.assertRaises(OpenAICompatibleEventError) as captured:
            translator.translate(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": "call-2",
                        "name": "lookup",
                        "arguments": '{"value":"' + secret + '"}',
                    },
                }
            )

        self.assertEqual(str(captured.exception), "openai-compatible event translation failed")
        self.assertNotIn(secret, repr(captured.exception))

    def test_reasoning_text_variant_is_preserved_as_reasoning(self) -> None:
        translator = _translator(_identity())
        translator.translate({"type": "response.created", "response": {}})

        events = translator.translate(
            {"type": "response.reasoning_text.delta", "delta": "working"}
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "content.delta")
        self.assertEqual(events[0].payload["channel"], "reasoning")
        self.assertEqual(
            translator.translate({"type": "response.reasoning_text.done"}), ()
        )

    def test_provider_failures_are_sanitized_and_terminal_once(self) -> None:
        translator = _translator(_identity())
        secret = "secret-provider-message"
        translator.translate({"type": "response.created", "response": {}})

        failed = translator.translate(
            {
                "type": "response.failed",
                "response": {
                    "error": {"message": secret},
                    "usage": {"input_tokens": 1},
                },
            }
        )

        self.assertEqual(
            [event.kind for event in failed],
            ["usage.observed", "run.failed"],
        )
        self.assertEqual(
            failed[0].payload,
            {"usage": _expected_usage("input_tokens", 1)},
        )
        self.assertEqual(
            failed[-1].payload["terminal_result"]["error"],
            {
                "code": "provider_stream_failed",
                "message": "The provider reported a failed response.",
            },
        )
        self.assertNotIn(secret, repr(failed))
        with self.assertRaises(OpenAICompatibleEventError):
            translator.translate({"type": "response.completed", "response": {}})

    def test_inner_failures_have_no_context_or_private_payload(self) -> None:
        sentinel = "PRIVATE_SENTINEL"

        def malformed_arguments() -> None:
            translator = _translator(_identity())
            translator.translate({"type": "response.created", "response": {}})
            translator.translate(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "lookup",
                        "arguments": '{"value":"' + sentinel,
                    },
                }
            )

        def duplicate_arguments() -> None:
            translator = _translator(_identity())
            translator.translate({"type": "response.created", "response": {}})
            translator.translate(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "lookup",
                        "arguments": '{"' + sentinel + '":1,"' + sentinel + '":2}',
                    },
                }
            )

        def failing_clock() -> None:
            def clock() -> str:
                raise RuntimeError(sentinel)

            ResponsesEventTranslator(RUN_ID, clock, _identity()).translate(
                {"type": "response.created", "response": {}}
            )

        for operation in (malformed_arguments, duplicate_arguments, failing_clock):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(OpenAICompatibleEventError) as captured:
                    operation()
                self.assertIsNone(captured.exception.__context__)
                self.assertIsNone(captured.exception.__cause__)
                rendered = "".join(
                    traceback.format_exception(captured.exception)
                )
                self.assertNotIn(sentinel, rendered)

    def test_unsupported_assistant_content_is_rejected_without_partial_history(self) -> None:
        for content in (
            [{"type": "refusal", "refusal": "cannot help"}],
            [
                {"type": "output_text", "text": "partial"},
                {"type": "output_image", "image_url": "private://image"},
            ],
        ):
            with self.subTest(content=content):
                translator = _translator(_identity())
                translator.translate(
                    {"type": "response.created", "response": {}}
                )
                with self.assertRaises(OpenAICompatibleEventError):
                    translator.translate(
                        {
                            "type": "response.output_item.done",
                            "item": {
                                "type": "message",
                                "role": "assistant",
                                "content": content,
                            },
                        }
                    )
                self.assertEqual(translator.completed_output_items, ())


class IdentityAwareUsageEmissionTests(unittest.TestCase):
    def test_usage_without_identity_is_rejected(self) -> None:
        translator = _translator(None)
        translator.translate({"type": "response.created", "response": {}})
        with self.assertRaises(OpenAICompatibleEventError):
            translator.translate(
                {
                    "type": "response.completed",
                    "response": {"usage": {"input_tokens": 1}},
                }
            )

    def test_emits_separate_events_for_input_output_and_cached_tokens(self) -> None:
        translator = _translator(_identity())
        translator.translate({"type": "response.created", "response": {}})

        terminal = translator.translate(
            {
                "type": "response.completed",
                "response": {
                    "usage": {
                        "input_tokens": 9,
                        "output_tokens": 4,
                        "input_tokens_details": {"cached_tokens": 3},
                    }
                },
            }
        )

        self.assertEqual(
            [event.kind for event in terminal[:-1]],
            ["usage.observed", "usage.observed", "usage.observed"],
        )
        self.assertEqual(
            [event.payload["usage"]["unit_kind"] for event in terminal[:-1]],
            ["input_tokens", "output_tokens", "cached_tokens"],
        )
        self.assertEqual(
            [event.payload["usage"]["units"] for event in terminal[:-1]],
            [9.0, 4.0, 3.0],
        )
        for event in terminal[:-1]:
            usage = event.payload["usage"]
            self.assertEqual(usage["run_id"], RUN_ID)
            self.assertEqual(usage["session_id"], SESSION_ID)
            self.assertEqual(usage["registration_id"], REGISTRATION_ID)
            self.assertEqual(usage["connection_id"], CONNECTION_ID)
            self.assertEqual(usage["provider_model_id"], PROVIDER_MODEL_ID)
            self.assertEqual(usage["observed_at"], NOW)
            self.assertNotIn("settled_amount", usage)
            self.assertNotIn("currency", usage)
            self.assertNotIn("estimate_amount", usage)

    def test_missing_token_counters_are_absent_no_zero_fill(self) -> None:
        translator = _translator(_identity())
        translator.translate({"type": "response.created", "response": {}})

        terminal = translator.translate(
            {
                "type": "response.completed",
                "response": {"usage": {"input_tokens": 7}},
            }
        )

        self.assertEqual(
            [event.kind for event in terminal[:-1]],
            ["usage.observed"],
        )
        self.assertEqual(
            terminal[0].payload["usage"]["unit_kind"], "input_tokens"
        )
        self.assertEqual(terminal[0].payload["usage"]["units"], 7.0)

    def test_total_tokens_is_not_normalized(self) -> None:
        translator = _translator(_identity())
        translator.translate({"type": "response.created", "response": {}})

        terminal = translator.translate(
            {
                "type": "response.completed",
                "response": {
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    }
                },
            }
        )

        self.assertEqual(
            [event.kind for event in terminal[:-1]],
            ["usage.observed", "usage.observed"],
        )
        kinds = [event.payload["usage"]["unit_kind"] for event in terminal[:-1]]
        self.assertNotIn("total_tokens", kinds)

    def test_negative_token_values_are_rejected(self) -> None:
        translator = _translator(_identity())
        translator.translate({"type": "response.created", "response": {}})
        with self.assertRaises(OpenAICompatibleEventError):
            translator.translate(
                {
                    "type": "response.completed",
                    "response": {"usage": {"input_tokens": -1}},
                }
            )

    def test_non_integer_token_values_are_skipped(self) -> None:
        translator = _translator(_identity())
        translator.translate({"type": "response.created", "response": {}})
        terminal = translator.translate(
            {
                "type": "response.completed",
                "response": {
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": "two",
                        "total_tokens": 5,
                    }
                },
            }
        )
        # Only the integer input_tokens event is emitted
        self.assertEqual(
            [event.payload["usage"]["unit_kind"] for event in terminal[:-1]],
            ["input_tokens"],
        )

    def test_no_usage_emitted_when_terminal_has_no_usage(self) -> None:
        translator = _translator(_identity())
        translator.translate({"type": "response.created", "response": {}})
        terminal = translator.translate(
            {"type": "response.completed", "response": {}}
        )
        self.assertEqual([event.kind for event in terminal], ["run.completed"])

    def test_constructor_rejects_malformed_identity(self) -> None:
        with self.assertRaises(OpenAICompatibleEventError):
            ResponsesEventTranslator(
                RUN_ID,
                lambda: NOW,
                identity="not-a-dataclass",  # type: ignore[arg-type]
            )

    def test_constructor_rejects_identity_with_blank_fields(self) -> None:
        with self.assertRaises(OpenAICompatibleEventError):
            ResponsesEventTranslator(
                RUN_ID,
                lambda: NOW,
                identity=RunIdentity(
                    session_id="",
                    registration_id=REGISTRATION_ID,
                    connection_id=CONNECTION_ID,
                    provider_model_id=PROVIDER_MODEL_ID,
                ),
            )


if __name__ == "__main__":
    unittest.main()


class RawProviderItemsAccumulationTests(unittest.TestCase):
    """Raw provider items preserve provider-private fields; canonical events strip them.

    Per B15 the provider-private state (``encrypted_content``, ``reasoning_details``,
    ``encrypted_function_args``) must round-trip into a scoped continuation record
    so a later same-scope request can replay it. The canonical view that engine
    events consume must not contain those fields.
    """

    def test_reasoning_output_item_accumulates_raw_provider_state(self) -> None:
        translator = _translator(_identity())
        translator.translate({"type": "response.created", "response": {}})

        reasoning_item = {
            "type": "reasoning",
            "id": "rs-secret-1",
            "summary": [{"type": "summary_text", "text": "thinking"}],
            "encrypted_content": "opaque-blob-1",
            "reasoning_details": [{"type": "summary", "text": "thinking"}],
        }
        events = translator.translate(
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": reasoning_item,
            }
        )

        self.assertEqual(events, ())
        raw = translator.raw_provider_items
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0]["type"], "reasoning")
        self.assertEqual(raw[0]["encrypted_content"], "opaque-blob-1")
        self.assertEqual(
            raw[0]["reasoning_details"], [{"type": "summary", "text": "thinking"}]
        )
        self.assertEqual(translator.completed_output_items, ())

    def test_raw_provider_items_preserve_provider_private_fields_canonical_strips(self) -> None:
        translator = _translator(_identity())
        translator.translate({"type": "response.created", "response": {}})

        translator.translate(
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello"}],
                    "reasoning_details": [{"type": "summary", "text": "leaked"}],
                },
            }
        )
        translator.translate(
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": "{}",
                    "encrypted_function_args": "encrypted-args",
                },
            }
        )

        # Canonical events strip provider-private fields.
        self.assertEqual(
            translator.completed_output_items,
            (
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": "{}",
                },
            ),
        )
        # Raw preserves them so a continuation record can round-trip them.
        raw = translator.raw_provider_items
        self.assertEqual(len(raw), 2)
        self.assertEqual(
            raw[0]["reasoning_details"], [{"type": "summary", "text": "leaked"}]
        )
        self.assertEqual(raw[1]["encrypted_function_args"], "encrypted-args")
        # Canonical view passed to events has no provider-private fields.
        for canonical in translator.completed_output_items:
            self.assertNotIn("reasoning_details", canonical)
            self.assertNotIn("encrypted_function_args", canonical)
            self.assertNotIn("encrypted_content", canonical)

    def test_raw_provider_items_property_isolates_external_mutation(self) -> None:
        translator = _translator(_identity())
        translator.translate({"type": "response.created", "response": {}})

        translator.translate(
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "reasoning",
                    "id": "rs-x",
                    "summary": [{"type": "summary_text", "text": "thinking"}],
                    "encrypted_content": "blob",
                },
            }
        )

        snapshot = translator.raw_provider_items
        snapshot[0]["encrypted_content"] = "tampered"
        self.assertEqual(translator.raw_provider_items[0]["encrypted_content"], "blob")

    def test_raw_provider_items_accumulate_in_output_order(self) -> None:
        translator = _translator(_identity())
        translator.translate({"type": "response.created", "response": {}})

        for index, kind in enumerate(("reasoning", "message", "function_call")):
            if kind == "reasoning":
                item = {
                    "type": "reasoning",
                    "id": f"rs-{index}",
                    "summary": [{"type": "summary_text", "text": "x"}],
                    "encrypted_content": f"blob-{index}",
                }
            elif kind == "message":
                item = {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"m-{index}"}],
                    "reasoning_details": [{"type": "summary", "text": "leaked"}],
                }
            else:
                item = {
                    "type": "function_call",
                    "call_id": f"call-{index}",
                    "name": "lookup",
                    "arguments": "{}",
                    "encrypted_function_args": f"args-{index}",
                }
            translator.translate(
                {"type": "response.output_item.done", "output_index": index, "item": item}
            )

        raw = translator.raw_provider_items
        self.assertEqual(
            [item["type"] for item in raw],
            ["reasoning", "message", "function_call"],
        )
        self.assertEqual(raw[0]["encrypted_content"], "blob-0")
        self.assertEqual(
            raw[1]["reasoning_details"], [{"type": "summary", "text": "leaked"}]
        )
        self.assertEqual(raw[2]["encrypted_function_args"], "args-2")
