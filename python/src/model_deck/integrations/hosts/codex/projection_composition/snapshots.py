from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from model_deck.engine.connections.ports import ConnectionRecord, ConnectionRepository
from model_deck.engine.model_library.ports import ModelRepository, RegisteredModelRecord
from model_deck.integrations.hosts.codex.agent_materializer.resolution import (
    ResolvedConnection,
    ResolvedModel,
)


class CommittedSnapshotError(ValueError):
    """Committed snapshot cannot be resolved without guessing."""


ConnectionMetadataResolver = Callable[[ConnectionRecord], ResolvedConnection]
"""Caller-supplied mapping from a committed record to render metadata.

The resolver decodes only opaque caller-owned references already present on
the record. It never reads credential values and never invents endpoint data.
"""


def _model_to_resolved(record: RegisteredModelRecord) -> ResolvedModel:
    return ResolvedModel(
        registration_id=record.registration_id,
        connection_id=record.connection_id,
        provider_model_id=record.provider_model_id,
        display_name=record.display_name,
        revision=record.revision,
    )


@dataclass(frozen=True, slots=True)
class CommittedModelSnapshots:
    """ModelSnapshot over committed PUBLIC ModelRepository.list_registered."""

    repository: ModelRepository

    def lookup(self, registration_id: str) -> ResolvedModel | None:
        records = self.repository.list_registered()
        matches = [r for r in records if r.registration_id == registration_id]
        if not matches:
            return None
        first = _model_to_resolved(matches[0])
        for other in matches[1:]:
            if _model_to_resolved(other) != first:
                raise CommittedSnapshotError("ambiguous committed model snapshot")
        return first


@dataclass(frozen=True, slots=True)
class CommittedConnectionSnapshots:
    """ConnectionSnapshot over committed PUBLIC ConnectionRepository.list_connections."""

    repository: ConnectionRepository
    resolve: ConnectionMetadataResolver

    def lookup(self, connection_id: str) -> ResolvedConnection | None:
        records = self.repository.list_connections()
        matches = [r for r in records if r.connection_id == connection_id]
        if not matches:
            return None
        if len(matches) > 1:
            raise CommittedSnapshotError("ambiguous committed connection snapshot")
        record = matches[0]
        try:
            resolved = self.resolve(record)
        except Exception:
            raise CommittedSnapshotError("connection metadata unavailable") from None
        if (
            not isinstance(resolved, ResolvedConnection)
            or resolved.connection_id != record.connection_id
            or resolved.revision != record.revision
        ):
            raise CommittedSnapshotError("connection metadata unavailable") from None
        return resolved
