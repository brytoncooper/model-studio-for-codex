"""In-memory validation of a packed plugin archive.

The validator composes two pure helpers:

- :func:`model_deck.plugins.archive_inspection.inspect_archive` rejects
  traversal, symlinks/special entries, encryption, duplicates, parent
  collisions, compression-ratio bombs, CRC/length mismatches and unbounded
  archives before any entry is is touched.
- :func:`model_deck.plugins.manifest_inspection.inspect_manifest` confirms
  the embedded ``manifest.json`` matches the bundled
  ``contracts/plugin.v1/manifest.schema.json`` schema, performs structural
  checks, and compares the manifest's declared API against the caller's
  declared API version.

The default caller API version is ``(1, 0)``, matching the only existing
shipped plugin manifest in the tree. Callers may override either integer
explicitly when a future host version lands.
"""
from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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

from .errors import AuthoringError, AuthoringErrorCode


DEFAULT_CALLER_PLUGIN_API_MAJOR: int = 1
"""Default caller plugin API major, matching shipped manifests."""

DEFAULT_CALLER_PLUGIN_API_MINOR: int = 0
"""Default caller plugin API minor, matching shipped manifests."""

_MANIFEST_ENTRY_NAME: str = "manifest.json"


@dataclass(frozen=True)
class ValidationReport:
    """Detached outcome of :func:`validate_project_archive`.

    Attributes:
        archive: Result returned by :func:`inspect_archive`.
        manifest: Result returned by :func:`inspect_manifest`.
    """

    archive: ArchiveInspectionResult
    manifest: InspectionResult

    @property
    def ok(self) -> bool:
        return self.archive.ok and self.manifest.ok and entrypoint_present(self)


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
        A :class:`ValidationReport` carrying both the archive inspection
        result and the manifest inspection result. Callers may surface
        either directly; the validator never raises ``InspectionError``
        itself, only :class:`AuthoringError` for manifest inspection
        failures.

    Raises:
        AuthoringError: When ``manifest.json`` is missing, unreadable, or
            fails JSON decoding, schema, compatibility, or entrypoint-path
            inspection. Non-fatal manifest findings such as duplicate
            contributions are returned through :class:`ValidationReport`.
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
        try:
            manifest_bytes = archive_file.read(_MANIFEST_ENTRY_NAME)
        except KeyError as exc:  # pragma: no cover - guarded above
            raise AuthoringError(
                code=AuthoringErrorCode.MISSING_MANIFEST,
                detail=f"archive does not contain {_MANIFEST_ENTRY_NAME!r}",
                field=_MANIFEST_ENTRY_NAME,
            ) from exc
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise AuthoringError(
                code=AuthoringErrorCode.MANIFEST_READ_FAILED,
                detail=f"could not read {_MANIFEST_ENTRY_NAME!r}: {exc}",
                field=_MANIFEST_ENTRY_NAME,
            ) from exc

    decoded = _decode_manifest_bytes(manifest_bytes)
    manifest = _inspect_manifest_for_authoring(
        decoded,
        caller_plugin_api_major=caller_plugin_api_major,
        caller_plugin_api_minor=caller_plugin_api_minor,
    )
    return ValidationReport(archive=archive, manifest=manifest)


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
