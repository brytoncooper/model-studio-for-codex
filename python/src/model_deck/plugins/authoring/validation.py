"""In-memory validation of a packed plugin archive.

The validator composes four pure helpers:

- :func:`model_deck.plugins.archive_inspection.inspect_archive` rejects
  traversal, symlinks/special entries, encryption, duplicates, parent
  collisions, compression-ratio bombs, CRC/length mismatches and unbounded
  archives before any entry is is touched.
- :func:`model_deck.plugins.manifest_inspection.inspect_manifest` confirms
  the embedded ``manifest.json`` matches the bundled
  ``contracts/plugin.v1/manifest.schema.json`` schema, performs structural
  checks, and compares the manifest's declared API against the caller's
  declared API version.
- :class:`model_deck.plugins.schema_bundle.PluginSchemaBundle` parses and
  detaches the declared operation schema resources shipped inside the
  archive, then confirms every operation ``input_schema`` and
  ``output_schema`` reference resolves inside that bundle.
- :func:`model_deck.plugins.panel_validation.validate_panel_semantics`
  validates the structure of each declared panel resource against the
  ``ui.panel.v1`` JSON schema and then enforces the semantic rules the
  schema deliberately does not cover (depth, node count, unique ids,
  ``text_input`` bindings, params/bindings disjointness, ``panel_id``
  match, and button ``operation_id`` membership).

The default caller API version is ``(1, 0)``, matching the only existing
shipped plugin manifest in the tree. Callers may override either integer
explicitly when a future host version lands.

An archive that declares no operations and no panels remains valid; the
operation-schema and panel checks are skipped when the manifest declares
no such contributions.
"""
from __future__ import annotations

import io
import json
import posixpath
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Iterable

from model_deck.plugins.archive_inspection import (
    ArchiveInspectionResult,
    inspect_archive,
)
from model_deck.plugins.manifest_inspection import (
    InspectionError,
    InspectionResult,
    inspect_entrypoint_path,
    inspect_manifest,
)
from model_deck.plugins.panel_validation import (
    PanelSemanticCode,
    PanelSemanticReport,
    validate_panel_semantics,
)
from model_deck.plugins.schema_bundle import (
    PluginSchemaBundle,
    PluginSchemaBundleError,
)
from model_deck_contracts.validator import (
    SchemaValidationError,
    validate_schema_ref,
)

from .errors import AuthoringError, AuthoringErrorCode


DEFAULT_CALLER_PLUGIN_API_MAJOR: int = 1
"""Default caller plugin API major, matching shipped manifests."""

DEFAULT_CALLER_PLUGIN_API_MINOR: int = 0
"""Default caller plugin API minor, matching shipped manifests."""

_MANIFEST_ENTRY_NAME: str = "manifest.json"
_PANEL_SCHEMA_REF: str = "contracts/ui.panel.v1/tree.schema.json"


@dataclass(frozen=True)
class PanelValidationFinding:
    """Detached per-panel result for one declared panel contribution.

    Attributes:
        panel_id: The manifest contribution id this panel was checked
            against.
        schema_path: The relative archive path declared by the
            manifest contribution.
        ok: ``True`` when both structural and semantic checks passed.
        semantic_report: The semantic validator report; ``None`` only
            when structural validation rejected the document before the
            semantic walk could run.
    """

    panel_id: str
    schema_path: str
    ok: bool
    semantic_report: PanelSemanticReport | None = None


@dataclass(frozen=True)
class ValidationReport:
    """Detached outcome of :func:`validate_project_archive`.

    Attributes:
        archive: Result returned by :func:`inspect_archive`.
        manifest: Result returned by :func:`inspect_manifest`.
        schema_bundle_ok: ``True`` when every declared operation
            ``input_schema`` and ``output_schema`` reference resolved
            inside the supplied bundle. ``None`` when the manifest
            declared no operation schemas.
        panels: Tuple of :class:`PanelValidationFinding`, one per
            declared panel contribution (in manifest order). Empty when
            the manifest declared no panels.
    """

    archive: ArchiveInspectionResult
    manifest: InspectionResult
    schema_bundle_ok: bool | None = None
    panels: tuple[PanelValidationFinding, ...] = ()

    @property
    def ok(self) -> bool:
        if not (self.archive.ok and self.manifest.ok and entrypoint_present(self)):
            return False
        if self.schema_bundle_ok is False:
            return False
        return all(finding.ok for finding in self.panels)


