"""Codex implementation of the application-owned host integration port."""

from __future__ import annotations

from collections.abc import Sequence, Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path

from model_deck.engine.hosts import (
    HostConflictError,
    HostNotFoundError,
    HostVersionMismatchError,
)

from .runtime import (
    CodexCompatibility,
    CodexLaunchPreparation,
    CodexProcessProbe,
    CodexRuntimeError,
    CodexRuntimeDescriptor,
    capability_status,
    classify_compatibility,
    discover_runtime,
    prepare_launch,
)


CODEX_HOST_ID = "com.openai.codex"
CODEX_API_PROFILE = "codex.app-server.v1"


@dataclass(frozen=True, slots=True)
class CodexHostCapabilityReport:
    descriptor: CodexRuntimeDescriptor
    compatibility: CodexCompatibility
    availability: str


class CodexHostAdapter:
    """Discovers and prepares one Codex app-server without launching it."""

    def __init__(
        self,
        *,
        applications_dir: Path,
        observed_protocol_version: str | None,
        supported_protocol_versions: AbstractSet[str],
        process_probe: CodexProcessProbe,
        arguments: Sequence[str] = (),
        overrides: Sequence[str] = (),
    ) -> None:
        self._applications_dir = Path(applications_dir)
        self._observed_protocol_version = observed_protocol_version
        self._supported_protocol_versions = frozenset(supported_protocol_versions)
        self._process_probe = process_probe
        self._arguments = tuple(arguments)
        self._overrides = tuple(overrides)
        self.last_preparation: CodexLaunchPreparation | None = None

    def inspect(self) -> CodexHostCapabilityReport:
        descriptor = discover_runtime(self._applications_dir)
        compatibility = classify_compatibility(
            descriptor,
            observed_protocol_version=self._observed_protocol_version,
            supported_protocol_versions=self._supported_protocol_versions,
        )
        return CodexHostCapabilityReport(
            descriptor=descriptor,
            compatibility=compatibility,
            availability=capability_status(
                descriptor,
                process_probe=self._process_probe,
                compatibility=compatibility,
            ),
        )

    def list_hosts(self) -> list[dict[str, str]]:
        # Discovery is read-only. Compatibility is enforced by prepare(), where
        # the frozen public contract can return an explicit domain error.
        try:
            discover_runtime(self._applications_dir)
        except CodexRuntimeError as exc:
            raise HostNotFoundError(str(exc)) from exc
        return [{"host_id": CODEX_HOST_ID, "api_profile": CODEX_API_PROFILE}]

    def prepare(self, host_id: str) -> bool:
        if host_id != CODEX_HOST_ID:
            raise HostNotFoundError("unknown Codex host_id")
        try:
            report = self.inspect()
        except CodexRuntimeError as exc:
            raise HostNotFoundError(str(exc)) from exc
        if report.compatibility.status != "supported":
            raise HostVersionMismatchError(
                report.compatibility.reason or "Codex app-server protocol is unknown"
            )
        if report.availability == "already-running-unverified":
            raise HostConflictError("Codex is already running and cannot be verified")
        self.last_preparation = prepare_launch(
            report.descriptor,
            argv=self._arguments,
            overrides=self._overrides,
            compatibility=report.compatibility,
            process_probe=self._process_probe,
        )
        return True


__all__ = [
    "CODEX_API_PROFILE",
    "CODEX_HOST_ID",
    "CodexHostAdapter",
    "CodexHostCapabilityReport",
]
