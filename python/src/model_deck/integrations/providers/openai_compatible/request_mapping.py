"""Pure typed-run mapping to OpenAI Responses and chat request bodies."""

from __future__ import annotations

from typing import Any

from model_deck.engine.routing.ports import RouteSnapshot
from model_deck.engine.runs.input_codec import normalized_messages_to_wire
from model_deck.engine.runs.options import run_options_to_wire
from model_deck.engine.runs.ports import RunRequest
from model_deck.engine.runs.tool_definitions import (
    parse_tool_definitions,
    tool_definitions_to_wire,
)

from .translation import ContinuationError, chat_request_from_responses

__all__ = [
    "OpenAICompatibleRequestMappingError",
    "build_chat_request",
    "build_responses_request",
]


_ERROR_MESSAGE = "openai-compatible request mapping failed"


class OpenAICompatibleRequestMappingError(ValueError):
    """A typed request cannot be represented without loss before transport."""


def _reject() -> None:
    raise OpenAICompatibleRequestMappingError(_ERROR_MESSAGE)


def _responses_tools(request: RunRequest) -> list[dict[str, Any]]:
    definitions = tool_definitions_to_wire(request.tools)
    parse_tool_definitions(definitions)
    tools: list[dict[str, Any]] = []
    for definition in definitions:
        tool = {
            "type": "function",
            "name": definition["name"],
            "parameters": definition["input_schema"],
        }
        if "description" in definition:
            tool["description"] = definition["description"]
        tools.append(tool)
    return tools


def _apply_options(payload: dict[str, Any], request: RunRequest) -> None:
    options = run_options_to_wire(request.options)
    for name in (
        "instructions",
        "service_tier",
        "max_output_tokens",
        "parallel_tool_calls",
    ):
        if name in options:
            payload[name] = options[name]
    if "reasoning_effort" in options:
        payload["reasoning"] = {"effort": options["reasoning_effort"]}
    if "output_format" in options:
        payload["text"] = {"format": options["output_format"]}
    if "tool_choice" in options:
        choice = options["tool_choice"]
        if choice["type"] == "named":
            payload["tool_choice"] = {
                "type": "function",
                "name": choice["tool_name"],
            }
        else:
            payload["tool_choice"] = choice["type"]


def build_responses_request(request: RunRequest) -> dict[str, Any]:
    """Build a detached OpenAI Responses body from one typed run request."""

    if (
        type(request) is not RunRequest
        or type(request.route_snapshot) is not RouteSnapshot
        or type(request.route_snapshot.provider_model_id) is not str
        or not request.route_snapshot.provider_model_id
        or type(request.tools) is not tuple
    ):
        _reject()

    payload: dict[str, Any] = {}
    failed = False
    try:
        normalized_input = normalized_messages_to_wire(request.input)
        tools = _responses_tools(request)
        payload = {
            "model": request.route_snapshot.provider_model_id,
            "input": normalized_input,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        _apply_options(payload, request)
    except Exception:
        failed = True
    if failed:
        _reject()
    return payload


def _has_image_tool_result(responses_request: dict[str, Any]) -> bool:
    for item in responses_request["input"]:
        if item["type"] != "function_call_output":
            continue
        output = item["output"]
        if type(output) is list and any(
            part["type"] == "input_image" for part in output
        ):
            return True
    return False


def build_chat_request(
    request: RunRequest,
    provider_id: str | None = None,
) -> dict[str, Any]:
    """Build chat.completions via the existing Responses translation helper."""

    responses_request = build_responses_request(request)
    if _has_image_tool_result(responses_request):
        _reject()
    failed = False
    try:
        chat_request = chat_request_from_responses(
            responses_request,
            provider_id=provider_id,
        )
    except ContinuationError:
        failed = True
    if failed:
        _reject()
    if "service_tier" in responses_request:
        chat_request["service_tier"] = responses_request["service_tier"]
    return chat_request
