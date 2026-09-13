"""Pure chat-completions stream translation into Responses-style events.

This module owns no HTTP, host, engine, credential, or persistence behavior.
An optional continuation collaborator receives completed metadata records, and
ID factories are injectable so isolated tests do not depend on global UUIDs.
"""

from __future__ import annotations

import copy
import json
import uuid


ASSISTANT_METADATA_FIELDS = (
    "reasoning_content",
    "reasoning",
    "reasoning_details",
    "extra_content",
)


class ContinuationError(ValueError):
    """Display-safe stream translation failure."""


def _new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _new_item_id(scope):
    return f"mdkc_{scope}_{uuid.uuid4().hex}"


def _merge_metadata(previous, incoming, field=""):
    """Accumulate streamed reasoning fields, including late signatures."""
    if isinstance(incoming, dict):
        merged = copy.deepcopy(previous) if isinstance(previous, dict) else {}
        for key, value in incoming.items():
            merged[key] = _merge_metadata(merged.get(key), value, key)
        return merged
    if isinstance(incoming, list):
        merged = copy.deepcopy(previous) if isinstance(previous, list) else []
        for position, value in enumerate(incoming):
            index = value.get("index", position) if isinstance(value, dict) else position
            if not isinstance(index, int) or index < 0 or index > 1024:
                raise ContinuationError(
                    "The provider returned invalid streamed reasoning metadata."
                )
            while len(merged) <= index:
                merged.append(None)
            merged[index] = _merge_metadata(merged[index], value, field)
        return merged
    if isinstance(incoming, str) and isinstance(previous, str):
        if field in ("type", "id"):
            return incoming
        if field in ("signature", "thought_signature") and previous == incoming:
            return previous
        return previous + incoming
    return copy.deepcopy(incoming)


