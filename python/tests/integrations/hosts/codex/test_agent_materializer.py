from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path

from model_deck.engine.projections.ports import ProjectionOutboxEvent
from model_deck.integrations.hosts.codex.agent_materializer import (
    AgentMaterializer,
    AgentMaterializerSettings,
    ResolvedConnection,
    ResolvedModel,
)
from model_deck.integrations.hosts.codex.agent_renderer import AGENT_MARKER
from model_deck.integrations.hosts.codex.projection_consumer.consumer import (
    CodexProjectionMaterializationError,
)

ACCOUNT_ID = "550e8400-e29b-41d4-a716-446655440000"
HELPER = "/Applications/Model Deck.app/Contents/MacOS/Model Deck"
SETTINGS = AgentMaterializerSettings(agents_rel_dir="agents", token_helper_path=HELPER)

CONNECTION = ResolvedConnection(
    connection_id="c1",
    kind="endpoint",
    revision=1,
    endpoint_name="OpenRouter",
    base_url="https://openrouter.ai/api/v1",
    credential_account_id=ACCOUNT_ID,
    billing_description="Uses OpenRouter credits.",
)
MODEL = ResolvedModel(
    registration_id="reg-1",
    connection_id="c1",
    provider_model_id="deepseek/deepseek-v4.1-flash",
    display_name="DeepSeek Flash",
    revision=3,
)


def make_event(**overrides):
    payload = {
        "connection_id": "c1",
        "display_name": "DeepSeek Flash",
        "provider_model_id": "deepseek/deepseek-v4.1-flash",
        "registration_id": "reg-1",
        "revision": 3,
    }
    payload.update(overrides.get("payload", {}))
    return ProjectionOutboxEvent(
        outbox_id=overrides.get("outbox_id", 1),
        aggregate_type="registered_model",
        aggregate_id=overrides.get("aggregate_id", "reg-1"),
        aggregate_revision=overrides.get("aggregate_revision", 3),
        event_kind="registered_model.upserted",
        payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
    )


class DictSnapshots:
    def __init__(self, connection=True, model=True):
        self._connection = CONNECTION if connection else None
        self._model = MODEL if model else None

    def lookup_connection(self, connection_id):
        return self._connection if connection_id == "c1" else None

    def lookup_model(self, registration_id):
        return self._model if registration_id == "reg-1" else None


class ConnectionSnapshot:
    def __init__(self, snaps):
        self._snaps = snaps

    def lookup(self, connection_id):
        return self._snaps.lookup_connection(connection_id)


class ModelSnapshot:
    def __init__(self, snaps):
        self._snaps = snaps

    def lookup(self, registration_id):
        return self._snaps.lookup_model(registration_id)


def materializer(snaps, settings=SETTINGS):
    return AgentMaterializer(ConnectionSnapshot(snaps), ModelSnapshot(snaps), settings)


