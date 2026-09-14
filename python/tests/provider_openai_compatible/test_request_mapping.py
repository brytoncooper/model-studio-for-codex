from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from typing import Any

from model_deck.engine.routing.ports import ExecutionMode, RouteSnapshot
from model_deck.engine.runs.input_codec import parse_normalized_messages
from model_deck.engine.runs.ports import (
    NormalizedRunInput,
    RunJsonSchemaOutputFormat,
    RunNamedToolChoice,
    RunNoToolChoice,
    RunOptions,
    RunRequest,
    RunServiceTier,
    ToolDefinition,
)
from model_deck.integrations.providers.openai_compatible.request_mapping import (
    ApplyResult,
    OpenAICompatibleRequestMappingError,
    apply_continuation_items,
    build_chat_request,
    build_responses_request,
)


def _route() -> RouteSnapshot:
    return RouteSnapshot(
        registration_id="550e8400-e29b-41d4-a716-446655440001",
        registration_revision=2,
        connection_id="550e8400-e29b-41d4-a716-446655440002",
        connection_revision=3,
        provider_id="com.example.openai-compatible",
        provider_model_id="provider/model-1",
        execution_mode=ExecutionMode.RESPONSES,
    )


def _history(*, image_tool_result: bool = True) -> list[dict]:
    output = [{"type": "input_text", "text": "sunny"}]
    if image_tool_result:
        output.append(
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,AAAA",
                "detail": "high",
            }
        )
    return [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "weather"},
                {
                    "type": "input_image",
                    "image_url": "https://example.invalid/map.png",
                    "detail": "low",
                },
            ],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Checking."}],
        },
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": '{"city":"Oslo"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": output,
        },
    ]


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="lookup",
        description="Look up weather",
        input_schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        host_execution_required=True,
    )


def _full_options(tier: RunServiceTier = RunServiceTier.STANDARD) -> RunOptions:
    return RunOptions(
        instructions="",
        reasoning_effort="high",
        service_tier=tier,
        max_output_tokens=512,
        parallel_tool_calls=False,
        output_format=RunJsonSchemaOutputFormat(
            name="answer",
            schema={
                "type": "object",
                "properties": {"forecast": {"type": "string"}},
                "required": ["forecast"],
                "additionalProperties": False,
            },
            description="Forecast result",
            strict=True,
        ),
        tool_choice=RunNamedToolChoice(tool_name="lookup"),
    )


def _request(
    *,
    history: list[dict] | None = None,
    options: RunOptions | None = None,
    tools: tuple[ToolDefinition, ...] | None = None,
) -> RunRequest:
    return RunRequest(
        run_id="550e8400-e29b-41d4-a716-446655440003",
        session_id="550e8400-e29b-41d4-a716-446655440004",
        client_request_id="client-1",
        idempotency_key="request-map-1",
        route_snapshot=_route(),
        input=parse_normalized_messages(history if history is not None else _history()),
        tools=(_tool(),) if tools is None else tools,
        options=_full_options() if options is None else options,
    )


