"""Deterministic conversion between Codex tools and engine function tools."""
from __future__ import annotations
from typing import Any

class ToolConversionError(ValueError):
    pass

def convert_tools(tools: Any) -> tuple[list[dict[str, Any]], dict[str, tuple[str|None, str]]]:
    if tools is None: return [], {}
    if type(tools) is not list: raise ToolConversionError("tools must be a list")
    result=[]; aliases={}; seen=set()
    for tool in tools:
        if type(tool) is not dict: raise ToolConversionError("tool must be an object")
        kind=tool.get("type")
        namespace=tool.get("namespace")
        fn=tool.get("function") if kind == "namespace" else tool
        if kind not in ("function", "namespace") or type(fn) is not dict:
            raise ToolConversionError("only function tools are supported")
        name=fn.get("name")
        if not isinstance(name,str) or not name: raise ToolConversionError("function name required")
        if namespace is None: namespace=fn.get("namespace")
        if namespace is not None and (not isinstance(namespace,str) or not namespace):
            raise ToolConversionError("invalid namespace")
        alias=name if namespace in (None,"functions") else f"{namespace}__{name}"
        if alias in seen: raise ToolConversionError("duplicate function tool")
        seen.add(alias); aliases[alias]=(None if namespace in (None,"functions") else namespace,name)
        converted={"type":"function","name":alias}
        for key in ("description","parameters","strict"):
            if key in fn: converted[key]=fn[key]
        result.append(converted)
    return result, aliases

def restore_function_call(item: dict[str,Any], aliases: dict[str,tuple[str|None,str]]) -> dict[str,Any]:
    if item.get("type") != "function_call": return item
    name=item.get("name"); identity=aliases.get(name)
    if identity is None: raise ToolConversionError("unknown function call")
    ns, original=identity; out=dict(item); out["name"]=original
    if ns: out["namespace"]=ns
    return out
