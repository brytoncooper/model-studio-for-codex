"""Pure, schema-checked Codex TOML editing with caller-supplied descriptors.

This module never discovers or writes files. The host adapter supplies exact
bytes and descriptor identities; the engine supplies authorization and tokens.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import tomlkit
from tomlkit.exceptions import TOMLKitError
from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

RAW_TOML_UTF8_LIMIT = 262144
DIFF_UTF8_LIMIT = 131072
FRAME_UTF8_LIMIT = 1048576
MAX_DESCRIPTORS = 256
MAX_DIAGNOSTICS = 32
MAX_CHANGED_IDS = 64
MAX_UNREPRESENTED = 256
MAX_DEPTH = 4
HASH_PREFIX = "sha256:"
SHAPES = "contracts/common/host_settings.schema.json#/definitions/"
METHODS = "contracts/engine.v1/methods/hosts.settings."


class DocumentError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid", field_id: str | None = None,
                 line: int | None = None, column: int | None = None) -> None:
        super().__init__(message)
        self.code, self.field_id = code, field_id
        self.line, self.column = line, column


class ProtectedDeniedError(PermissionError):
    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.path = path


class SourceTooLargeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EntrySpec:
    """An explicitly identified entry; child paths are absolute document paths."""
    entry_id: str
    label: str
    fields: tuple[FieldSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class FieldSpec:
    field_id: str
    label: str
    type: str
    toml_path: tuple[str | int, ...]
    sensitivity: str = "public"
    editability: str = "editable"
    value_state: str = "explicit"
    application_effect: str = "future_session"
    description: str | None = None
    constraints: Mapping[str, Any] | None = None
    default: Any = None
    has_default: bool = False
    entries: tuple[EntrySpec, ...] = ()


@dataclass(frozen=True, slots=True)
class SectionSpec:
    section_id: str
    title: str
    field_ids: tuple[str, ...]
    description: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentContext:
    host_id: str
    document_id: str
    document_revision: str
    exists: bool
    target: Mapping[str, Any]
    schema_profile: Mapping[str, Any]
    precedence: Sequence[Mapping[str, Any]]
    context_revision: str
    inherited_values: Mapping[str, Any] = field(default_factory=dict)
    protected_paths: frozenset[str] = frozenset()
    support_level: str = "supported"


def sha256_hex(data: bytes) -> str:
    return HASH_PREFIX + hashlib.sha256(data).hexdigest()


def _schema(ref: str, value: Any) -> None:
    try:
        validate_schema_ref(ref, value)
    except SchemaValidationError:
        raise DocumentError("settings shape rejected", code="envelope") from None


def _bounded_json(value: Any) -> None:
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise DocumentError("settings result is not finite JSON", code="internal") from None
    if size > FRAME_UTF8_LIMIT - 1024:
        raise SourceTooLargeError("settings result exceeds frame bound")


def _check_source_bytes(source: bytes) -> None:
    if not isinstance(source, bytes):
        raise DocumentError("source must be bytes", code="envelope")
    if len(source) > RAW_TOML_UTF8_LIMIT:
        raise SourceTooLargeError("settings source exceeds size bound")


def _parse(source: bytes) -> Any:
    _check_source_bytes(source)
    try:
        text = source.decode("utf-8")
    except UnicodeError:
        raise DocumentError("source is not valid UTF-8 TOML", code="syntax") from None
    try:
        return tomlkit.parse(text)
    except TOMLKitError as exc:
        line = getattr(exc, "line", None)
        column = getattr(exc, "col", None)
        raise DocumentError("invalid TOML syntax", code="syntax",
                            line=line if isinstance(line, int) and line > 0 else None,
                            column=column + 1 if type(column) is int and column >= 0 else None) from None
    except (ValueError, RecursionError):
        raise DocumentError("invalid TOML syntax", code="syntax") from None


def _dotted(path: tuple[str | int, ...]) -> str:
    return ".".join(map(str, path))


def _get_path(document: Any, path: tuple[str | int, ...]) -> tuple[bool, Any]:
    node = document
    for part in path:
        if isinstance(part, str) and isinstance(node, Mapping) and part in node:
            node = node[part]
        elif type(part) is int and isinstance(node, list) and 0 <= part < len(node):
            node = node[part]
        else:
            return False, None
    return True, node


def _plain(value: Any) -> Any:
    if hasattr(value, "unwrap"):
        return value.unwrap()
    return copy.deepcopy(value)


def _set_path(document: Any, path: tuple[str | int, ...], value: Any, *, unset: bool) -> None:
    present, previous = _get_path(document, path)
    if unset and not present:
        return
    if not unset and present and _plain(previous) == value and type(_plain(previous)) is type(value):
        return
    node = document
    for part in path[:-1]:
        if isinstance(part, str) and isinstance(node, Mapping):
            if part not in node:
                node[part] = tomlkit.table()
            node = node[part]
        elif type(part) is int and isinstance(node, list) and 0 <= part < len(node):
            node = node[part]
        else:
            raise DocumentError("field path cannot be edited", code="invalid")
    final = path[-1]
    if isinstance(final, str) and isinstance(node, Mapping):
        if unset:
            del node[final]
        else:
            node[final] = value
    else:
        # Array membership/identity belongs to the injected schema owner.
        raise DocumentError("entry membership edits unsupported", code="unsupported")


def _field_bindings(sections: Sequence[SectionSpec], specs: Sequence[FieldSpec]):
    """Validate identities once and enumerate all injected fields with entry scope."""
    if len(sections) > 64 or len(specs) > MAX_DESCRIPTORS:
        raise SourceTooLargeError("descriptor bound exceeded")
    by_id = {spec.field_id: spec for spec in specs}
    if len(by_id) != len(specs):
        raise DocumentError("duplicate field identity", code="envelope")
    bindings: dict[tuple[str, str | None], FieldSpec] = {}
    seen_paths: set[tuple[str | int, ...]] = set()
    entry_count = 0
    visible_fields: list[FieldSpec] = []

    def add(spec: FieldSpec, entry_id: str | None, depth: int, parent_path=(), *, hidden=False) -> None:
        nonlocal entry_count
        key = (spec.field_id, entry_id)
        if key in bindings or spec.toml_path in seen_paths:
            raise DocumentError("duplicate field target", code="envelope")
        if not spec.toml_path or any(not ((isinstance(p, str) and p) or (type(p) is int and p >= 0)) for p in spec.toml_path):
            raise DocumentError("invalid descriptor path", code="envelope")
        if parent_path and spec.toml_path[:len(parent_path)] != parent_path:
            raise DocumentError("entry field outside container", code="envelope")
        bindings[key] = spec
        if not hidden:
            visible_fields.append(spec)
        seen_paths.add(spec.toml_path)
        if len(bindings) > MAX_DESCRIPTORS:
            raise SourceTooLargeError("descriptor bound exceeded")
        if spec.type != "entries" and spec.entries:
            raise DocumentError("non-container field has entries", code="envelope")
        if spec.type == "entries":
            if depth > MAX_DEPTH:
                raise SourceTooLargeError("entry depth bound exceeded")
            seen_entries = set()
            for entry in spec.entries:
                if entry.entry_id in seen_entries:
                    raise DocumentError("duplicate entry identity", code="envelope")
                seen_entries.add(entry.entry_id)
                entry_count += 1
                if entry_count > 256:
                    raise SourceTooLargeError("entry count bound exceeded")
                _schema(SHAPES + "entry_group", {"entry_id": entry.entry_id, "label": entry.label, "fields": []})
                for child in entry.fields:
                    add(child, entry.entry_id, depth + 1, spec.toml_path,
                        hidden=hidden or spec.sensitivity == "secret")

    seen_sections = set()
    for section in sections:
        if section.section_id in seen_sections:
            raise DocumentError("duplicate section identity", code="envelope")
        seen_sections.add(section.section_id)
        for fid in section.field_ids:
            if fid not in by_id:
                raise DocumentError("unknown section field", code="envelope")
            add(by_id[fid], None, 1)
    if {spec.field_id for spec in specs} != {fid for fid, entry in bindings if entry is None}:
        raise DocumentError("descriptor has no section", code="envelope")
    secret_paths = [spec.toml_path for spec in bindings.values() if spec.sensitivity == "secret"]
    for spec in visible_fields:
        if any(spec.toml_path != path and spec.toml_path[:len(path)] == path for path in secret_paths):
            raise DocumentError("secret child cannot be a separately rendered field", code="envelope")
    return by_id, bindings


def _value_error(spec: FieldSpec, value: Any) -> str | None:
    kind = spec.type
    valid = {
        "boolean": type(value) is bool,
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "string": isinstance(value, str) and len(value) <= 2048,
        "enum": type(value) in (bool, int, float, str) and (not isinstance(value, str) or len(value) <= 2048),
        "string_list": isinstance(value, list) and len(value) <= 256 and all(isinstance(v, str) and len(v) <= 2048 for v in value),
        "entries": isinstance(value, (dict, list)),
    }.get(kind, False)
    if not valid or (isinstance(value, float) and not math.isfinite(value)):
        return "value does not match field type"
    constraints = spec.constraints or {}
    if type(value) in (int, float):
        if "minimum" in constraints and value < constraints["minimum"]:
            return "value below minimum"
        if "maximum" in constraints and value > constraints["maximum"]:
            return "value above maximum"
    if isinstance(value, (str, list)):
        if len(value) < constraints.get("min_length", 0):
            return "value below minimum length"
        if "max_length" in constraints and len(value) > constraints["max_length"]:
            return "value above maximum length"
        if isinstance(value, list) and len(value) > constraints.get("max_items", 256):
            return "value above item bound"
    if kind == "enum" and "choices" in constraints:
        if not any(type(value) is type(choice["value"]) and value == choice["value"] for choice in constraints["choices"]):
            return "value not in allowed choices"
    return None


def _descriptor(spec: FieldSpec, document: Any, context: DocumentContext) -> dict[str, Any]:
    present, item = _get_path(document, spec.toml_path)
    descriptor = {"field_id": spec.field_id, "label": spec.label, "key": _dotted(spec.toml_path),
                  "type": spec.type, "value_state": spec.value_state if present else "unset",
                  "editability": spec.editability, "application_effect": spec.application_effect,
                  "sensitivity": spec.sensitivity}
    if spec.description is not None:
        descriptor["description"] = spec.description
    if spec.constraints is not None:
        _schema(SHAPES + "constraints", dict(spec.constraints))
        if spec.sensitivity != "secret":
            descriptor["constraints"] = copy.deepcopy(dict(spec.constraints))
    if spec.sensitivity == "secret":
        descriptor["configured"] = present or spec.field_id in context.inherited_values
    elif spec.type == "entries":
        descriptor["entries"] = [{"entry_id": entry.entry_id, "label": entry.label,
                                  "fields": [_descriptor(child, document, context) for child in entry.fields]}
                                 for entry in spec.entries]
    else:
        if present:
            value = _plain(item)
            if _value_error(spec, value) is None:
                descriptor["value"] = copy.deepcopy(value)
            else:
                descriptor["value_state"] = "unknown"
        if spec.has_default and _value_error(spec, spec.default) is None:
            descriptor["default"] = copy.deepcopy(spec.default)
        if spec.field_id in context.inherited_values:
            effective = context.inherited_values[spec.field_id]
            if _value_error(spec, effective) is None:
                descriptor["effective"] = copy.deepcopy(effective)
                if not present:
                    descriptor["value_state"] = "inherited"
        elif "value" in descriptor:
            descriptor["effective"] = copy.deepcopy(descriptor["value"])
    _schema(SHAPES + "field_descriptor", descriptor)
    return descriptor


def _leaf_paths(document: Any, prefix=()):
    if isinstance(document, Mapping):
        for key, value in document.items():
            yield from _leaf_paths(value, prefix + (key,))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            yield from _leaf_paths(value, prefix + (index,))
    else:
        yield prefix


def _structured(document: Any, context: DocumentContext, sections, specs) -> dict[str, Any]:
    by_id, bindings = _field_bindings(sections, specs)
    output = []
    for section in sections:
        entry = {"section_id": section.section_id, "title": section.title,
                 "fields": [_descriptor(by_id[fid], document, context) for fid in section.field_ids]}
        if section.description is not None:
            entry["description"] = section.description
        output.append(entry)
    covered = {spec.toml_path for spec in bindings.values()}
    secret = [spec.toml_path for spec in bindings.values() if spec.sensitivity == "secret"]
    unrepresented = sorted({_dotted(path) for path in _leaf_paths(document)
                            if path not in covered and not any(path[:len(p)] == p for p in secret)})
    if any(len(path) > 2048 for path in unrepresented):
        raise SourceTooLargeError("unrepresented path bound exceeded")
    return {"sections": output, "unrepresented_paths": unrepresented[:MAX_UNREPRESENTED]}


def _check_context(source: bytes, context: DocumentContext) -> None:
    expected = sha256_hex(source) if context.exists else "absent"
    if not context.exists and source:
        raise DocumentError("missing document has source bytes", code="conflict")
    if context.document_revision != expected:
        raise DocumentError("source revision mismatch", code="conflict")
    if context.support_level != context.schema_profile.get("support_level"):
        raise DocumentError("schema support context mismatch", code="conflict")


def read_snapshot(source: bytes, ctx: DocumentContext, sections: Sequence[SectionSpec],
                  specs: Sequence[FieldSpec]) -> dict[str, Any]:
    document = _parse(source)
    _check_context(source, ctx)
    snapshot = {"host_id": ctx.host_id, "document_id": ctx.document_id,
                "document_revision": ctx.document_revision, "exists": ctx.exists,
                "target": copy.deepcopy(dict(ctx.target)), "schema_profile": copy.deepcopy(dict(ctx.schema_profile)),
                "precedence": copy.deepcopy(list(ctx.precedence)), "raw_toml": source.decode("utf-8"),
                "structured": _structured(document, ctx, sections, specs), "context_revision": ctx.context_revision}
    _schema(METHODS + "read.result.schema.json", {"snapshot": snapshot})
    _bounded_json({"snapshot": snapshot})
    return snapshot


def _protected_changed(base: Any, candidate: Any, context: DocumentContext, bindings) -> bool:
    protected = {spec.toml_path for spec in bindings.values() if spec.editability != "editable"}
    protected.update(tuple(path.split(".")) for path in context.protected_paths)
    for path in protected:
        before, old = _get_path(base, path)
        after, new = _get_path(candidate, path)
        if before != after:
            return True
        if before:
            # Include node spelling/trivia: a protected subtree is not ours to reformat.
            old_text = old.as_string() if hasattr(old, "as_string") else repr(old)
            new_text = new.as_string() if hasattr(new, "as_string") else repr(new)
            if _plain(old) != _plain(new) or old_text != new_text:
                return True
            if repr(getattr(old, "trivia", None)) != repr(getattr(new, "trivia", None)):
                return True
    return False


def _diagnostic(message: str, spec: FieldSpec | None = None, error: DocumentError | None = None):
    result = {"severity": "error", "code": error.code if error else "invalid_value", "message": message}
    if spec is not None:
        result["field_id"] = spec.field_id
    if error is not None:
        if error.line is not None:
            result["line"] = error.line
        if error.column is not None:
            result["column"] = error.column
    return result


def validate_candidate(source: bytes, ctx: DocumentContext, sections: Sequence[SectionSpec],
                       specs: Sequence[FieldSpec], draft: Mapping[str, Any]) -> dict[str, Any]:
    base = _parse(source)
    _check_context(source, ctx)
    _, bindings = _field_bindings(sections, specs)
    # Validate descriptor metadata even if the document contains no value.
    _structured(base, ctx, sections, specs)
    _schema(SHAPES + "draft", draft)
    level = "schema" if ctx.support_level == "supported" else "toml_only"
    result = {"valid": False, "validation_level": level, "diagnostics": [], "context_revision": ctx.context_revision}
    if draft["kind"] == "raw":
        try:
            candidate_bytes = draft["raw_toml"].encode("utf-8")
        except UnicodeError:
            raise DocumentError("source is not valid UTF-8 TOML", code="syntax") from None
        try:
            candidate = _parse(candidate_bytes)
        except DocumentError as error:
            result["diagnostics"] = [_diagnostic(str(error), error=error)]
            _schema(METHODS + "validate.result.schema.json", result)
            return result
    else:
        if level == "toml_only":
            raise DocumentError("structured edits unsupported for toml_only profile", code="unsupported")
        candidate = copy.deepcopy(base)
        seen_paths = []
        for change in draft["changes"]:
            spec = bindings.get((change["field_id"], change.get("entry_id")))
            if spec is None:
                raise DocumentError("unknown field or entry identity", code="envelope")
            path = spec.toml_path
            if any(path[:len(old)] == old or old[:len(path)] == path for old in seen_paths):
                raise DocumentError("conflicting field targets", code="envelope")
            seen_paths.append(path)
            if spec.editability != "editable":
                raise ProtectedDeniedError("field is not editable", path=_dotted(path))
            if spec.type == "entries":
                raise DocumentError("edit an identified child field instead of its container", code="unsupported")
            if change["operation"] == "set":
                error = _value_error(spec, change["value"])
                if error:
                    result["diagnostics"].append(_diagnostic(error, spec))
                    continue
            try:
                _set_path(candidate, path, copy.deepcopy(change.get("value")), unset=change["operation"] == "unset")
            except (TypeError, ValueError, KeyError, IndexError):
                raise DocumentError("field edit rejected", code="invalid") from None
        candidate_bytes = tomlkit.dumps(candidate).encode("utf-8")
        _check_source_bytes(candidate_bytes)
    if _protected_changed(base, candidate, ctx, bindings):
        raise ProtectedDeniedError("protected settings changed")
    for spec in bindings.values():
        present, item = _get_path(candidate, spec.toml_path)
        if present:
            error = _value_error(spec, _plain(item))
            if error:
                result["diagnostics"].append(_diagnostic(error, spec))
    result["diagnostics"] = result["diagnostics"][:MAX_DIAGNOSTICS]
    result.update(valid=not result["diagnostics"], candidate_content_hash=sha256_hex(candidate_bytes),
                  candidate_raw_toml=candidate_bytes.decode("utf-8"),
                  candidate_structured=_structured(candidate, ctx, sections, specs))
    _schema(METHODS + "validate.result.schema.json", result)
    _bounded_json(result)
    return result


def preview_candidate(source: bytes, ctx: DocumentContext, sections: Sequence[SectionSpec],
                      specs: Sequence[FieldSpec], draft: Mapping[str, Any]) -> dict[str, Any]:
    result = validate_candidate(source, ctx, sections, specs, draft)
    if not result["valid"]:
        result["preview"] = None
        _schema(METHODS + "preview.result.schema.json", result)
        return result
    _, bindings = _field_bindings(sections, specs)
    before, after = _parse(source), _parse(result["candidate_raw_toml"].encode("utf-8"))
    changed_ids = []
    effects = set()
    for spec in bindings.values():
        old_present, old = _get_path(before, spec.toml_path)
        new_present, new = _get_path(after, spec.toml_path)
        if old_present != new_present or (old_present and _plain(old) != _plain(new)):
            if spec.field_id not in changed_ids:
                changed_ids.append(spec.field_id)
            effects.add(spec.application_effect)
    changed = source.decode("utf-8") != result["candidate_raw_toml"]
    if changed and any(spec.sensitivity == "secret" for spec in bindings.values()):
        # Suppress the complete display diff rather than guessing TOML value spans.
        # This includes multiline secrets and unchanged secret context lines.
        diff, truncated = "<redacted>\n", True
    else:
        diff = "".join(difflib.unified_diff(source.decode("utf-8").splitlines(keepends=True),
                                          result["candidate_raw_toml"].splitlines(keepends=True),
                                          fromfile="base", tofile="candidate"))
        encoded = diff.encode("utf-8")
        truncated = len(encoded) > DIFF_UTF8_LIMIT
        diff = encoded[:DIFF_UTF8_LIMIT].decode("utf-8", errors="ignore")
    preview = {"base_content_hash": ctx.document_revision, "candidate_content_hash": result["candidate_content_hash"],
               "changed": changed, "diff": diff, "diff_truncated": truncated,
               "changed_field_ids": changed_ids[:MAX_CHANGED_IDS], "application_effects": sorted(effects),
               "protected_projection_changes": False}
    result["preview"] = preview
    # The engine issues the real token. Validate a copy with a placeholder to
    # prove every other byte of the adapter result has the frozen wire shape.
    wire = copy.deepcopy(result)
    wire["preview"]["preview_id"] = "adapter-shape-check"
    _schema(METHODS + "preview.result.schema.json", wire)
    _bounded_json(wire)
    return result


def assert_save_allowed(source: bytes, candidate_raw_toml: str, candidate_content_hash: str) -> str:
    """Hash/size guard only; adapter must repeat context/protection validation."""
    _check_source_bytes(source)
    raw = candidate_raw_toml.encode("utf-8")
    _check_source_bytes(raw)
    actual = sha256_hex(raw)
    if actual != candidate_content_hash:
        raise DocumentError("candidate hash mismatch", code="conflict")
    return actual
