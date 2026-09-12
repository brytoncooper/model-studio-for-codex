"""Offline validation and detached wire encoding of advertised function tools."""
from __future__ import annotations

import json
from typing import Any
from model_deck_contracts import validate_tool_input_schema
from .ports import ToolDefinition


def parse_tool_definitions(value: Any) -> tuple[ToolDefinition, ...]:
    if not isinstance(value, list) or len(value) > 128:
        raise ValueError("tools must be an array of at most 128 definitions")
    try:
        encoded = json.dumps(value, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("tool definitions must be finite UTF-8 JSON") from None
    if len(encoded) > 1048576:
        raise ValueError("tool definitions exceed byte bound")
    definitions = []
    names = set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {"name", "description", "input_schema", "host_execution_required"}:
            raise ValueError("invalid tool definition fields")
        name = item.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 128 or name in names:
            raise ValueError("tool names must be nonempty bounded and unique")
        names.add(name)
        description = item.get("description")
        if "description" in item and (not isinstance(description, str) or len(description) > 4096):
            raise ValueError("invalid tool description")
        if type(item.get("host_execution_required")) is not bool:
            raise ValueError("host execution requirement must be boolean")
        schema = validate_tool_input_schema(item.get("input_schema"))
        definitions.append(ToolDefinition(name, schema, item["host_execution_required"], description))
    return tuple(definitions)


def tool_definitions_to_wire(tools: tuple[ToolDefinition, ...]) -> list[dict[str, Any]]:
    result = []
    for tool in tools:
        item = {"name": tool.name, "input_schema": validate_tool_input_schema(tool.input_schema),
                "host_execution_required": tool.host_execution_required}
        if tool.description is not None:
            item["description"] = tool.description
        result.append(item)
    return result
