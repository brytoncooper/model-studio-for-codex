"""Bounded in-memory validation for schemas shipped inside one plugin."""

from __future__ import annotations

import copy
import json
import math
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final
from urllib.parse import quote, unquote, urlsplit

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012


MAX_SCHEMA_RESOURCES: Final[int] = 512
MAX_SCHEMA_RESOURCE_BYTES: Final[int] = 1 << 20
MAX_SCHEMA_TOTAL_BYTES: Final[int] = 8 << 20
MAX_JSON_DEPTH: Final[int] = 64
MAX_JSON_NODES: Final[int] = 200_000
MAX_JSON_STRING: Final[int] = 1 << 20
MAX_JSON_ARRAY: Final[int] = 4096
MAX_JSON_OBJECT: Final[int] = 1024
MAX_INSTANCE_BYTES: Final[int] = 1 << 20

_DRAFT_URI = "https://json-schema.org/draft/2020-12/schema"
_INTERNAL_ROOT = "urn:model-deck-plugin-schema:"
_BUNDLE_ERROR = "plugin schema bundle is invalid"
_DATA_ERROR = "plugin schema data is invalid"
_ALLOWED_FORMATS = frozenset({"uuid", "date-time", "uri"})
_ALLOWED_KEYWORDS = frozenset(
    {
        "$id",
        "$ref",
        "$schema",
        "additionalProperties",
        "allOf",
        "const",
        "default",
        "definitions",
        "description",
        "enum",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "not",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
    }
)


class PluginSchemaBundleError(ValueError):
    """The supplied schema resources or a requested reference are invalid."""


class PluginSchemaDataError(ValueError):
    """Operation data is not bounded JSON or does not match its schema."""


class _InternalValidationError(ValueError):
    pass


def _raise_bundle_error() -> None:
    raise PluginSchemaBundleError(_BUNDLE_ERROR)


def _raise_data_error() -> None:
    raise PluginSchemaDataError(_DATA_ERROR)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InternalValidationError
        result[key] = value
    return result


def _reject_non_finite(_: str) -> None:
    raise _InternalValidationError


def _bounded_json_copy(
    value: Any,
    *,
    depth: int = 0,
    nodes: list[int] | None = None,
    active: set[int] | None = None,
) -> Any:
    if nodes is None:
        nodes = [0]
    if active is None:
        active = set()
    nodes[0] += 1
    if nodes[0] > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
        raise _InternalValidationError
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _InternalValidationError
        return value
    if type(value) is str:
        if len(value) > MAX_JSON_STRING:
            raise _InternalValidationError
        failed = False
        try:
            value.encode("utf-8")
        except UnicodeError:
            failed = True
        if failed:
            raise _InternalValidationError
        return value
    if type(value) is list:
        if len(value) > MAX_JSON_ARRAY or id(value) in active:
            raise _InternalValidationError
        active.add(id(value))
        try:
            result = [
                _bounded_json_copy(
                    item, depth=depth + 1, nodes=nodes, active=active
                )
                for item in value
            ]
        finally:
            active.remove(id(value))
        return result
    if type(value) is dict:
        if len(value) > MAX_JSON_OBJECT or id(value) in active:
            raise _InternalValidationError
        active.add(id(value))
        result: dict[str, Any] = {}
        try:
            for key, item in value.items():
                if type(key) is not str:
                    raise _InternalValidationError
                _bounded_json_copy(key, depth=depth + 1, nodes=nodes, active=active)
                result[key] = _bounded_json_copy(
                    item, depth=depth + 1, nodes=nodes, active=active
                )
        finally:
            active.remove(id(value))
        return result
    raise _InternalValidationError