class ResponsesRequestMappingTests(unittest.TestCase):
    def test_maps_typed_history_flat_tools_and_all_options(self) -> None:
        request = _request()

        mapped = build_responses_request(request)

        self.assertEqual(mapped["model"], "provider/model-1")
        self.assertEqual(mapped["input"], _history())
        self.assertIs(mapped["stream"], True)
        self.assertEqual(
            mapped["tools"],
            [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Look up weather",
                    "parameters": _tool().input_schema,
                }
            ],
        )
        self.assertEqual(mapped["instructions"], "")
        self.assertEqual(mapped["reasoning"], {"effort": "high"})
        self.assertEqual(mapped["service_tier"], "standard")
        self.assertEqual(mapped["max_output_tokens"], 512)
        self.assertIs(mapped["parallel_tool_calls"], False)
        self.assertEqual(
            mapped["text"]["format"],
            {
                "type": "json_schema",
                "name": "answer",
                "schema": _full_options().output_format.schema,
                "description": "Forecast result",
                "strict": True,
            },
        )
        self.assertEqual(
            mapped["tool_choice"],
            {"type": "function", "name": "lookup"},
        )
        self.assertNotIn("host_execution_required", mapped["tools"][0])

    def test_explicit_tiers_none_choice_and_omitted_options_retain_shape(self) -> None:
        for tier in RunServiceTier:
            with self.subTest(tier=tier):
                mapped = build_responses_request(
                    _request(
                        history=[],
                        options=RunOptions(
                            instructions="",
                            service_tier=tier,
                            parallel_tool_calls=False,
                            tool_choice=RunNoToolChoice(),
                        ),
                        tools=(),
                    )
                )
                self.assertEqual(mapped["instructions"], "")
                self.assertIs(mapped["parallel_tool_calls"], False)
                self.assertEqual(mapped["service_tier"], tier.value)
                self.assertEqual(mapped["tool_choice"], "none")
                self.assertNotIn("reasoning", mapped)
                self.assertNotIn("max_output_tokens", mapped)
                self.assertNotIn("text", mapped)
                self.assertNotIn("tools", mapped)

    def test_result_is_deeply_detached_and_does_not_mutate_request(self) -> None:
        request = _request()
        before = copy.deepcopy(request)

        mapped = build_responses_request(request)

        self.assertEqual(request, before)
        mapped["input"][0]["content"][0]["text"] = "mapped mutation"
        mapped["tools"][0]["parameters"]["properties"]["city"]["type"] = "number"
        mapped["text"]["format"]["schema"]["properties"]["forecast"]["type"] = "number"
        self.assertEqual(request.input.messages[0]["content"][0]["text"], "weather")
        self.assertEqual(
            request.tools[0].input_schema["properties"]["city"]["type"],
            "string",
        )
        self.assertEqual(
            request.options.output_format.schema["properties"]["forecast"]["type"],
            "string",
        )

    def test_malformed_typed_messages_fail_with_fixed_safe_error(self) -> None:
        malformed = RunRequest(
            run_id="run",
            session_id="session",
            client_request_id="client",
            idempotency_key="key",
            route_snapshot=_route(),
            input=NormalizedRunInput(messages=("secret malformed history",)),
        )

        with self.assertRaises(OpenAICompatibleRequestMappingError) as caught:
            build_responses_request(malformed)

        self.assertEqual(
            str(caught.exception),
            "openai-compatible request mapping failed",
        )
        self.assertNotIn("secret malformed history", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_malformed_typed_tool_members_fail_with_fixed_safe_error(self) -> None:
        malformed_tools: tuple[Any, ...] = (
            ToolDefinition(
                name=7,
                input_schema={"type": "object"},
                host_execution_required=True,
            ),
            ToolDefinition(
                name="lookup",
                input_schema={"type": "object"},
                host_execution_required="yes",
            ),
            object(),
        )

        for malformed_tool in malformed_tools:
            with self.subTest(tool=type(malformed_tool).__name__):
                with self.assertRaises(OpenAICompatibleRequestMappingError) as caught:
                    build_responses_request(
                        _request(history=[], options=RunOptions(), tools=(malformed_tool,))
                    )
                self.assertEqual(
                    str(caught.exception),
                    "openai-compatible request mapping failed",
                )
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)


class ChatRequestMappingTests(unittest.TestCase):
    def test_reuses_chat_translation_for_text_tool_results_and_user_images(self) -> None:
        request = _request(history=_history(image_tool_result=False))

        mapped = build_chat_request(request)

        self.assertEqual(mapped["model"], "provider/model-1")
        self.assertEqual(
            mapped["messages"][0]["content"][1],
            {
                "type": "image_url",
                "image_url": {
                    "url": "https://example.invalid/map.png",
                    "detail": "low",
                },
            },
        )
        self.assertEqual(mapped["messages"][-1]["role"], "tool")
        self.assertEqual(mapped["messages"][-1]["content"], "sunny")
        self.assertEqual(mapped["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(
            mapped["tool_choice"],
            {"type": "function", "function": {"name": "lookup"}},
        )
        self.assertIs(mapped["parallel_tool_calls"], False)
        self.assertEqual(mapped["max_tokens"], 512)
        self.assertEqual(mapped["service_tier"], "standard")

    def test_rejects_image_tool_results_before_lossy_chat_translation(self) -> None:
        with self.assertRaisesRegex(
            OpenAICompatibleRequestMappingError,
            "^openai-compatible request mapping failed$",
        ):
            build_chat_request(_request())

    def test_wraps_chat_translation_guard_with_fixed_safe_error(self) -> None:
        request = _request(
            history=[],
            options=RunOptions(reasoning_effort="none"),
            tools=(),
        )
        request = replace(
            request,
            route_snapshot=replace(
                request.route_snapshot,
                provider_model_id="gemini-3-pro",
            ),
        )

        with self.assertRaises(OpenAICompatibleRequestMappingError) as caught:
            build_chat_request(request, provider_id="google")

        self.assertEqual(
            str(caught.exception),
            "openai-compatible request mapping failed",
        )
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)


