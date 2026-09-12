"""Pure Responses-to-chat-completions request translation.

Extracted behavior reference: ``chat_wire.chat_request_from_responses`` plus
``chat_wire._apply_reasoning_settings``. This module performs no I/O, reads no
environment, imports no legacy runtime, and holds no credentials.

Host-state boundary: the legacy ``continuation.load(scope, item)`` lookup is
replaced by an explicit ``load_record`` callable supplied by the caller, and
the legacy ``provider_for_base_url`` resolution is replaced by an explicit
``provider_id`` string. Anything needing live host state stays outside.
"""
import copy
import json
import uuid


class ContinuationError(ValueError):
    """Display-safe translation failure; never includes stored reasoning."""


def _default_new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def apply_reasoning_settings(payload, request, provider_id=None):
    """Apply provider-specific reasoning controls to a chat payload.

    Preserves legacy ``_apply_reasoning_settings`` exactly: DeepSeek V4
    thinking toggle plus effort remap, Gemini effort remap with the
    disable-thinking guard, and no verified control for other providers.
    """
    effort = (request.get("reasoning") or {}).get("effort")
    model = str(request.get("model") or "")
    if not effort:
        return
    if provider_id == "deepseek" and model.startswith("deepseek-v4-"):
        payload["thinking"] = {"type": "disabled" if effort == "none" else "enabled"}
        if effort != "none":
            payload["reasoning_effort"] = {"minimal": "low", "medium": "high", "xhigh": "high",
                                            "ultra": "max"}.get(effort, effort)
    elif provider_id == "google" and model.startswith(("gemini-2.5-", "gemini-3", "gemini-4")):
        if effort == "none" and not model.startswith("gemini-2.5-flash"):
            raise ContinuationError("This Gemini model cannot disable thinking. Choose a supported reasoning level.")
        payload["reasoning_effort"] = {"xhigh": "high", "max": "high", "ultra": "high"}.get(effort, effort)


def chat_request_from_responses(request, load_record=None, provider_id=None, new_id=None):
    """Build a chat.completions payload from a Responses-format request.

    :param request: Normalized Responses request dict (output of Responses
        normalization; this function does not perform that normalization).
    :param load_record: Optional callable ``(item, index)`` returning
        ``{"metadata": dict, "response_id": str|None}`` or ``None``.
        Replaces the legacy continuation-store lookup with an explicit input.
    :param provider_id: Explicit provider id string (e.g. ``"deepseek"``).
    :param new_id: Optional callable ``(prefix)`` for generated call ids.
    """
    make_id = new_id or _default_new_id
    messages = []
    if request.get("instructions"):
        messages.append({"role": "system", "content": request["instructions"]})
    last_assistant_response = None
    for index, item in enumerate(request.get("input") or []):
        kind = item.get("type")
        if kind == "reasoning":
            continue
        record = load_record(item, index) if load_record is not None else None
        metadata = record["metadata"] if record else {}
        response_id = record["response_id"] if record else None
        if kind == "message":
            role = "system" if item.get("role") == "developer" else item.get("role", "user")
            parts = item.get("content") or []
            if role == "assistant":
                text = "".join(part.get("text", "") for part in parts if part.get("type") == "output_text")
                assistant = {"role": "assistant", "content": text}
                assistant.update(copy.deepcopy(metadata.get("assistant_fields") or {}))
                messages.append(assistant)
                last_assistant_response = response_id
                continue
            content = []
            for part in parts:
                if part.get("type") == "input_text":
                    content.append({"type": "text", "text": part.get("text", "")})
                elif part.get("type") == "input_image" and part.get("image_url"):
                    content.append({"type": "image_url", "image_url": {"url": part["image_url"]}})
            if all(part["type"] == "text" for part in content):
                content = "".join(part["text"] for part in content)
            messages.append({"role": role, "content": content})
            last_assistant_response = None
        elif kind == "function_call":
            call = {"id": item.get("call_id") or make_id("call"), "type": "function",
                    "function": {"name": item.get("name", ""), "arguments": item.get("arguments") or "{}"}}
            call.update(copy.deepcopy(metadata.get("call_fields") or {}))
            same_response = response_id == last_assistant_response
            if messages and messages[-1].get("role") == "assistant" and same_response:
                messages[-1].setdefault("tool_calls", []).append(call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
            messages[-1].update(copy.deepcopy(metadata.get("assistant_fields") or {}))
            last_assistant_response = response_id
        elif kind == "function_call_output":
            output = item.get("output")
            if isinstance(output, list):
                output = "\n".join(part.get("text", "") for part in output if part.get("type") == "input_text")
            elif not isinstance(output, str):
                output = json.dumps(output)
            messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""), "content": output})
            last_assistant_response = None
    payload = {"model": request.get("model"), "messages": messages, "stream": True,
               "stream_options": {"include_usage": True}}
    tools = request.get("tools") or []
    if tools:
        payload["tools"] = [{"type": "function", "function": {key: value for key, value in tool.items()
                                                                 if key in ("name", "description", "parameters", "strict")}}
                            for tool in tools]
        choice = request.get("tool_choice", "auto")
        if isinstance(choice, dict) and choice.get("type") == "function":
            choice = {"type": "function", "function": {"name": choice.get("name", "")}}
        payload["tool_choice"] = choice
        if "parallel_tool_calls" in request:
            payload["parallel_tool_calls"] = request["parallel_tool_calls"]
    if "max_output_tokens" in request:
        payload["max_tokens"] = request["max_output_tokens"]
    text_format = (request.get("text") or {}).get("format") or {}
    if text_format.get("type") == "json_object":
        payload["response_format"] = {"type": "json_object"}
    elif text_format.get("type") == "json_schema":
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            key: value for key, value in text_format.items() if key in ("name", "description", "schema", "strict")}}
    apply_reasoning_settings(payload, request, provider_id)
    return payload