def _freeze_json(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    return value


def _resource_path(value: Any) -> str:
    if type(value) is not str or not value or len(value) > 256:
        raise _InternalValidationError
    if any(marker in value for marker in ("\\", "\x00", "?", "#", "%", ":")):
        raise _InternalValidationError
    if value.startswith("/"):
        raise _InternalValidationError
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _InternalValidationError
    return value


def _parse_resource(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or len(raw) > MAX_SCHEMA_RESOURCE_BYTES:
        raise _InternalValidationError
    document: Any = None
    failed = False
    try:
        text = raw.decode("utf-8")
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeError, ValueError, TypeError, RecursionError, _InternalValidationError):
        failed = True
    if failed or type(document) is not dict:
        raise _InternalValidationError
    return _bounded_json_copy(document)


def _decode_pointer_token(token: str) -> str:
    index = 0
    while index < len(token):
        if token[index] != "~":
            index += 1
            continue
        if index + 1 >= len(token) or token[index + 1] not in ("0", "1"):
            raise _InternalValidationError
        index += 2
    return token.replace("~1", "/").replace("~0", "~")


def _resolve_pointer(document: Any, fragment: str) -> Any:
    failed = False
    try:
        fragment.encode("utf-8")
    except UnicodeError:
        failed = True
    if failed:
        raise _InternalValidationError
    if fragment == "":
        return document
    if not fragment.startswith("/"):
        raise _InternalValidationError
    node = document
    for raw_token in fragment[1:].split("/"):
        token = _decode_pointer_token(raw_token)
        if isinstance(node, Mapping) and token in node:
            node = node[token]
            continue
        if type(node) in (list, tuple) and token.isascii() and token.isdigit():
            index = int(token)
            if index < len(node):
                node = node[index]
                continue
        raise _InternalValidationError
    return node


def _canonical_fragment(fragment: str) -> str:
    decoded: str | None = None
    failed = False
    try:
        for index, character in enumerate(fragment):
            if character == "%" and (
                index + 2 >= len(fragment)
                or any(
                    digit not in "0123456789abcdefABCDEF"
                    for digit in fragment[index + 1 : index + 3]
                )
            ):
                raise _InternalValidationError
        decoded = unquote(fragment, errors="strict")
        decoded.encode("utf-8")
    except (UnicodeError, ValueError, _InternalValidationError):
        failed = True
    if failed or decoded is None:
        raise _InternalValidationError
    if decoded == "":
        return ""
    if not decoded.startswith("/"):
        raise _InternalValidationError
    raw_tokens = decoded[1:].split("/")
    if any(token == "" for token in raw_tokens):
        raise _InternalValidationError
    tokens = [_decode_pointer_token(token) for token in raw_tokens]
    return "/" + "/".join(
        token.replace("~", "~0").replace("/", "~1") for token in tokens
    )


def _resolve_reference(
    reference: Any,
    *,
    base_path: str | None,
    documents: Mapping[str, Any],
) -> tuple[str, str]:
    if type(reference) is not str or not reference or len(reference) > 4096:
        raise _InternalValidationError
    if "\\" in reference or "\x00" in reference:
        raise _InternalValidationError
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc or parsed.query:
        raise _InternalValidationError
    document_part, separator, fragment = reference.partition("#")
    if document_part:
        if "%" in document_part or document_part.startswith("/"):
            raise _InternalValidationError
        parts = document_part.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise _InternalValidationError
        if document_part.startswith("contracts/"):
            path = document_part
        elif base_path is not None:
            path = posixpath.join(posixpath.dirname(base_path), document_part)
        else:
            path = document_part
        path = _resource_path(path)
    elif base_path is not None:
        path = base_path
    else:
        raise _InternalValidationError
    if path not in documents:
        raise _InternalValidationError
    resolved_fragment = _canonical_fragment(fragment if separator else "")
    _resolve_pointer(documents[path], resolved_fragment)
    return path, resolved_fragment


def _internal_reference(path: str, fragment: str = "") -> str:
    reference = _INTERNAL_ROOT + path
    return reference + ("#" + quote(fragment, safe="/~") if fragment else "")


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _collect_schema_locations(
    node: Any,
    *,
    resource_path: str,
    fragment: str = "",
    locations: set[tuple[str, str]],
) -> None:
    if type(node) is bool:
        locations.add((resource_path, fragment))
        return
    if type(node) is not dict:
        raise _InternalValidationError
    locations.add((resource_path, fragment))
    for key in ("properties", "definitions"):
        mapping = node.get(key)
        if type(mapping) is dict:
            for name, child in mapping.items():
                _collect_schema_locations(
                    child,
                    resource_path=resource_path,
                    fragment=f"{fragment}/{key}/{_pointer_token(name)}",
                    locations=locations,
                )
    if "items" in node:
        _collect_schema_locations(
            node["items"],
            resource_path=resource_path,
            fragment=f"{fragment}/items",
            locations=locations,
        )
    for key in ("oneOf", "allOf"):
        values = node.get(key)
        if type(values) is list:
            for index, child in enumerate(values):
                _collect_schema_locations(
                    child,
                    resource_path=resource_path,
                    fragment=f"{fragment}/{key}/{index}",
                    locations=locations,
                )
    if "not" in node:
        _collect_schema_locations(
            node["not"],
            resource_path=resource_path,
            fragment=f"{fragment}/not",
            locations=locations,
        )
    if type(node.get("additionalProperties")) in (dict, bool):
        _collect_schema_locations(
            node["additionalProperties"],
            resource_path=resource_path,
            fragment=f"{fragment}/additionalProperties",
            locations=locations,
        )


def _prepare_schema(
    node: Any,
    *,
    resource_path: str,
    documents: Mapping[str, Any],
    schema_locations: set[tuple[str, str]],
    root: bool = True,
) -> Any:
    if type(node) is bool:
        return node
    if type(node) is not dict:
        raise _InternalValidationError
    if any(key not in _ALLOWED_KEYWORDS for key in node):
        raise _InternalValidationError
    if node.get("additionalProperties") is True:
        raise _InternalValidationError
    if "format" in node and node["format"] not in _ALLOWED_FORMATS:
        raise _InternalValidationError
    if "$schema" in node and node["$schema"] != _DRAFT_URI:
        raise _InternalValidationError
    if "$id" in node:
        if not root or node["$id"] != resource_path:
            raise _InternalValidationError

    prepared = copy.deepcopy(node)
    if "$ref" in node:
        target_path, fragment = _resolve_reference(
            node["$ref"], base_path=resource_path, documents=documents
        )
        if (target_path, fragment) not in schema_locations:
            raise _InternalValidationError
        prepared["$ref"] = _internal_reference(target_path, fragment)
    for key in ("properties", "definitions"):
        mapping = node.get(key)
        if type(mapping) is dict:
            prepared[key] = {
                name: _prepare_schema(
                    child,
                    resource_path=resource_path,
                    documents=documents,
                    schema_locations=schema_locations,
                    root=False,
                )
                for name, child in mapping.items()
            }
    if "items" in node:
        prepared["items"] = _prepare_schema(
            node["items"],
            resource_path=resource_path,
            documents=documents,
            schema_locations=schema_locations,
            root=False,
        )
    for key in ("oneOf", "allOf"):
        values = node.get(key)
        if type(values) is list:
            prepared[key] = [
                _prepare_schema(
                    child,
                    resource_path=resource_path,
                    documents=documents,
                    schema_locations=schema_locations,
                    root=False,
                )
                for child in values
            ]
    if "not" in node:
        prepared["not"] = _prepare_schema(
            node["not"],
            resource_path=resource_path,
            documents=documents,
            schema_locations=schema_locations,
            root=False,
        )
    if type(node.get("additionalProperties")) is dict:
        prepared["additionalProperties"] = _prepare_schema(
            node["additionalProperties"],
            resource_path=resource_path,
            documents=documents,
            schema_locations=schema_locations,
            root=False,
        )
    prepared["$id"] = _internal_reference(resource_path)
    if not root:
        prepared.pop("$id", None)
    return prepared


@dataclass(frozen=True, slots=True)
class PluginSchemaBundle:
    """Detached immutable schema resources for one plugin artifact."""

    resource_paths: tuple[str, ...]
    _documents: Mapping[str, Any] = field(repr=False, compare=False)
    _schema_locations: frozenset[tuple[str, str]] = field(repr=False, compare=False)
    _registry: Registry = field(repr=False, compare=False)

    @classmethod
    def from_resources(cls, resources: Mapping[str, bytes]) -> "PluginSchemaBundle":
        entries: list[tuple[Any, Any]] | None = None
        failed = False
        try:
            entries = list(resources.items()) if isinstance(resources, Mapping) else None
        except Exception:
            failed = True
        if failed or entries is None or len(entries) > MAX_SCHEMA_RESOURCES:
            _raise_bundle_error()

        total_bytes = 0
        documents: dict[str, dict[str, Any]] = {}
        try:
            for raw_path, raw in entries:
                path = _resource_path(raw_path)
                if type(raw) is not bytes:
                    raise _InternalValidationError
                total_bytes += len(raw)
                if total_bytes > MAX_SCHEMA_TOTAL_BYTES:
                    raise _InternalValidationError
                documents[path] = _parse_resource(raw)
        except (UnicodeError, ValueError, _InternalValidationError):
            failed = True
        if failed:
            _raise_bundle_error()

        prepared: dict[str, dict[str, Any]] = {}
        schema_locations: set[tuple[str, str]] = set()
        failed = False
        try:
            for path, document in documents.items():
                Draft202012Validator.check_schema(document)
                _collect_schema_locations(
                    document,
                    resource_path=path,
                    locations=schema_locations,
                )
            for path, document in documents.items():
                prepared[path] = _prepare_schema(
                    document,
                    resource_path=path,
                    documents=documents,
                    schema_locations=schema_locations,
                )
                Draft202012Validator.check_schema(prepared[path])
        except (SchemaError, _InternalValidationError, TypeError, ValueError, RecursionError):
            failed = True
        if failed:
            _raise_bundle_error()

        frozen_prepared = {
            path: _freeze_json(document) for path, document in prepared.items()
        }
        resources_for_registry = [
            (_internal_reference(path), Resource(contents=document, specification=DRAFT202012))
            for path, document in frozen_prepared.items()
        ]
        registry = Registry().with_resources(resources_for_registry)
        frozen_documents = MappingProxyType(
            {path: _freeze_json(document) for path, document in documents.items()}
        )
        return cls(
            tuple(sorted(documents)),
            frozen_documents,
            frozenset(schema_locations),
            registry,
        )

    def check_reference(self, reference: str) -> None:
        failed = False
        try:
            target = _resolve_reference(
                reference, base_path=None, documents=self._documents
            )
            if target not in self._schema_locations:
                raise _InternalValidationError
        except (UnicodeError, ValueError, _InternalValidationError):
            failed = True
        if failed:
            _raise_bundle_error()

    def validate(self, reference: str, instance: Any) -> Any:
        failed_reference = False
        target: tuple[str, str] | None = None
        try:
            target = _resolve_reference(
                reference, base_path=None, documents=self._documents
            )
        except _InternalValidationError:
            failed_reference = True
        if failed_reference or target is None:
            _raise_bundle_error()
        if target not in self._schema_locations:
            _raise_bundle_error()

        detached: Any = None
        failed_data = False
        try:
            detached = _bounded_json_copy(instance)
            encoded = json.dumps(
                detached,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > MAX_INSTANCE_BYTES:
                raise _InternalValidationError
        except (UnicodeError, ValueError, TypeError, RecursionError, _InternalValidationError):
            failed_data = True
        if failed_data:
            _raise_data_error()

        path, fragment = target
        validation_failed = False
        try:
            Draft202012Validator(
                {"$ref": _internal_reference(path, fragment)},
                registry=self._registry,
                format_checker=Draft202012Validator.FORMAT_CHECKER,
            ).validate(detached)
        except (ValidationError, Unresolvable, RecursionError):
            validation_failed = True
        if validation_failed:
            _raise_data_error()
        return detached
