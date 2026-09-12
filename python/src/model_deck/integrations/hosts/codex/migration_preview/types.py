from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SourceInventoryEntry:
    path: str
    classification: str
    sha256: str | None
    store_version: str | int | None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ProposedConnection:
    connection_id: str
    provider_id: str
    endpoint_config_ref: str | None
    credential_ref: str | None
    revision: int
    classification: str
    source_paths: tuple[str, ...]
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ProposedRegisteredModel:
    registration_id: str
    provider_model_id: str
    connection_id: str
    display_name: str
    revision: int
    classification: str
    source_paths: tuple[str, ...]
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class LegacyImportPreviewPlan:
    fixture_root: str
    sources: tuple[SourceInventoryEntry, ...]
    proposed_connections: tuple[ProposedConnection, ...]
    proposed_models: tuple[ProposedRegisteredModel, ...]
    import_ready_connections: tuple[ProposedConnection, ...]
    import_ready_models: tuple[ProposedRegisteredModel, ...]
    diagnostics: tuple[str, ...]

    def to_sortable_dict(self) -> dict[str, Any]:
        def source_row(entry: SourceInventoryEntry) -> dict[str, Any]:
            return {
                "path": entry.path,
                "classification": entry.classification,
                "sha256": entry.sha256,
                "store_version": entry.store_version,
                "detail": entry.detail,
            }

        def connection_row(entry: ProposedConnection) -> dict[str, Any]:
            return {
                "connection_id": entry.connection_id,
                "provider_id": entry.provider_id,
                "endpoint_config_ref": entry.endpoint_config_ref,
                "credential_ref": entry.credential_ref,
                "revision": entry.revision,
                "classification": entry.classification,
                "source_paths": list(entry.source_paths),
                "detail": entry.detail,
            }

        def model_row(entry: ProposedRegisteredModel) -> dict[str, Any]:
            return {
                "registration_id": entry.registration_id,
                "provider_model_id": entry.provider_model_id,
                "connection_id": entry.connection_id,
                "display_name": entry.display_name,
                "revision": entry.revision,
                "classification": entry.classification,
                "source_paths": list(entry.source_paths),
                "detail": entry.detail,
            }

        return {
            "fixture_root": self.fixture_root,
            "sources": [source_row(item) for item in self.sources],
            "proposed_connections": [connection_row(item) for item in self.proposed_connections],
            "proposed_models": [model_row(item) for item in self.proposed_models],
            "import_ready_connections": [connection_row(item) for item in self.import_ready_connections],
            "import_ready_models": [model_row(item) for item in self.import_ready_models],
            "diagnostics": list(self.diagnostics),
        }
