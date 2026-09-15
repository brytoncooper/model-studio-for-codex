"""Deterministic conversion between Codex and engine function tool schemas."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ToolConversionError(ValueError):
    """Codex advertised a tool the serial function bridge cannot preserve."""


def convert_tools(tools: Any) -> tuple[list[dict[str, Any]], dict[str, tuple[str | None, str]]]:
    if tools is None:
        return [], {}
    if not isinstance(tools, list):
        raise ToolConversionError("tools must be a list")
    converted: list[dict[str, Any]] = []
    aliases: dict[str, tuple[str | None, str]] = {}
    def append_function(function: dict[str, Any], namespace: str | None) -> None:
        if namespace is not None and not isinstance(namespace, str):
            raise ToolConversionError("function namespace is invalid")
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ToolConversionError("function name is required")
        normalized_namespace = None if namespace in (None, "functions") else namespace
        alias = name if normalized_namespace is None else f"{normalized_namespace}__{name}"
        if len(alias) > 128:
            raise ToolConversionError("flattened function name is too long")
        if alias in aliases:
            raise ToolConversionError("function tools must be unique")
        input_schema = function.get("parameters", {})
        if not isinstance(input_schema, dict):
            raise ToolConversionError("function parameters must be an object")
        aliases[alias] = (normalized_namespace, name)
        engine_tool: dict[str, Any] = {
            "name": alias,
            "input_schema": input_schema,
            "host_execution_required": True,
        }
        description = function.get("description")
        if description is not None:
            if not isinstance(description, str):
                raise ToolConversionError("function description must be text")
            engine_tool["description"] = description[:4096]
        converted.append(engine_tool)

    for tool in tools:
        if not isinstance(tool, dict):
            raise ToolConversionError("tool must be an object")
        kind = tool.get("type")
        if kind == "web_search":
            # Codex advertises its provider-native search capability even when
            # the active sandbox disables network access. V2 providers cannot
            # execute that built-in; omit it while retaining host-run functions.
            continue
        if kind == "function":
            append_function(tool, tool.get("namespace"))
            continue
        if kind != "namespace":
            raise ToolConversionError("only function tools are supported")
        namespace = tool.get("name", tool.get("namespace"))
        nested_tools = tool.get("tools")
        if not isinstance(namespace, str) or not namespace or not isinstance(nested_tools, list):
            raise ToolConversionError("function namespace is invalid")
        for nested in nested_tools:
            if not isinstance(nested, dict) or nested.get("type") != "function":
                raise ToolConversionError("namespace tools must be functions")
            append_function(nested, namespace)
    return converted, aliases


def restore_tool_identity(alias: str, aliases: Mapping[str, tuple[str | None, str]]) -> tuple[str | None, str]:
    identity = aliases.get(alias)
    if identity is None:
        raise ToolConversionError("engine requested an unknown function tool")
    return identity


__all__ = ["ToolConversionError", "convert_tools", "restore_tool_identity"]