class ChatStreamTranslator:
    """Turn chat-completions chunks into Responses events, one item per output."""

    def __init__(
        self,
        continuation=None,
        scope=None,
        *,
        new_id=None,
        new_item_id=None,
    ):
        self._make_id = new_id or _new_id
        self._make_item_id = new_item_id or _new_item_id
        self.response_id = self._make_id("resp")
        self.started = False
        self.next_output_index = 0
        self.message = None
        self.reasoning = None
        self.tool_calls = {}
        self.usage = None
        self.finish_reason = None
        self.error = None
        self.finished = False
        self.continuation = continuation
        self.scope = scope
        self.assistant_fields = {}

    def _item_id(self, prefix):
        if self.continuation is not None:
            return self._make_item_id(self.scope)
        return self._make_id(prefix)

    def feed(self, chunk):
        events = []
        if self.finished or not isinstance(chunk, dict):
            return events
        if not self.started:
            self.started = True
            events.append(
                {"type": "response.created", "response": {"id": self.response_id}}
            )
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        if chunk.get("error"):
            self.error = (
                "The provider reported an error while streaming this response."
            )
            return events
        for choice in chunk.get("choices") or []:
            if choice.get("index", 0) != 0:
                self.error = (
                    "The provider returned multiple choices for a native agent request."
                )
                continue
            delta = choice.get("delta") or {}
            if self.finish_reason and any(
                delta.get(key)
                for key in (
                    "content",
                    "reasoning_content",
                    "reasoning",
                    "tool_calls",
                )
            ):
                self.error = (
                    "The provider sent output after the response had already ended."
                )
            if choice.get("finish_reason"):
                if (
                    self.finish_reason
                    and self.finish_reason != choice["finish_reason"]
                ):
                    self.error = (
                        "The provider returned conflicting response outcomes."
                    )
                self.finish_reason = choice["finish_reason"]
            for field in ASSISTANT_METADATA_FIELDS:
                if field in delta:
                    self.assistant_fields[field] = _merge_metadata(
                        self.assistant_fields.get(field),
                        delta[field],
                        field,
                    )
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                events.extend(self._reasoning_delta(reasoning))
            content = delta.get("content")
            if isinstance(content, str) and content:
                events.extend(self._text_delta(content))
            for call in delta.get("tool_calls") or []:
                if isinstance(call, dict):
                    events.extend(self._tool_call_delta(call))
        return events

    def finish(self):
        if self.finished:
            return []
        self.finished = True
        events = (
            []
            if self.started
            else [
                {
                    "type": "response.created",
                    "response": {"id": self.response_id},
                }
            ]
        )
        self.started = True
        document = {"id": self.response_id}
        if self.usage is not None:
            document["usage"] = self._usage()
        if self.error or self.finish_reason not in (
            "stop",
            "tool_calls",
            "length",
            "content_filter",
        ):
            document.update(
                status="failed",
                error={
                    "code": "server_error",
                    "message": self.error
                    or "The provider ended the stream without a successful terminal event.",
                },
            )
            return events + [{"type": "response.failed", "response": document}]
        if self.finish_reason in ("length", "content_filter"):
            document.update(
                status="incomplete",
                incomplete_details={
                    "reason": (
                        "max_output_tokens"
                        if self.finish_reason == "length"
                        else "content_filter"
                    )
                },
            )
            return events + [
                {"type": "response.incomplete", "response": document}
            ]
        for state in self.tool_calls.values():
            try:
                arguments = json.loads(state["arguments"])
            except (ValueError, TypeError):
                arguments = None
            if (
                not state["announced"]
                or not state["call_id"]
                or not isinstance(arguments, dict)
            ):
                document.update(
                    status="failed",
                    error={
                        "code": "server_error",
                        "message": "The provider returned an incomplete tool call.",
                    },
                )
                return events + [
                    {"type": "response.failed", "response": document}
                ]
        if (self.finish_reason == "tool_calls") != bool(self.tool_calls):
            document.update(
                status="failed",
                error={
                    "code": "server_error",
                    "message": (
                        "The provider's terminal event did not match its tool calls."
                    ),
                },
            )
            return events + [{"type": "response.failed", "response": document}]
        self._save_continuation()
        events.extend(self._close_reasoning())
        events.extend(self._close_message())
        for state in sorted(
            self.tool_calls.values(), key=lambda state: state["index"]
        ):
            events.append(
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": state["id"],
                    "output_index": state["index"],
                    "arguments": state["arguments"],
                }
            )
            events.append(
                {
                    "type": "response.output_item.done",
                    "output_index": state["index"],
                    "item": self._function_item(state),
                }
            )
        document["status"] = "completed"
        events.append({"type": "response.completed", "response": document})
        return events

    def _save_continuation(self):
        if self.continuation is None:
            return
        records = []
        if self.message:
            item = {
                "type": "message",
                "id": self.message["id"],
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": self.message["text"]}
                ],
            }
            records.append((item, {"assistant_fields": self.assistant_fields}))
        for state in self.tool_calls.values():
            records.append(
                (
                    self._function_item(state),
                    {
                        "assistant_fields": self.assistant_fields,
                        "call_fields": state["extra_fields"],
                    },
                )
            )
        if self.reasoning:
            records.append(
                (
                    {
                        "type": "reasoning",
                        "id": self.reasoning["id"],
                        "summary": [
                            {
                                "type": "summary_text",
                                "text": self.reasoning["text"],
                            }
                        ],
                    },
                    {"assistant_fields": self.assistant_fields},
                )
            )
        self.continuation.save(self.scope, self.response_id, records)

    def _usage(self):
        usage = self.usage
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get(
            "cached_tokens", usage.get("prompt_cache_hit_tokens", 0)
        ) or 0
        input_tokens = usage.get("prompt_tokens", 0) or 0
        output_tokens = usage.get("completion_tokens", 0) or 0
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": usage.get(
                "total_tokens", input_tokens + output_tokens
            ),
            "input_tokens_details": {"cached_tokens": cached},
            "output_tokens_details": {
                "reasoning_tokens": (
                    usage.get("completion_tokens_details") or {}
                ).get("reasoning_tokens", 0)
                or 0
            },
        }

    def _take_index(self):
        index = self.next_output_index
        self.next_output_index += 1
        return index

    def _reasoning_delta(self, text):
        events = []
        if self.reasoning is None:
            self.reasoning = {
                "id": self._item_id("mdk-rs"),
                "index": self._take_index(),
                "text": "",
            }
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": self.reasoning["index"],
                    "item": {
                        "type": "reasoning",
                        "id": self.reasoning["id"],
                        "summary": [],
                    },
                }
            )
        self.reasoning["text"] += text
        events.append(
            {
                "type": "response.reasoning_summary_text.delta",
                "item_id": self.reasoning["id"],
                "output_index": self.reasoning["index"],
                "summary_index": 0,
                "delta": text,
            }
        )
        return events

    def _close_reasoning(self):
        if self.reasoning is None or self.reasoning.get("closed"):
            return []
        self.reasoning["closed"] = True
        return [
            {
                "type": "response.reasoning_summary_text.done",
                "item_id": self.reasoning["id"],
                "output_index": self.reasoning["index"],
                "summary_index": 0,
                "text": self.reasoning["text"],
            },
            {
                "type": "response.output_item.done",
                "output_index": self.reasoning["index"],
                "item": {
                    "type": "reasoning",
                    "id": self.reasoning["id"],
                    "summary": [
                        {
                            "type": "summary_text",
                            "text": self.reasoning["text"],
                        }
                    ],
                },
            },
        ]

    def _text_delta(self, text):
        events = []
        if self.message is None:
            self.message = {
                "id": self._item_id("mdk-msg"),
                "index": self._take_index(),
                "text": "",
            }
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": self.message["index"],
                    "item": {
                        "type": "message",
                        "id": self.message["id"],
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                }
            )
        self.message["text"] += text
        events.append(
            {
                "type": "response.output_text.delta",
                "item_id": self.message["id"],
                "output_index": self.message["index"],
                "content_index": 0,
                "delta": text,
            }
        )
        return events

    def _close_message(self):
        if self.message is None or self.message.get("closed"):
            return []
        self.message["closed"] = True
        return [
            {
                "type": "response.output_text.done",
                "item_id": self.message["id"],
                "output_index": self.message["index"],
                "content_index": 0,
                "text": self.message["text"],
            },
            {
                "type": "response.output_item.done",
                "output_index": self.message["index"],
                "item": {
                    "type": "message",
                    "id": self.message["id"],
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": self.message["text"],
                            "annotations": [],
                        }
                    ],
                },
            },
        ]

    def _tool_call_delta(self, call):
        events = []
        position = call.get("index", 0) if isinstance(call.get("index"), int) else 0
        state = self.tool_calls.get(position)
        if state is None:
            state = {
                "id": self._item_id("mdk-fc"),
                "call_id": None,
                "name": "",
                "arguments": "",
                "index": None,
                "announced": False,
                "extra_fields": {},
            }
            self.tool_calls[position] = state
        if call.get("id"):
            if state["announced"] and state["call_id"] != call["id"]:
                self.error = (
                    "The provider changed a tool-call identifier during its response."
                )
            state["call_id"] = call["id"]
        if "extra_content" in call:
            state["extra_fields"]["extra_content"] = _merge_metadata(
                state["extra_fields"].get("extra_content"),
                call["extra_content"],
            )
        function = call.get("function") or {}
        if function.get("name"):
            state["name"] += function["name"]
        if isinstance(function.get("arguments"), str):
            state["arguments"] += function["arguments"]
        if not state["announced"] and state["name"] and state["call_id"]:
            state["announced"] = True
            state["index"] = self._take_index()
            item = self._function_item(state)
            item["arguments"] = ""
            item["status"] = "in_progress"
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": state["index"],
                    "item": item,
                }
            )
        if (
            state["announced"]
            and isinstance(function.get("arguments"), str)
            and function["arguments"]
        ):
            events.append(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": state["id"],
                    "output_index": state["index"],
                    "delta": function["arguments"],
                }
            )
        return events

    @staticmethod
    def _function_item(state):
        return {
            "type": "function_call",
            "id": state["id"],
            "call_id": state["call_id"],
            "name": state["name"],
            "arguments": state["arguments"],
            "status": "completed",
        }
