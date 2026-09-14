"""Composition-owned Codex projection coordinator wiring existing adapters.

This executable-boundary module composes the SQLite outbox/intent/receipt adapters,
``CommittedModelSnapshots`` / ``CommittedConnectionSnapshots``,
``AgentMaterializer``, and ``FixtureConditionalProjectionFiles`` into one
``CodexProjectionCoordinator`` that exposes the engine-owned
``ProjectionCoordinator`` port.

It never reaches into another system's private code or storage. It reads
committed desired state through the explicit ``ModelRepository`` and
``ConnectionRepository`` it is given and writes projection files into the
injected ``projection_root`` only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from model_deck.adapters.filesystem.conditional_projection_files import (
    FixtureConditionalProjectionFiles,
)
from model_deck.adapters.storage.sqlite_projection_dependency_recovery import (
    recover_connection_dependencies,
)
from model_deck.adapters.storage.sqlite_projection_intents import (
    SQLiteProjectionMutationIntentJournal,
)
from model_deck.adapters.storage.sqlite_projection_outbox import (
    SQLiteProjectionOutboxReader,
)
from model_deck.adapters.storage.sqlite_projection_receipts import (
    SQLiteProjectionReceiptStore,
)
from model_deck.engine.connections.ports import ConnectionRepository
from model_deck.engine.model_library.ports import ModelRepository
from model_deck.engine.projections.coordinator import (
    ProjectionCoordinator,
    ProjectionStatusReport,
)
from model_deck.engine.projections.file_port import ConditionalProjectionFiles
from model_deck.engine.projections.intents import ProjectionMutationIntentJournal
from model_deck.engine.projections.ports import ProjectionOutboxReader
from model_deck.engine.projections.receipts import ProjectionReceiptStore
from model_deck.integrations.hosts.codex.agent_materializer.materializer import (
    AgentMaterializer,
    AgentMaterializerSettings,
)
from model_deck.integrations.hosts.codex.agent_materializer.resolution import ResolvedConnection
from model_deck.integrations.hosts.codex.projection_composition.snapshots import (
    CommittedConnectionSnapshots,
    CommittedModelSnapshots,
    ConnectionMetadataResolver,
)
from model_deck.integrations.hosts.codex.projection_consumer.consumer import (
    CodexProjectionConsumer,
    CONSUMER_ID,
)

MANAGED_AGENT_MARKER = b"# Managed by OpenRouter Settings native-agent registration v1\n"
AGENTS_REL_DIR = "agents"


@dataclass(frozen=True, slots=True)
class CodexProjectionCoordinator(ProjectionCoordinator):
    """Wire the existing adapters into one Codex projection coordinator."""

    host_id: str
    projection_root: Path
    database_path: Path
    model_repository: ModelRepository
    connection_repository: ConnectionRepository
    resolver: ConnectionMetadataResolver
    consumer: CodexProjectionConsumer
    outbox: ProjectionOutboxReader
    receipts: SQLiteProjectionReceiptStore
    _last_reconciled_at: str | None

    @classmethod
    def build(
        cls,
        *,
        host_id: str = CONSUMER_ID,
        projection_root: Path,
        database_path: Path,
        model_repository: ModelRepository,
        connection_repository: ConnectionRepository,
        resolver: ConnectionMetadataResolver,
        token_helper_path: Path,
    ) -> "CodexProjectionCoordinator":
        outbox = SQLiteProjectionOutboxReader(database_path)
        journal: ProjectionMutationIntentJournal = SQLiteProjectionMutationIntentJournal(database_path)
        receipts: ProjectionReceiptStore = SQLiteProjectionReceiptStore(database_path)
        files: ConditionalProjectionFiles = FixtureConditionalProjectionFiles(
            projection_root, owned_prefix=MANAGED_AGENT_MARKER
        )
        settings = AgentMaterializerSettings(
            agents_rel_dir=AGENTS_REL_DIR, token_helper_path=str(token_helper_path)
        )
        models = CommittedModelSnapshots(model_repository)
        connections = CommittedConnectionSnapshots(connection_repository, resolver)
        materializer = AgentMaterializer(connections=connections, models=models, settings=settings)
        consumer = CodexProjectionConsumer(outbox, journal, files, receipts, materializer)
        return cls(
            host_id=host_id,
            projection_root=projection_root,
            database_path=database_path,
            model_repository=model_repository,
            connection_repository=connection_repository,
            resolver=resolver,
            consumer=consumer,
            outbox=outbox,
            receipts=receipts,
            _last_reconciled_at=None,
        )

    def reconcile_after(self, *, trigger: str) -> None:
        try:
            self.consumer.consume_pending(limit=100)
        except Exception:
            # Reconciliation is best-effort by design. Never raise to the caller.
            return
        object.__setattr__(self, "_last_reconciled_at", (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ))

    def recover_then_reconcile(self, *, trigger: str) -> None:
        """Dependency recovery, then a fresh sweep. Startup-only entry point."""
        try:
            recover_connection_dependencies(self.database_path)
        except Exception:
            pass
        self.reconcile_after(trigger=trigger)

    def status(self) -> ProjectionStatusReport:
        try:
            pending_count = len(self.outbox.list_pending(limit=1000))
        except FileNotFoundError:
            pending_count = 0
        unresolved_conflict = self.receipts.latest_unresolved_conflict_detail(
            consumer_id=self.host_id
        )
        if unresolved_conflict is not None:
            value = "failed"
        elif pending_count > 0:
            value = "pending"
        else:
            value = "ready"
        return ProjectionStatusReport(
            host_id=self.host_id,
            status=value,
            last_reconciled_at=self._last_reconciled_at,
            pending_count=pending_count,
            last_conflict_reason=unresolved_conflict,
        )