def _decode_manifest_bytes(raw: bytes) -> Mapping[str, Any]:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuthoringError(
            code=AuthoringErrorCode.MANIFEST_READ_FAILED,
            detail=f"manifest.json is not valid JSON: {exc.msg}",
            field=_MANIFEST_ENTRY_NAME,
        ) from exc
    if not isinstance(decoded, Mapping):
        raise AuthoringError(
            code=AuthoringErrorCode.MANIFEST_READ_FAILED,
            detail="manifest.json must decode to a JSON object",
            field=_MANIFEST_ENTRY_NAME,
        )
    return decoded


def _inspect_manifest_for_authoring(
    decoded: Mapping[str, Any],
    *,
    caller_plugin_api_major: int,
    caller_plugin_api_minor: int,
) -> InspectionResult:
    """Inspect a manifest without exposing lower-level inspection errors."""
    try:
        return inspect_manifest(
            decoded,
            caller_plugin_api_major=caller_plugin_api_major,
            caller_plugin_api_minor=caller_plugin_api_minor,
        )
    except InspectionError as exc:
        raise AuthoringError(
            code=AuthoringErrorCode.MANIFEST_READ_FAILED,
            detail=f"manifest failed inspection: {exc.code}",
            field=_MANIFEST_ENTRY_NAME,
        ) from None


def _read_zip_bytes(
    archive_file: zipfile.ZipFile,
    name: str,
    *,
    missing_code: str,
    missing_detail: str,
    read_failed_code: str,
    read_failed_label: str,
) -> bytes:
    try:
        return archive_file.read(name)
    except KeyError as exc:
        raise AuthoringError(
            code=missing_code,
            detail=missing_detail,
            field=name,
        ) from exc
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise AuthoringError(
            code=read_failed_code,
            detail=f"{read_failed_label}: {exc}",
            field=name,
        ) from exc


_MAX_TRANSITIVE_SCHEMA_HOPS: int = 64


def _iter_dollar_ref_paths(node: Any) -> Iterable[str]:
    """Yield every non-empty document path referenced by ``$ref`` keys.

    Fragment-only references (e.g. ``#/definitions/foo``) and
    external/cross-bundle references are ignored on purpose: the
    bundle resolves them on its own; the validator only needs to know
    which in-archive documents to load.
    """
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            ref = current.get("$ref")
            if isinstance(ref, str) and ref:
                document_part = ref.split("#", 1)[0]
                if document_part:
                    yield document_part
            for value in current.values():
                if isinstance(value, (Mapping, list)):
                    stack.append(value)
        elif isinstance(current, list):
            for value in current:
                if isinstance(value, (Mapping, list)):
                    stack.append(value)


def _resolve_ref_path(reference: str, *, base_path: str) -> str | None:
    """Resolve a ``$ref`` document part to an archive-relative path.

    Returns ``None`` when the reference points outside the archive
    (for example, a URL or a fragment-only ref) or when the result
    would traverse outside the archive root.
    """
    if not reference:
        return None
    if any(marker in reference for marker in ("\\", "\x00")):
        return None
    if reference.startswith("/"):
        return None
    if ":" in reference and not reference.startswith("./"):
        # Treat any URL/URI as out-of-bundle.
        return None
    base_dir = posixpath.dirname(base_path)
    if base_dir and not reference.startswith("./") and not reference.startswith("../"):
        joined = posixpath.join(base_dir, reference) if base_dir else reference
    elif reference.startswith("./"):
        joined = posixpath.join(base_dir, reference[2:]) if base_dir else reference[2:]
    else:
        joined = posixpath.normpath(posixpath.join(base_dir, reference)) if base_dir else reference
    parts = joined.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return joined


