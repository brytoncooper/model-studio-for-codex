"""Bounded offline validation of host-advertised tool input schemas.

The returned object is detached from its input. No URI resolver or network
retrieval is installed; only local fragment references/base IDs are accepted.
"""
from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import unquote

from jsonschema import Draft202012Validator, SchemaError


def _copy_json(value: Any, depth: int = 0) -> Any:
    if depth > 64:
        raise ValueError("tool schema exceeds depth bound")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, str) and len(value) <= 1048576:
        return value
    if isinstance(value, list) and len(value) <= 4096:
        return [_copy_json(item, depth + 1) for item in value]
    if isinstance(value, dict) and len(value) <= 1024 and all(isinstance(key, str) for key in value):
        return {key: _copy_json(item, depth + 1) for key, item in value.items()}
    raise ValueError("tool schema must be bounded finite JSON")



def _local_pointer_target(root: dict[str, Any], reference: str) -> Any:
    fragment = unquote(reference[1:])
    if not fragment:
        return root
    if not fragment.startswith("/"):
        # Anchors in schema locations are visited by the normal schema walk.
        return None
    node: Any = root
    for escaped in fragment[1:].split("/"):
        token = escaped.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isascii() and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return None
    return node


def _reject_external_schema_references(root: dict[str, Any]) -> None:
    """Inspect schema positions, leaving literal JSON annotations untouched."""
    pending: list[Any] = [root]
    visited: set[int] = set()
    schema_maps = ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas")
    schema_arrays = ("allOf", "anyOf", "oneOf", "prefixItems")
    schema_values = ("items", "additionalProperties", "unevaluatedProperties", "unevaluatedItems",
                     "contains", "propertyNames", "not", "if", "then", "else", "contentSchema")
    while pending:
        node = pending.pop()
        if not isinstance(node, dict) or id(node) in visited:
            continue
        visited.add(id(node))
        for key in ("$ref", "$dynamicRef", "$recursiveRef", "$id"):
            if key not in node:
                continue
            reference = node[key]
            if not isinstance(reference, str) or not reference.startswith("#"):
                raise ValueError("tool schema external references are unsupported")
            if key != "$id":
                # A local pointer may deliberately use annotation data as a schema.
                # That target is then active and must obey the reference policy.
                pending.append(_local_pointer_target(root, reference))
        for key in schema_maps:
            children = node.get(key)
            if isinstance(children, dict):
                pending.extend(children.values())
        for key in schema_arrays:
            children = node.get(key)
            if isinstance(children, list):
                pending.extend(children)
        for key in schema_values:
            pending.append(node.get(key))


def validate_tool_input_schema(schema: Any) -> dict[str, Any]:
    """Return detached finite Draft 2020-12 object schema, or raise ValueError."""
    if not isinstance(schema, dict):
        raise ValueError("tool input schema must be an object")
    try:
        encoded = json.dumps(schema, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("tool schema must be finite UTF-8 JSON") from None
    if len(encoded) > 1048576:
        raise ValueError("tool schema exceeds byte bound")
    detached = _copy_json(schema)
    try:
        Draft202012Validator.check_schema(detached)
    except SchemaError:
        raise ValueError("invalid tool input JSON Schema") from None
    _reject_external_schema_references(detached)
    return detached