class ApplyContinuationItemsTests(unittest.TestCase):
    """apply_continuation_items must match by visible identity, not by item_ref."""

    def _body(self):
        return {
            "model": "m",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "earlier reply"}],
                },
            ],
            "stream": True,
        }

    def test_responses_apply_replaces_matching_message_in_place(self) -> None:
        body = self._body()
        records = (
            (
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "earlier reply"}],
                },
                {"assistant_fields": {"reasoning_content": "stale"}},
            ),
        )
        apply_continuation_items(body, records, wire="responses")
        self.assertEqual(len(body["input"]), 2)
        self.assertEqual(
            body["input"][1].get("reasoning_content"), "stale"
        )

    def test_responses_apply_reports_unmatched_records_without_prepending(self) -> None:
        body = self._body()
        records = (
            (
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "no-match"}],
                },
                {"assistant_fields": {"reasoning_content": "orphan"}},
            ),
        )
        result = apply_continuation_items(body, records, wire="responses")
        self.assertEqual(result.matched, 0)
        self.assertEqual(result.unmatched, 1)
        # body length unchanged
        self.assertEqual(len(body["input"]), 2)

    def test_responses_apply_raises_for_required_metadata_missing_on_match(self) -> None:
        body = self._body()
        # missing reasoning_content for an item with reasoning field required
        records = (
            (
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "earlier reply"}],
                },
                {"assistant_fields": {}},
            ),
        )
        with self.assertRaises(OpenAICompatibleRequestMappingError):
            apply_continuation_items(body, records, wire="responses")

    def test_responses_apply_preserves_provider_private_fields_on_reasoning(self) -> None:
        body = {
            "model": "m",
            "input": [
                {
                    "type": "reasoning",
                    "id": "rs-1",
                    "summary": [{"type": "summary_text", "text": "thinking"}],
                    "encrypted_content": "blob-1",
                },
            ],
            "stream": True,
        }
        records = (
            (
                {
                    "type": "reasoning",
                    "id": "rs-1",
                    "summary": [{"type": "summary_text", "text": "thinking"}],
                },
                {"call_fields": {"encrypted_content": "blob-1"}},
            ),
        )
        result = apply_continuation_items(body, records, wire="responses")
        self.assertEqual(result.matched, 1)
        self.assertEqual(body["input"][0]["encrypted_content"], "blob-1")

    def test_responses_restore_complete_raw_item_and_reasoning_order(self) -> None:
        body = self._body()
        visible_message = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "earlier reply"}],
        }
        records = (
            (
                {"type": "reasoning", "summary": []},
                {"raw_item": {
                    "type": "reasoning", "id": "rs-first",
                    "summary": [], "encrypted_content": "opaque-first",
                }},
                "resp-1",
            ),
            (
                {"type": "reasoning", "summary": []},
                {"raw_item": {
                    "type": "reasoning", "id": "rs-second",
                    "summary": [], "encrypted_content": "opaque-second",
                }},
                "resp-1",
            ),
            (
                visible_message,
                {"raw_item": {
                    **visible_message,
                    "id": "msg-provider",
                    "encrypted_content": "opaque-message",
                    "signature": "synthetic-signature",
                }},
                "resp-1",
            ),
        )

        result = apply_continuation_items(body, records, wire="responses")

        self.assertEqual(result.matched, 3)
        restored = body["input"][1:4]
        self.assertEqual(
            [item["id"] for item in restored],
            ["rs-first", "rs-second", "msg-provider"],
        )
        self.assertEqual(restored[0]["encrypted_content"], "opaque-first")
        self.assertEqual(restored[2]["signature"], "synthetic-signature")

    def test_responses_disambiguate_repeated_visible_messages_by_record_order(self) -> None:
        visible = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "same reply"}],
        }
        body = {"model": "m", "input": [copy.deepcopy(visible), copy.deepcopy(visible)]}
        records = (
            (visible, {"raw_item": {**visible, "id": "msg-first", "signature": "sig-1"}}, "resp-1"),
            (visible, {"raw_item": {**visible, "id": "msg-second", "signature": "sig-2"}}, "resp-2"),
        )

        result = apply_continuation_items(body, records, wire="responses")

        self.assertEqual(result, ApplyResult(matched=2, unmatched=0))
        self.assertEqual(
            [(item["id"], item["signature"]) for item in body["input"]],
            [("msg-first", "sig-1"), ("msg-second", "sig-2")],
        )

    def test_chat_apply_replaces_matching_messages(self) -> None:
        body = {
            "model": "m",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "earlier reply"},
            ],
            "stream": True,
        }
        records = (
            (
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "earlier reply"}],
                },
                {"assistant_fields": {"reasoning_content": "stale"}},
            ),
        )
        apply_continuation_items(body, records, wire="chat_completions")
        self.assertEqual(
            body["messages"][1].get("reasoning_content"), "stale"
        )


if __name__ == "__main__":
    unittest.main()