def _collect_transitive_schema_resources(
    archive_file: zipfile.ZipFile,
    *,
    seed_paths: set[str],
    names: set[str],
    resources: dict[str, bytes],
) -> None:
    """Read schemas transitively referenced via ``$ref`` into ``resources``.

    Each loaded JSON schema may ``$ref`` other schemas shipped in the
    same archive. Those must also be bundled so the
    :class:`PluginSchemaBundle` can resolve the reference graph. The
    walk is bounded by :data:`_MAX_TRANSITIVE_SCHEMA_HOPS`; deeper
    cycles or graphs are rejected with a stable authoring error so the
    caller never sees a half-built bundle.
    """
    frontier = set(seed_paths)
    for _ in range(_MAX_TRANSITIVE_SCHEMA_HOPS):
        if not frontier:
            return
        next_frontier: set[str] = set()
        for path in frontier:
            try:
                document = json.loads(resources[path])
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(document, Mapping):
                continue
            for referenced in _iter_dollar_ref_paths(document):
                resolved = _resolve_ref_path(referenced, base_path=path)
                if resolved is None:
                    continue
                if resolved in resources:
                    continue
                if resolved not in names:
                    continue
                try:
                    payload = archive_file.read(resolved)
                except KeyError:
                    continue
                resources[resolved] = payload
                next_frontier.add(resolved)
        frontier = next_frontier
    if frontier:
        raise AuthoringError(
            code=AuthoringErrorCode.OPERATION_SCHEMA_RESOURCE_INVALID,
            detail=(
                "transitive operation schema $ref graph exceeds "
                f"{_MAX_TRANSITIVE_SCHEMA_HOPS} hops"
            ),
            field="contributes.operations[*].input_schema/output_schema",
        )


def _collect_operation_schema_resources(
    archive_file: zipfile.ZipFile,
    manifest: InspectionResult,
    names: set[str],
) -> tuple[Mapping[str, bytes], tuple[str, ...]]:
    """Read every declared operation schema resource from the archive.

    Returns the mapping passed to :class:`PluginSchemaBundle` together
    with the preserved reference list (operation ``input_schema`` and
    ``output_schema`` strings in manifest order, deduplicated by
    insertion order on first occurrence).
    """
    resources: dict[str, bytes] = {}
    references: list[str] = []
    seen: set[str] = set()
    loaded_paths: set[str] = set()
    for operation in manifest.contributions.operations:
        for reference in (operation.input_schema, operation.output_schema):
            if not isinstance(reference, str) or not reference:
                raise AuthoringError(
                    code=AuthoringErrorCode.OPERATION_SCHEMA_REFERENCE_INVALID,
                    detail="operation declares an empty schema reference",
                    field=f"contributes.operations[{operation.operation_id}]",
                )
            if reference in seen:
                continue
            seen.add(reference)
            references.append(reference)
            resource_path = reference.split("#", 1)[0]
            if not resource_path:
                # Fragment-only reference (e.g. ``#/definitions/foo``).
                # Cannot be resolved against the archive; let
                # :meth:`PluginSchemaBundle.check_reference` raise the
                # precise bundle error so the caller sees
                # ``operation_schema_reference_invalid`` for the
                # unreferenced document part.
                continue
            if resource_path in loaded_paths:
                continue
            if resource_path not in names:
                raise AuthoringError(
                    code=AuthoringErrorCode.OPERATION_SCHEMA_RESOURCE_INVALID,
                    detail="declared operation schema resource is not in the archive",
                    field=reference,
                )
            payload = _read_zip_bytes(
                archive_file,
                resource_path,
                missing_code=AuthoringErrorCode.OPERATION_SCHEMA_RESOURCE_INVALID,
                missing_detail="declared operation schema resource is not in the archive",
                read_failed_code=AuthoringErrorCode.OPERATION_SCHEMA_RESOURCE_INVALID,
                read_failed_label="could not read declared operation schema resource",
            )
            resources[resource_path] = payload
            loaded_paths.add(resource_path)
    _collect_transitive_schema_resources(
        archive_file,
        seed_paths=loaded_paths,
        names=names,
        resources=resources,
    )
    return resources, tuple(references)


