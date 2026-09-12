"""Pure in-memory manifest inspection.

The inspector validates a raw manifest mapping against the frozen
``contracts/plugin.v1/manifest.schema.json`` and returns a detached,
immutable :class:`InspectionResult`. It performs additional structural
checks the schema does not cover (entrypoint path normalization,
duplicate contribution identities, API compatibility against a caller-
supplied target). It does not access the filesystem, spawn processes,
make network calls, or perform plugin discovery.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from model_deck_contracts.validator import (
    SchemaValidationError,
    validate_schema_ref,
)

from .errors import InspectionError, InspectionErrorCode
from .models import (
    Api,
    Contributions,
    Entrypoint,
    Identity,
    InspectionFailure,
    InspectionResult,
    OperationContribution,
    PanelContribution,
    ProviderContribution,
)

_MANIFEST_SCHEMA_REF = "contracts/plugin.v1/manifest.schema.json"

_PATH_MAX_LENGTH = 256

# Fixed human-readable detail for schema validation failures. The bundled
# validator's exception message can include the failing instance value
# (e.g. the invalid version string); callers must never see that echoed back.
_SCHEMA_INVALID_DETAIL = "manifest failed schema validation"


def _as_mapping(raw: Any) -> Mapping[str, Any]:
    """Reject non-mapping inputs without leaking the raw value into errors."""
    if isinstance(raw, Mapping):
        return raw
    raise InspectionError(
        code=InspectionErrorCode.SCHEMA_INVALID,
        detail="manifest must be a JSON object",
    )


def _safe_schema_field(exc: SchemaValidationError) -> str | None:
    """Return the schema location of ``exc`` without echoing its message.

    The bundled validator's exception can carry the failing instance value
    through its ``message`` and ``instance`` attributes. This helper
    extracts only the schema path (e.g. ``"contributes.operations.0.effect"``)
    so that the public :class:`InspectionError` cannot leak caller-supplied
    data. When the validator reports no specific path, returns ``None``.
    """
    absolute_path = getattr(exc, "absolute_path", None)
    if absolute_path is None:
        return None
    parts = list(absolute_path)
    if not parts:
        return None
    return ".".join(str(part) for part in parts)


def _check_entrypoint_path(path: Any) -> str:
    """Structural checks for the entrypoint path string.

    The schema regex ``^[^/\\\\][^\\\\0]*$`` already rejects paths that start
    with ``/`` or ``\\\\`` and rejects embedded NUL bytes. The additional
    checks here reject empty segments, ``.`` and ``..`` segments, embedded
    backslashes, drive-letter colons, ensure ``maxLength`` parity with the
    schema, and confirm the path is not empty. The inspector makes no
    filesystem access and no claim about whether the resolved path is a
    symlink.
    """
    if not isinstance(path, str):
        raise InspectionError(
            code=InspectionErrorCode.SCHEMA_INVALID,
            detail="entrypoint.path must be a string",
            field="entrypoint.path",
        )
    if path == "":
        raise InspectionError(
            code=InspectionErrorCode.ENTRYPOINT_PATH_INVALID,
            detail="entrypoint.path is empty",
            field="entrypoint.path",
        )
    if len(path) > _PATH_MAX_LENGTH:
        raise InspectionError(
            code=InspectionErrorCode.ENTRYPOINT_PATH_INVALID,
            detail=f"entrypoint.path exceeds {_PATH_MAX_LENGTH} chars",
            field="entrypoint.path",
        )
    if path.startswith("/") or path.startswith("\\"):
        raise InspectionError(
            code=InspectionErrorCode.ENTRYPOINT_PATH_INVALID,
            detail="entrypoint.path must be relative, not absolute",
            field="entrypoint.path",
        )
    # UNC paths (``\\server\share\...``) start with a backslash and are
    # rejected by the leading-backslash check above; no extra branch needed.
    if "\\" in path:
        raise InspectionError(
            code=InspectionErrorCode.ENTRYPOINT_PATH_INVALID,
            detail="entrypoint.path contains a backslash",
            field="entrypoint.path",
        )
    # Reject Windows-style drive prefixes (``C:``, ``C:/...``, ``C:\...``)
    # and any other colon usage. POSIX entrypoint paths never legitimately
    # carry a colon and the schema regex does not forbid it.
    if ":" in path:
        raise InspectionError(
            code=InspectionErrorCode.ENTRYPOINT_PATH_INVALID,
            detail="entrypoint.path contains a drive letter or colon",
            field="entrypoint.path",
        )
    if "\x00" in path:
        raise InspectionError(
            code=InspectionErrorCode.ENTRYPOINT_PATH_INVALID,
            detail="entrypoint.path contains NUL",
            field="entrypoint.path",
        )
    # Explicit segment-by-segment checks using pure POSIX semantics (split on
    # ``/``). The inspector never reads the filesystem, so there is no
    # symlink resolution; this only validates the string the plugin declared.
    segments = path.split("/")
    for segment in segments:
        if segment == "":
            raise InspectionError(
                code=InspectionErrorCode.ENTRYPOINT_PATH_INVALID,
                detail="entrypoint.path has an empty segment",
                field="entrypoint.path",
            )
        if segment == ".":
            raise InspectionError(
                code=InspectionErrorCode.ENTRYPOINT_PATH_INVALID,
                detail="entrypoint.path references the current directory",
                field="entrypoint.path",
            )
        if segment == "..":
            raise InspectionError(
                code=InspectionErrorCode.ENTRYPOINT_PATH_INVALID,
                detail="entrypoint.path contains parent traversal",
                field="entrypoint.path",
            )
    return path


def _scan_duplicates(
    kind: str,
    items: list[Mapping[str, Any]],
    field_path: str,
) -> tuple[list[InspectionFailure], dict[str, int]]:
    """Walk a contribution list, recording indices per id and emitting failures."""
    failures: list[InspectionFailure] = []
    counts: dict[str, int] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise InspectionError(
                code=InspectionErrorCode.SCHEMA_INVALID,
                detail=f"{kind} contribution must be an object",
                field=f"{field_path}[{index}]",
            )
        cid = item.get("id")
        if not isinstance(cid, str):
            raise InspectionError(
                code=InspectionErrorCode.SCHEMA_INVALID,
                detail=f"{kind} contribution missing string id",
                field=f"{field_path}[{index}].id",
            )
        prior = counts.get(cid, 0)
        if prior > 0:
            # ``detail`` deliberately omits the caller-supplied id; the
            # structured ``id`` attribute carries it for callers that need it.
            failures.append(
                InspectionFailure(
                    code=InspectionErrorCode.DUPLICATE_CONTRIBUTION,
                    field=f"{field_path}[{index}].id",
                    detail=(
                        f"duplicate {kind} contribution "
                        f"(prior occurrence at index {prior - 1})"
                    ),
                    kind=kind,
                    id=cid,
                    index=index,
                )
            )
        counts[cid] = prior + 1
    return failures, counts


def _build_operations(items: list[Mapping[str, Any]]) -> tuple[OperationContribution, ...]:
    out: list[OperationContribution] = []
    for item in items:
        out.append(
            OperationContribution(
                operation_id=item["id"],
                input_schema=item["input_schema"],
                output_schema=item["output_schema"],
                effect=item["effect"],
            )
        )
    return tuple(out)


def _build_panels(items: list[Mapping[str, Any]]) -> tuple[PanelContribution, ...]:
    out: list[PanelContribution] = []
    for item in items:
        out.append(
            PanelContribution(
                panel_id=item["id"],
                schema=item["schema"],
            )
        )
    return tuple(out)


def _build_providers(items: list[Mapping[str, Any]]) -> tuple[ProviderContribution, ...]:
    out: list[ProviderContribution] = []
    for item in items:
        features_raw = item.get("features", [])
        if not isinstance(features_raw, list):
            raise InspectionError(
                code=InspectionErrorCode.SCHEMA_INVALID,
                detail="provider contribution features must be an array",
                field="contributes.providers[].features",
            )
        features = tuple(str(f) for f in features_raw)
        out.append(
            ProviderContribution(
                provider_id=item["id"],
                port=item["port"],
                execution_mode=item["execution_mode"],
                features=features,
            )
        )
    return tuple(out)


def _validate_api_version(
    manifest_major: int,
    manifest_min_minor: int,
    caller_major: int,
    caller_minor: int,
) -> None:
    """Compatibility: caller accepts the manifest if the major matches and the
    caller's minor is at least the manifest's declared minimum minor.

    Raises ``InspectionError`` with code ``incompatible_api_version`` when the
    manifest is not acceptable to the caller.
    """
    if isinstance(manifest_major, bool) or not isinstance(manifest_major, int):
        raise InspectionError(
            code=InspectionErrorCode.SCHEMA_INVALID,
            detail="plugin_api.major must be an integer",
            field="plugin_api.major",
        )
    if isinstance(manifest_min_minor, bool) or not isinstance(manifest_min_minor, int):
        raise InspectionError(
            code=InspectionErrorCode.SCHEMA_INVALID,
            detail="plugin_api.minimum_minor must be an integer",
            field="plugin_api.minimum_minor",
        )
    if isinstance(caller_major, bool) or not isinstance(caller_major, int):
        raise InspectionError(
            code=InspectionErrorCode.SCHEMA_INVALID,
            detail="caller_plugin_api_major must be an integer",
            field="caller_plugin_api_major",
        )
    if isinstance(caller_minor, bool) or not isinstance(caller_minor, int):
        raise InspectionError(
            code=InspectionErrorCode.SCHEMA_INVALID,
            detail="caller_plugin_api_minor must be an integer",
            field="caller_plugin_api_minor",
        )
    if manifest_major != caller_major:
        raise InspectionError(
            code=InspectionErrorCode.INCOMPATIBLE_API_VERSION,
            detail=(
                f"manifest declares plugin_api.major={manifest_major}; "
                f"caller supports major={caller_major}"
            ),
            field="plugin_api.major",
        )
    if caller_minor < manifest_min_minor:
        raise InspectionError(
            code=InspectionErrorCode.INCOMPATIBLE_API_VERSION,
            detail=(
                f"manifest requires plugin_api.minimum_minor>={manifest_min_minor}; "
                f"caller minor={caller_minor}"
            ),
            field="plugin_api.minimum_minor",
        )


def inspect_manifest(
    raw: Any,
    *,
    caller_plugin_api_major: int,
    caller_plugin_api_minor: int,
) -> InspectionResult:
    """Inspect a raw plugin manifest and return a detached result.

    Args:
        raw: An already-decoded manifest mapping. Must be a ``Mapping[str, object]``.
        caller_plugin_api_major: The plugin API major the caller can host.
        caller_plugin_api_minor: The plugin API minor the caller can host.

    Returns:
        An immutable :class:`InspectionResult` with ``ok == True`` when the
        manifest is valid against the schema and compatible with the caller's
        declared plugin API version.

    Raises:
        InspectionError: On the first schema or compatibility defect. Duplicate
            contribution identities are not raised — they are accumulated into
            ``InspectionResult.errors`` so the caller can report all of them
            in one pass.
    """
    document = _as_mapping(raw)
    try:
        validate_schema_ref(_MANIFEST_SCHEMA_REF, document)
    except SchemaValidationError as exc:
        # Sanitize the validator exception: do not propagate its raw message
        # (which can include the failing caller-supplied value) and do not
        # chain it into our exception's traceback either. ``from None``
        # suppresses the chained ``__cause__`` display in Python's default
        # ``traceback.format_exception`` output, so a caller that introspects
        # the formatted traceback cannot recover the validator's raw message
        # (and the failing caller-supplied value embedded in it). The
        # structured ``detail`` is the fixed constant; ``field`` carries
        # the schema path only.
        raise InspectionError(
            code=InspectionErrorCode.SCHEMA_INVALID,
            detail=_SCHEMA_INVALID_DETAIL,
            field=_safe_schema_field(exc),
        ) from None

    plugin_api = document.get("plugin_api", {})
    manifest_major = plugin_api.get("major")
    manifest_min_minor = plugin_api.get("minimum_minor")
    _validate_api_version(
        manifest_major,
        manifest_min_minor,
        caller_plugin_api_major,
        caller_plugin_api_minor,
    )

    entrypoint = document.get("entrypoint", {})
    path = _check_entrypoint_path(entrypoint.get("path"))

    contributions_section = document.get("contributes") or {}
    operations_raw = contributions_section.get("operations") or []
    panels_raw = contributions_section.get("panels") or []
    providers_raw = contributions_section.get("providers") or []

    if not isinstance(operations_raw, list):
        raise InspectionError(
            code=InspectionErrorCode.SCHEMA_INVALID,
            detail="contributes.operations must be an array",
            field="contributes.operations",
        )
    if not isinstance(panels_raw, list):
        raise InspectionError(
            code=InspectionErrorCode.SCHEMA_INVALID,
            detail="contributes.panels must be an array",
            field="contributes.panels",
        )
    if not isinstance(providers_raw, list):
        raise InspectionError(
            code=InspectionErrorCode.SCHEMA_INVALID,
            detail="contributes.providers must be an array",
            field="contributes.providers",
        )

    failures: list[InspectionFailure] = []
    op_failures, _op_counts = _scan_duplicates(
        "operation", list(operations_raw), "contributes.operations"
    )
    panel_failures, _panel_counts = _scan_duplicates(
        "panel", list(panels_raw), "contributes.panels"
    )
    provider_failures, _provider_counts = _scan_duplicates(
        "provider", list(providers_raw), "contributes.providers"
    )
    failures.extend(op_failures)
    failures.extend(panel_failures)
    failures.extend(provider_failures)

    permissions_raw = document.get("permissions") or []
    if not isinstance(permissions_raw, list):
        raise InspectionError(
            code=InspectionErrorCode.SCHEMA_INVALID,
            detail="permissions must be an array",
            field="permissions",
        )
    permissions = tuple(str(p) for p in permissions_raw)

    identity = Identity(
        manifest_id=document["id"],
        manifest_version=document["manifest_version"],
        version=document["version"],
    )
    api = Api(major=manifest_major, minimum_minor=manifest_min_minor)
    entrypoint_obj = Entrypoint(
        runtime=entrypoint["runtime"],
        path=path,
    )
    contributions = Contributions(
        operations=_build_operations(list(operations_raw)),
        panels=_build_panels(list(panels_raw)),
        providers=_build_providers(list(providers_raw)),
        permissions=permissions,
    )

    return InspectionResult(
        identity=identity,
        api=api,
        entrypoint=entrypoint_obj,
        contributions=contributions,
        errors=tuple(failures),
    )


def inspect_entrypoint_path(path: str) -> str:
    """Validate an entrypoint path string and return it unchanged on success.

    Pure string check; the inspector never resolves the path against a
    filesystem.
    """
    return _check_entrypoint_path(path)
