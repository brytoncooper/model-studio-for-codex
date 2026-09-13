from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from model_deck.adapters.storage.sqlite_connection_repository import SQLiteConnectionRepository
from model_deck.adapters.storage.sqlite_model_repository import SQLiteModelRepository
from model_deck.engine.connections.ports import SaveConnectionCommand
from model_deck.engine.model_library.ports import (
    RegisterModelCommand,
    RemoveModelCommand,
    RenameModelCommand,
)
from model_deck.engine.projections.ports import ProjectionOutboxEvent
from model_deck.integrations.hosts.codex.agent_materializer import (
    AgentMaterializer,
    AgentMaterializerSettings,
    ResolvedConnection,
)
from model_deck.integrations.hosts.codex.projection_composition import (
    CommittedConnectionSnapshots,
    CommittedModelSnapshots,
    CommittedSnapshotError,
)
from model_deck.integrations.hosts.codex.projection_consumer.consumer import (
    CodexProjectionMaterializationError,
)

SETTINGS = AgentMaterializerSettings(
    agents_rel_dir="agents",
    token_helper_path="/Applications/Model Deck.app/Contents/MacOS/Model Deck",
)


def endpoint_resolver(record):
    return ResolvedConnection(
        connection_id=record.connection_id,
        kind="endpoint",
        revision=record.revision,
        endpoint_name=record.endpoint_config_ref,
        base_url="https://openrouter.ai/api/v1",
        credential_account_id=record.credential_ref,
        billing_description="Uses OpenRouter credits.",
    )


def make_event(registration_id, connection_id, provider_model_id, display_name, revision):
    payload = {
        "connection_id": connection_id,
        "display_name": display_name,
        "provider_model_id": provider_model_id,
        "registration_id": registration_id,
        "revision": revision,
    }
    return ProjectionOutboxEvent(
        outbox_id=1,
        aggregate_type="registered_model",
        aggregate_id=registration_id,
        aggregate_revision=revision,
        event_kind="registered_model.upserted",
        payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
    )


class ProjectionSnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "deck.sqlite"
        self.models = SQLiteModelRepository(db)
        self.conns = SQLiteConnectionRepository(db)
        self.conns.save(
            SaveConnectionCommand(
                connection_id="c1",
                provider_id="openrouter",
                expected_revision=0,
                idempotency_key="conn-1",
                endpoint_config_ref="OpenRouter",
                credential_ref="550e8400-e29b-41d4-a716-446655440000",
            )
        )
        self.reg = self.models.register(
            RegisterModelCommand(
                connection_id="c1",
                provider_model_id="deepseek/deepseek-v4.1-flash",
                display_name="DeepSeek Flash",
                expected_revision=0,
                idempotency_key="reg-1",
            )
        )

    def _materializer(self, resolve=endpoint_resolver):
        return AgentMaterializer(
            CommittedConnectionSnapshots(self.conns, resolve),
            CommittedModelSnapshots(self.models),
            SETTINGS,
        )

    def _event_for_current(self):
        rec = self.models.list_registered()[0]
        return make_event(
            rec.registration_id, rec.connection_id, rec.provider_model_id, rec.display_name, rec.revision
        )

    def test_materialize_current_commit(self):
        write = self._materializer().materialize(self._event_for_current())
        self.assertTrue(write.path.name.endswith(".toml"))
        self.assertIn(b"deepseek/deepseek-v4.1-flash", write.data)

    def test_updated_record_materializes_new_revision(self):
        renamed = self.models.rename(
            RenameModelCommand(
                registration_id=self.reg.registration_id,
                display_name="DeepSeek Flash II",
                expected_revision=self.reg.revision,
                idempotency_key="rename-1",
            )
        )
        event = make_event(
            renamed.registration_id,
            renamed.connection_id,
            renamed.provider_model_id,
            renamed.display_name,
            renamed.revision,
        )
        write = self._materializer().materialize(event)
        self.assertIn(b"deepseek/deepseek-v4.1-flash", write.data)

    def test_removed_record_refuses(self):
        event = self._event_for_current()
        self.models.remove(
            RemoveModelCommand(
                registration_id=self.reg.registration_id,
                expected_revision=self.reg.revision,
                idempotency_key="remove-1",
            )
        )
        with self.assertRaises(CodexProjectionMaterializationError):
            self._materializer().materialize(event)

    def test_resolver_revision_mismatch_rejected(self):
        def stale(record):
            resolved = endpoint_resolver(record)
            return ResolvedConnection(
                connection_id=resolved.connection_id,
                kind=resolved.kind,
                revision=resolved.revision - 1,
                endpoint_name=resolved.endpoint_name,
                base_url=resolved.base_url,
                credential_account_id=resolved.credential_account_id,
                billing_description=resolved.billing_description,
            )

        with self.assertRaises(CommittedSnapshotError):
            CommittedConnectionSnapshots(self.conns, stale).lookup("c1")
        with self.assertRaises(CodexProjectionMaterializationError):
            self._materializer(resolve=stale).materialize(self._event_for_current())

    def test_resolver_identity_mismatch_rejected(self):
        def wrong_id(record):
            resolved = endpoint_resolver(record)
            return ResolvedConnection(
                connection_id="other",
                kind=resolved.kind,
                revision=resolved.revision,
                endpoint_name=resolved.endpoint_name,
                base_url=resolved.base_url,
                credential_account_id=resolved.credential_account_id,
                billing_description=resolved.billing_description,
            )

        with self.assertRaises(CommittedSnapshotError):
            CommittedConnectionSnapshots(self.conns, wrong_id).lookup("c1")
        with self.assertRaises(CodexProjectionMaterializationError):
            self._materializer(resolve=wrong_id).materialize(self._event_for_current())

    def test_resolver_failure_carries_no_secret(self):
        def boom(record):
            raise RuntimeError("metadata store exploded: " + str(record.credential_ref))

        with self.assertRaises(CommittedSnapshotError) as ctx:
            CommittedConnectionSnapshots(self.conns, boom).lookup("c1")
        self.assertNotIn("550e8400", str(ctx.exception))
        with self.assertRaises(CodexProjectionMaterializationError):
            self._materializer(resolve=boom).materialize(self._event_for_current())


    def test_resolver_failures_normalized_fixed_message_no_chain(self):
        import traceback
        sentinel = "SECRET-9f31-leak-probe"
        cases = {
            "typed": lambda record: (_ for _ in ()).throw(
                CommittedSnapshotError("wrapped boom " + sentinel)
            ),
            "runtime": lambda record: (_ for _ in ()).throw(
                RuntimeError("store exploded " + sentinel)
            ),
            "none": lambda record: None,
        }
        for name, resolver in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(CommittedSnapshotError) as ctx:
                    CommittedConnectionSnapshots(self.conns, resolver).lookup("c1")
                exc = ctx.exception
                self.assertEqual(str(exc), "connection metadata unavailable", name)
                self.assertIsNone(exc.__cause__, name)
                self.assertTrue(exc.__suppress_context__, name)
                tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                self.assertNotIn(sentinel, tb, name)
                self.assertNotIn("550e8400", tb, name)


if __name__ == "__main__":
    unittest.main()