def _verify_operation_schema_references(
    manifest: InspectionResult,
    archive_file: zipfile.ZipFile,
    names: set[str],
) -> bool:
    """Return ``True`` when every declared schema reference resolves.

    Returns ``True`` and short-circuits when the manifest declares no
    operations or no schema references. Raises :class:`AuthoringError`
    on the first structural defect so the caller sees a stable code
    instead of a downstream cascade.
    """
    if not manifest.contributions.operations:
        return True
    resources, references = _collect_operation_schema_resources(
        archive_file, manifest, names
    )
    if not references:
        return True
    try:
        bundle = PluginSchemaBundle.from_resources(resources)
    except PluginSchemaBundleError as exc:
        raise AuthoringError(
            code=AuthoringErrorCode.OPERATION_SCHEMA_RESOURCE_INVALID,
            detail="declared operation schema resources do not form a valid bundle",
            field="contributes.operations[*].input_schema/output_schema",
        ) from exc
    for reference in references:
        try:
            bundle.check_reference(reference)
        except PluginSchemaBundleError as exc:
            raise AuthoringError(
                code=AuthoringErrorCode.OPERATION_SCHEMA_REFERENCE_INVALID,
                detail="declared operation schema reference is not in the schema bundle",
                field=reference,
            ) from exc
    return True


def _verify_declared_panels(
    manifest: InspectionResult,
    archive_file: zipfile.ZipFile,
    names: set[str],
) -> tuple[PanelValidationFinding, ...]:
    """Validate every declared panel contribution against its resource.

    Returns one :class:`PanelValidationFinding` per manifest panel in
    declaration order. Structural defects raise :class:`AuthoringError`;
    semantic defects are captured per finding so the report can surface
    every defect at once.
    """
    declared_operation_ids = frozenset(
        operation.operation_id for operation in manifest.contributions.operations
    )
    findings: list[PanelValidationFinding] = []
    for panel in manifest.contributions.panels:
        schema_path = panel.schema
        if schema_path not in names:
            raise AuthoringError(
                code=AuthoringErrorCode.PANEL_RESOURCE_MISSING,
                detail="declared panel resource is not in the archive",
                field=schema_path,
            )
        payload = _read_zip_bytes(
            archive_file,
            schema_path,
            missing_code=AuthoringErrorCode.PANEL_RESOURCE_MISSING,
            missing_detail="declared panel resource is not in the archive",
            read_failed_code=AuthoringErrorCode.PANEL_RESOURCE_NOT_READABLE,
            read_failed_label="could not read declared panel resource",
        )
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AuthoringError(
                code=AuthoringErrorCode.PANEL_RESOURCE_NOT_READABLE,
                detail=f"panel resource is not valid JSON: {exc.msg}",
                field=schema_path,
            ) from exc
        if not isinstance(decoded, Mapping):
            raise AuthoringError(
                code=AuthoringErrorCode.PANEL_RESOURCE_NOT_READABLE,
                detail="panel resource must decode to a JSON object",
                field=schema_path,
            )
        try:
            validate_schema_ref(_PANEL_SCHEMA_REF, decoded)
        except SchemaValidationError as exc:
            raise AuthoringError(
                code=AuthoringErrorCode.PANEL_SCHEMA_INVALID,
                detail="panel resource does not match the ui.panel.v1 tree schema",
                field=schema_path,
            ) from exc
        semantic = validate_panel_semantics(
            decoded,
            declared_panel_id=panel.panel_id,
            declared_operation_ids=declared_operation_ids,
        )
        findings.append(
            PanelValidationFinding(
                panel_id=panel.panel_id,
                schema_path=schema_path,
                ok=semantic.ok,
                semantic_report=semantic,
            )
        )
    return tuple(findings)


def _raise_first_panel_semantic_failure(
    panels: tuple[PanelValidationFinding, ...],
) -> None:
    """Translate the first semantic panel failure into an AuthoringError."""
    for finding in panels:
        if finding.ok or finding.semantic_report is None:
            continue
        first = finding.semantic_report.failures[0]
        code_map = {
            PanelSemanticCode.PANEL_ID_MISMATCH: AuthoringErrorCode.PANEL_ID_MISMATCH,
            PanelSemanticCode.READY_STATE_MISSING_ROOT: (
                AuthoringErrorCode.PANEL_STATE_READY_MISSING_ROOT
            ),
            PanelSemanticCode.DEPTH_EXCEEDED: AuthoringErrorCode.PANEL_DEPTH_EXCEEDED,
            PanelSemanticCode.NODES_EXCEEDED: AuthoringErrorCode.PANEL_NODES_EXCEEDED,
            PanelSemanticCode.DUPLICATE_NODE_ID: (
                AuthoringErrorCode.PANEL_DUPLICATE_NODE_ID
            ),
            PanelSemanticCode.BINDING_NOT_TEXT_INPUT: (
                AuthoringErrorCode.PANEL_BINDING_NOT_TEXT_INPUT
            ),
            PanelSemanticCode.PARAMS_BINDINGS_COLLISION: (
                AuthoringErrorCode.PANEL_PARAMS_BINDINGS_COLLISION
            ),
            PanelSemanticCode.OPERATION_UNKNOWN: AuthoringErrorCode.PANEL_OPERATION_UNKNOWN,
        }
        raise AuthoringError(
            code=code_map.get(first.code, AuthoringErrorCode.PANEL_SCHEMA_INVALID),
            detail="panel failed semantic validation",
            field=f"{finding.schema_path}:{first.field}",
        )