class AgentMaterializerTest(unittest.TestCase):
    def test_endpoint_output_matches_real_renderer(self):
        from model_deck.integrations.hosts.codex.agent_renderer import (
            RenderRequest,
            render_managed_agent,
        )

        snaps = DictSnapshots()
        write = materializer(snaps).materialize(make_event())
        self.assertEqual(write.path, Path("agents") / write.path.name)
        self.assertFalse(write.path.is_absolute())
        expected = render_managed_agent(
            RenderRequest(
                kind="endpoint",
                provider_model_id=MODEL.provider_model_id,
                display_name=MODEL.display_name,
                endpoint_name=CONNECTION.endpoint_name,
                base_url=CONNECTION.base_url,
                credential_account_id=CONNECTION.credential_account_id,
                token_helper_path=HELPER,
                reasoning_effort=None,
                billing_description=CONNECTION.billing_description,
            )
        )
        self.assertEqual(write.path.name, expected.filename)
        self.assertEqual(write.data, expected.content)
        text = write.data.decode("utf-8")
        self.assertTrue(text.startswith(AGENT_MARKER + "\n"))
        tomllib.loads(text)

    def test_subscription_output(self):
        connection = ResolvedConnection(connection_id="c9", kind="subscription", revision=1)
        model = ResolvedModel(
            registration_id="reg-9",
            connection_id="c9",
            provider_model_id="gpt-5.6-sol",
            display_name="Sol",
            revision=2,
        )

        class C:
            def lookup(self, connection_id):
                return connection if connection_id == "c9" else None

        class M:
            def lookup(self, registration_id):
                return model if registration_id == "reg-9" else None

        payload = {
            "connection_id": "c9",
            "display_name": "Sol",
            "provider_model_id": "gpt-5.6-sol",
            "registration_id": "reg-9",
            "revision": 2,
        }
        event = ProjectionOutboxEvent(
            outbox_id=7,
            aggregate_type="registered_model",
            aggregate_id="reg-9",
            aggregate_revision=2,
            event_kind="registered_model.upserted",
            payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True),
        )
        write = AgentMaterializer(C(), M(), SETTINGS).materialize(event)
        self.assertEqual(write.path.name, "subscription_gpt_5_6_sol.toml")
        tomllib.loads(write.data.decode("utf-8"))

    def test_missing_model_fails_closed(self):
        with self.assertRaises(CodexProjectionMaterializationError):
            materializer(DictSnapshots(model=False)).materialize(make_event())

    def test_missing_connection_fails_closed(self):
        with self.assertRaises(CodexProjectionMaterializationError):
            materializer(DictSnapshots(connection=False)).materialize(make_event())

    def test_stale_model_revision_mismatch(self):
        snaps = DictSnapshots()
        stale = ResolvedModel(
            registration_id="reg-1",
            connection_id="c1",
            provider_model_id=MODEL.provider_model_id,
            display_name=MODEL.display_name,
            revision=2,
        )

        class M:
            def lookup(self, registration_id):
                return stale

        with self.assertRaises(CodexProjectionMaterializationError):
            AgentMaterializer(
                ConnectionSnapshot(snaps), M(), SETTINGS
            ).materialize(make_event())

    def test_wrong_connection_identity_mismatch(self):
        snaps = DictSnapshots()
        wrong = ResolvedConnection(
            connection_id="c-other",
            kind="endpoint",
            revision=1,
            endpoint_name="OpenRouter",
            base_url="https://openrouter.ai/api/v1",
            credential_account_id=ACCOUNT_ID,
            billing_description="Uses OpenRouter credits.",
        )

        class C:
            def lookup(self, connection_id):
                return wrong

        with self.assertRaises(CodexProjectionMaterializationError):
            AgentMaterializer(
                C(), ModelSnapshot(snaps), SETTINGS
            ).materialize(make_event())

    def test_event_identity_mismatch(self):
        with self.assertRaises(CodexProjectionMaterializationError):
            materializer(DictSnapshots()).materialize(make_event(aggregate_id="reg-other"))

    def test_resolver_runtime_error_converted_without_secret(self):
        import traceback
        from model_deck.integrations.hosts.codex.agent_materializer import (
            ConnectionSnapshot as _CS,
            ModelSnapshot as _MS,
        )
        secret = "super-secret-token-abc123"

        class BadModels:
            def lookup(self, registration_id):
                raise RuntimeError(secret)

        snaps = DictSnapshots()
        mat = AgentMaterializer(ConnectionSnapshot(snaps), BadModels(), SETTINGS)
        with self.assertRaises(CodexProjectionMaterializationError) as ctx:
            mat.materialize(make_event())
        self.assertNotIn(secret, str(ctx.exception))
        formatted = "".join(traceback.format_exception(ctx.exception))
        self.assertNotIn(secret, formatted)

    def test_renderer_runtime_error_converted_without_secret(self):
        import traceback
        secret = "renderer-secret-xyz789"

        def bad_renderer(request):
            raise RuntimeError(secret)

        snaps = DictSnapshots()
        mat = AgentMaterializer(
            ConnectionSnapshot(snaps), ModelSnapshot(snaps), SETTINGS, renderer=bad_renderer
        )
        with self.assertRaises(CodexProjectionMaterializationError) as ctx:
            mat.materialize(make_event())
        self.assertNotIn(secret, str(ctx.exception))
        formatted = "".join(traceback.format_exception(ctx.exception))
        self.assertNotIn(secret, formatted)

    def test_render_error_message_not_leaked(self):
        import traceback
        from model_deck.integrations.hosts.codex.agent_renderer.renderer import RenderError
        secret = "render-detail-secret-456"

        def bad_renderer(request):
            raise RenderError(secret)

        snaps = DictSnapshots()
        mat = AgentMaterializer(
            ConnectionSnapshot(snaps), ModelSnapshot(snaps), SETTINGS, renderer=bad_renderer
        )
        with self.assertRaises(CodexProjectionMaterializationError) as ctx:
            mat.materialize(make_event())
        self.assertNotIn(secret, str(ctx.exception))
        formatted = "".join(traceback.format_exception(ctx.exception))
        self.assertNotIn(secret, formatted)

    def test_invalid_settings_fail_closed(self):
        bad = AgentMaterializerSettings(
            agents_rel_dir="/abs", token_helper_path=HELPER
        )
        with self.assertRaises(CodexProjectionMaterializationError):
            materializer(DictSnapshots(), settings=bad).materialize(make_event())


if __name__ == "__main__":
    unittest.main()
