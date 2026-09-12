from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

from model_deck_contracts.paths import schemas_root


class SchemaValidationError(ValueError):
    pass


_MAX_DEPTH = 64
_MAX_NODES = 200_000

_REGISTRY: Registry | None = None


def _bundle_root() -> Path:
    return schemas_root().resolve()


def _confine_path(path: Path) -> Path:
    resolved = path.resolve()
    root = _bundle_root()
    if not resolved.is_relative_to(root):
        raise SchemaValidationError(f"schema path outside bundle: {path}")
    return resolved


def _normalize_schema_uri(uri: str) -> str:
    uri = uri.replace("\\", "/")
    if uri.startswith("file://"):
        raise SchemaValidationError("file:// schema URIs are not allowed")
    if uri.startswith("http://") or uri.startswith("https://"):
        raise SchemaValidationError(f"remote schema URI not allowed: {uri}")
    if not uri.startswith("contracts/"):
        uri = f"contracts/{uri.lstrip('/')}"
    return uri


def normalize_schema_ref(schema_ref: str) -> str:
    document, separator, fragment = schema_ref.partition("#")
    uri = _normalize_schema_uri(document)
    if not separator:
        return uri
    if not fragment:
        return uri
    if not fragment.startswith("/"):
        fragment = f"/{fragment}"
    return f"{uri}#{fragment}"


def _uri_to_path(uri: str) -> Path:
    rel = _normalize_schema_uri(uri)
    return _confine_path(_bundle_root() / rel)


def _build_registry() -> Registry:
    resources: dict[str, Resource] = {}
    contracts = _bundle_root() / "contracts"
    if not contracts.is_dir():
        raise SchemaValidationError(
            "bundled contracts schemas are missing; run scripts/generate_contracts.py"
        )
    for path in sorted(contracts.rglob("*.schema.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(_bundle_root())).replace("\\", "/")
        schema_id = _normalize_schema_uri(doc.get("$id") or rel)
        resource = Resource.from_contents(doc)
        resources[schema_id] = resource
        resources[rel] = resource

    def retrieve(uri: str) -> Resource:
        key = _normalize_schema_uri(uri)
        if key in resources:
            return resources[key]
        raise NoSuchResource(ref=uri)

    registry: Registry = Registry(retrieve=retrieve)
    return registry.with_resources(resources.items())


def _get_registry() -> Registry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def _walk_bounds(value: Any, depth: int = 0, counter: list[int] | None = None) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > _MAX_NODES:
        raise SchemaValidationError("instance exceeds maximum node count")
    if depth > _MAX_DEPTH:
        raise SchemaValidationError("instance exceeds maximum depth")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise SchemaValidationError("non-finite number")
    if isinstance(value, dict):
        for item in value.values():
            _walk_bounds(item, depth + 1, counter)
        return
    if isinstance(value, list):
        for item in value:
            _walk_bounds(item, depth + 1, counter)


def load_schema(relative: str) -> dict:
    rel = _normalize_schema_uri(relative)
    path = _uri_to_path(rel)
    return json.loads(path.read_text(encoding="utf-8"))


def validate_schema_ref(schema_ref: str, instance: Any) -> None:
    _walk_bounds(instance)
    registry = _get_registry()
    ref_uri = normalize_schema_ref(schema_ref)
    validator = Draft202012Validator(
        {"$ref": ref_uri},
        registry=registry,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    try:
        validator.validate(instance)
    except ValidationError as exc:
        raise SchemaValidationError(exc.message) from exc


def validate_instance(
    schema: dict,
    instance: Any,
    *,
    schema_ref: str | None = None,
    base_uri: str | None = None,
) -> None:
    if schema_ref:
        validate_schema_ref(schema_ref, instance)
        return
    if base_uri:
        document_uri = _normalize_schema_uri(base_uri)
        if "$id" not in schema:
            schema = {"$schema": schema.get("$schema", "https://json-schema.org/draft/2020-12/schema"), **schema, "$id": document_uri}
    _walk_bounds(instance)
    registry = _get_registry()
    validator = Draft202012Validator(
        schema,
        registry=registry,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    try:
        validator.validate(instance)
    except ValidationError as exc:
        raise SchemaValidationError(exc.message) from exc


def reset_registry_cache() -> None:
    global _REGISTRY
    _REGISTRY = None
