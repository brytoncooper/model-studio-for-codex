"""Frozen dataclasses that expose the detached inspection result.

These types intentionally mirror the schema vocabulary one-for-one and add
nothing the schema does not already declare. Callers receive only the values
that survived inspection; no mutable views into the input are exposed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    """Plugin identity extracted from the manifest.

    Attributes:
        manifest_id: Reverse-domain plugin id (``org.example.notebook``).
        manifest_version: Constant integer from ``manifest_version``.
        version: Plugin semver string.
    """

    manifest_id: str
    manifest_version: int
    version: str


@dataclass(frozen=True)
class Api:
    """Plugin-side API declaration.

    The host should compare this against its own API version using
    ``caller_major == manifest.major`` and ``caller_minor >= manifest.minimum_minor``.
    """

    major: int
    minimum_minor: int


@dataclass(frozen=True)
class Entrypoint:
    """Plugin entrypoint declaration.

    The path is a relative POSIX-style reference to a single file inside the
    plugin's own distribution. Inspection does not resolve it on disk.
    """

    runtime: str
    path: str


@dataclass(frozen=True)
class OperationContribution:
    """An operation contribution declared by the manifest."""

    operation_id: str
    input_schema: str
    output_schema: str
    effect: str


@dataclass(frozen=True)
class PanelContribution:
    """A panel contribution declared by the manifest."""

    panel_id: str
    schema: str


@dataclass(frozen=True)
class ProviderContribution:
    """A provider port contribution declared by the manifest.

    ``features`` is preserved as a tuple of string tokens (possibly empty).
    ``port`` is the schema-const value ``"provider.execution/v1"``.
    """

    provider_id: str
    port: str
    execution_mode: str
    features: tuple[str, ...] = ()


@dataclass(frozen=True)
class Contributions:
    """All contributions declared by the manifest.

    Empty contribution sections become empty tuples. ``permissions`` is the
    raw string list from the manifest (schema-bounded length only).
    """

    operations: tuple[OperationContribution, ...] = ()
    panels: tuple[PanelContribution, ...] = ()
    providers: tuple[ProviderContribution, ...] = ()
    permissions: tuple[str, ...] = ()


@dataclass(frozen=True)
class InspectionFailure:
    """One non-fatal defect recorded in :attr:`InspectionResult.errors`.

    The dataclass is fully frozen; callers cannot mutate the diagnostic
    data after inspection. The structured fields carry the caller-supplied
    identifiers (``id``, ``index``); the human-readable ``detail`` never
    echoes those values verbatim.

    Attributes:
        code: Stable error code from ``InspectionErrorCode``.
        field: Schema or manifest location of the defect.
        detail: Human-readable explanation; never echoes caller values.
        kind: Optional contribution kind (``operation``, ``panel``,
            ``provider``) for duplicate-contribution entries.
        id: Optional contribution identity (the structured value; not
            interpolated into ``detail``).
        index: Optional zero-based index of the offending entry.
    """

    code: str
    field: str
    detail: str = ""
    kind: str | None = None
    id: str | None = None
    index: int | None = None


@dataclass(frozen=True)
class InspectionResult:
    """Detached, immutable inspection outcome.

    ``ok`` is true when ``errors`` is empty. The dataclass is frozen; the
    enclosed collections are tuples and every entry in ``errors`` is itself
    a frozen :class:`InspectionFailure`, so the result is safe to share
    between threads and to retain as a long-lived value.
    """

    identity: Identity
    api: Api
    entrypoint: Entrypoint
    contributions: Contributions
    errors: tuple[InspectionFailure, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors
