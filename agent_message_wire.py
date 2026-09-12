"""Plaintext collaboration messages across native Codex provider boundaries.

OpenAI's reserved collaboration schemas encrypt task messages. The router offers
those functions under its own namespace, then restores native call identities and
the plaintext marker consumed by Codex. Existing encrypted history stays opaque.
"""
import json


NATIVE_NAMESPACE = "collaboration"
GENERIC_NAMESPACE = "model_deck_agents"
MESSAGE_FUNCTIONS = frozenset({"spawn_agent", "send_message", "followup_task"})


class AgentMessageError(ValueError):
    pass


def _namespaces(tools):
    """Visit tool namespace definitions, never arbitrary JSON Schema objects."""
    if not isinstance(tools, list):
        return
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "namespace":
            yield tool
            yield from _namespaces(tool.get("tools"))


def _request_namespaces(request):
    yield from _namespaces(request.get("tools"))
    inputs = request.get("input")
    if not isinstance(inputs, list):
        return
    for item in inputs:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            yield from _namespaces(item.get("tools"))


def prepare_request(raw_body):
    """Return rewritten JSON bytes and whether a native wire field changed.

Call once per original outbound request. A caller-owned namespace with our
reserved alias is rejected rather than being mistaken for native agent tools.
Malformed/nonobject JSON passes through for the upstream's normal validation.
"""
    try:
        request = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        return raw_body, False
    if not isinstance(request, dict):
        return raw_body, False
    namespaces = list(_request_namespaces(request))
    if any(namespace.get("name") == GENERIC_NAMESPACE for namespace in namespaces):
        raise AgentMessageError("A tool namespace conflicts with Model Deck's agent-message bridge.")
    changed = False
    for namespace in namespaces:
        if namespace.get("name") != NATIVE_NAMESPACE:
            continue
        namespace["name"] = GENERIC_NAMESPACE
        changed = True
        tools = namespace.get("tools")
        if not isinstance(tools, list):
            continue
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "function" \
                    or tool.get("name") not in MESSAGE_FUNCTIONS:
                continue
            parameters = tool.get("parameters")
            properties = parameters.get("properties") if isinstance(parameters, dict) else None
            message = properties.get("message") if isinstance(properties, dict) else None
            if isinstance(message, dict):
                message.pop("encrypted", None)
    inputs = request.get("input")
    for item in inputs if isinstance(inputs, list) else []:
        if isinstance(item, dict) and item.get("type") == "function_call" \
                and item.get("namespace") == NATIVE_NAMESPACE \
                and (item.get("name") not in MESSAGE_FUNCTIONS or item.get("encrypted_function_args") == []):
            item["namespace"] = GENERIC_NAMESPACE
            changed = True
    if not changed:
        return raw_body, False
    return json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), True


def mark_plaintext_agent_call(item):
    """Mark an already-plaintext native message call; never touch other items."""
    if isinstance(item, dict) and item.get("type") == "function_call" \
            and item.get("namespace") == NATIVE_NAMESPACE and item.get("name") in MESSAGE_FUNCTIONS:
        item["encrypted_function_args"] = []
    return item


def _restore_item(item):
    if isinstance(item, dict) and item.get("type") == "function_call" \
            and item.get("namespace") == GENERIC_NAMESPACE:
        item["namespace"] = NATIVE_NAMESPACE
        mark_plaintext_agent_call(item)


def restore_event(event):
    """Restore native identities in place without changing IDs or reasoning."""
    if not isinstance(event, dict):
        return event
    if event.get("type") in {"response.output_item.added", "response.output_item.done"}:
        _restore_item(event.get("item"))
    response = event.get("response")
    outputs = response.get("output") if isinstance(response, dict) else None
    for item in outputs if isinstance(outputs, list) else []:
        _restore_item(item)
    return event


def reject_encrypted_agent_messages(request):
    """Reject opaque inter-agent assignments before forwarding to another provider."""
    inputs = request.get("input") if isinstance(request, dict) else None
    for item in inputs if isinstance(inputs, list) else []:
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        content = item.get("content")
        encrypted = bool(item.get("encrypted_content"))
        if isinstance(content, list):
            encrypted = encrypted or any(
                isinstance(part, dict) and (part.get("type") == "encrypted_content" or "encrypted_content" in part)
                for part in content)
        if encrypted:
            raise AgentMessageError(
                "This agent assignment was encrypted by OpenAI and cannot be sent to another provider. "
                "Start a new delegation through Model Deck's plaintext agent-message bridge.")