def validate_project_archive(
    archive_bytes: bytes,
    *,
    caller_plugin_api_major: int = DEFAULT_CALLER_PLUGIN_API_MAJOR,
    caller_plugin_api_minor: int = DEFAULT_CALLER_PLUGIN_API_MINOR,
) -> ValidationReport:
    """Validate a packed plugin archive in memory.

    Args:
        archive_bytes: Raw bytes of the packed archive.
        caller_plugin_api_major: Caller plugin API major. Defaults to ``1``.
        caller_plugin_api_minor: Caller plugin API minor. Defaults to ``0``.

    Returns:
        A :class:`ValidationReport` carrying the archive inspection
        result, the manifest inspection result, the operation-schema
        bundle verdict, and one :class:`PanelValidationFinding` per
        declared panel contribution. Callers may surface either
        directly; the validator never raises ``InspectionError``
        itself, only :class:`AuthoringError` for the various
        inspection failures.

    Raises:
        AuthoringError: When ``manifest.json`` is missing, unreadable, or
            fails JSON decoding, schema, compatibility, or entrypoint-path
            inspection; when a declared operation schema resource is
            missing, unreadable, or fails to form a valid bundle; when a
            declared operation schema reference does not resolve inside
            the bundle; when a declared panel resource is missing,
            unreadable, fails schema validation, or fails any of the
            semantic rules the ``ui.panel.v1`` schema does not cover.
    """
    archive = inspect_archive(archive_bytes)
    manifest_names = {entry.name for entry in archive.entries}
    if _MANIFEST_ENTRY_NAME not in manifest_names:
        raise AuthoringError(
            code=AuthoringErrorCode.MISSING_MANIFEST,
            detail=f"archive does not contain {_MANIFEST_ENTRY_NAME!r}",
            field=_MANIFEST_ENTRY_NAME,
        )

    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive_file:
        manifest_bytes = _read_zip_bytes(
            archive_file,
            _MANIFEST_ENTRY_NAME,
            missing_code=AuthoringErrorCode.MISSING_MANIFEST,
            missing_detail=f"archive does not contain {_MANIFEST_ENTRY_NAME!r}",
            read_failed_code=AuthoringErrorCode.MANIFEST_READ_FAILED,
            read_failed_label=f"could not read {_MANIFEST_ENTRY_NAME!r}",
        )

    decoded = _decode_manifest_bytes(manifest_bytes)
    manifest = _inspect_manifest_for_authoring(
        decoded,
        caller_plugin_api_major=caller_plugin_api_major,
        caller_plugin_api_minor=caller_plugin_api_minor,
    )

    schema_bundle_ok: bool | None
    panel_findings: tuple[PanelValidationFinding, ...] = ()
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive_file:
        schema_bundle_ok = _verify_operation_schema_references(
            manifest, archive_file, manifest_names
        )
        panel_findings = _verify_declared_panels(
            manifest, archive_file, manifest_names
        )

    report = ValidationReport(
        archive=archive,
        manifest=manifest,
        schema_bundle_ok=schema_bundle_ok,
        panels=panel_findings,
    )

    if any(not finding.ok for finding in panel_findings):
        _raise_first_panel_semantic_failure(panel_findings)

    return report


def entrypoint_present(
    report: ValidationReport,
) -> bool:
    """Return ``True`` when the declared entrypoint file is archived.

    Centralising the check keeps :func:`validate_project_archive` free of
    the import below and lets the CLI reuse the same logic for advisory
    messages.
    """
    try:
        entrypoint_path = inspect_entrypoint_path(report.manifest.entrypoint.path)
    except InspectionError:
        return False
    names = {entry.name for entry in report.archive.entries}
    return entrypoint_path in names
