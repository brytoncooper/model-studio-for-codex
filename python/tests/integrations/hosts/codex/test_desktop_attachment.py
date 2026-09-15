"""D1a — focused red/green tests for the V2-owned Codex Desktop attachment.

The producer owns ``model_deck.integrations.hosts.codex.desktop_attachment``,
which is expected to export:

  - ``DesktopAttachmentConfiguration.load(environment)``
      parses the three required environment variables, validates that
      every path points inside the same isolated engine directory, that
      the bridge descriptor base URL is loopback ``/v1`` and that the
      engine credential token is non-empty.

  - ``EngineRegisteredModelCatalog(engine, configuration)``
      exposes ``load_models()`` / ``catalog_entries(...)`` /
      ``selected()`` / ``select(...)`` (the structural
      ``CatalogProjection`` shape consumed by ``AppServerBridge``),
      backed by a call to the public ``engine.v1.models.list`` with
      ``collection="registered"`` and one entry per descriptor
      registration/model with its current ``display_name``.

  - ``V2DesktopRouteAdapter(configuration)``
      provides the custom provider route used by ``thread/start`` and
      ``thread/resume``: the provider identity is ``model_deck_v2`` and
      the route carries the descriptor ``base_url`` together with the
      name of the process-local environment variable from which Codex reads
      the bridge bearer. Neither the engine nor bridge token is serialized
      into app-server messages; CodexResponsesBridge rejects anything except
      the descriptor token.

  - ``run_desktop_attachment(arguments, environment, emit, process_launcher)``
      composes the existing ``AppServerBridge`` with the catalog and
      adapter above; subscription (``gpt-*``) routing is left on the
      host ``openai`` provider. The composition accepts explicit
      ``engine=`` and ``router=`` keyword arguments so the host adapter
      can thread the same V2 ``Engine`` instance and ``RouterProcess``
      used elsewhere; the catalog must observe that exact engine so
      ``bridge.router is router`` and ``bridge.catalog`` reflects the
      engine the caller injected.

The descriptor JSON carries the same fields as the bridge descriptor in
``bridge.py`` (``base_url``, ``provider_id``, ``model``, ``token_path``)
plus two identifiers that pin which engine registration belongs to this
attachment:

  - ``registration_id`` — the unique id of the V2 registration entry.
  - ``connection_id``  — the connection this registration lives under.

The catalog must filter ``engine.v1.models.list(collection="registered")``
results to only entries whose ``registration_id`` and ``connection_id``
match the descriptor; entries with different identifiers belong to other
attachments or other registrations and must not appear in the Desktop
picker.

These tests intentionally do not exercise a live Codex process: a
fake engine stands in for ``engine.v1.models.list``, a fake router
satisfies the ``RouterProcess`` shape, and ``bridge.request`` is
monkey-patched to return scripted responses.

Configuration environment keys (the producer must read these names):

  - ``MODEL_DECK_V2_ENGINE_RENDEZVOUS_PATH``  rendezvous file under the
    isolated engine directory.
  - ``MODEL_DECK_V2_ENGINE_CREDENTIAL_PATH`` credential file (raw token
    on disk) under the same isolated engine directory.
  - ``MODEL_DECK_V2_CODEX_BRIDGE_DESCRIPTOR_PATH`` JSON descriptor whose
    ``base_url`` is loopback ``/v1`` and whose ``token_path`` resolves
    under the same isolated engine directory.

Initial red is expected: this module fails with ``ModuleNotFoundError``
until the producer lands ``desktop_attachment.py``. Once landed, the
producer must satisfy the public names and the observable behaviour
asserted below.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from model_deck.integrations.hosts.codex import app_server
from model_deck.integrations.hosts.codex.app_server import (
    AppServerBridge,
    BridgeError,
)


# --- Settled configuration environment keys ---------------------------------

ENGINE_RENDEZVOUS_ENV = "MODEL_DECK_V2_ENGINE_RENDEZVOUS_PATH"
ENGINE_CREDENTIAL_ENV = "MODEL_DECK_V2_ENGINE_CREDENTIAL_PATH"
BRIDGE_DESCRIPTOR_ENV = "MODEL_DECK_V2_CODEX_BRIDGE_DESCRIPTOR_PATH"

CUSTOM_PROVIDER_NAME = "model_deck_v2"


# --- Fakes -----------------------------------------------------------------


class _FakeEngine:
    """Stand-in for the public V2 engine RPC client.

    Only the methods the catalog actually depends on are implemented.
    ``list_calls`` records every ``v1.models.list`` invocation so the
    tests can prove the catalog goes through the public engine API.
    """

    def __init__(self, registered: list[dict[str, Any]] | None = None) -> None:
        self._registered: list[dict[str, Any]] = list(registered or [])
        self.list_calls: list[dict[str, Any]] = []

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.assert_supported_method(method)
        call_params = dict(params)
        self.list_calls.append(call_params)
        return {
            "collection": call_params.get("collection", "registered"),
            "items": list(self._registered),
        }

    @staticmethod
    def assert_supported_method(method: str) -> None:
        if method != "engine.v1.models.list":
            raise AssertionError(f"unexpected engine method: {method}")


def _make_registered_item(
    *,
    provider_model_id: str,
    display_name: str,
    connection_id: str | None = None,
    registration_id: str | None = None,
    revision: int = 1,
) -> dict[str, Any]:
    return {
        "kind": "registered",
        "registration_id": registration_id or str(uuid.uuid4()),
        "provider_model_id": provider_model_id,
        "connection_id": connection_id or str(uuid.uuid4()),
        "display_name": display_name,
        "revision": revision,
    }


def _write_descriptor(
    directory: Path,
    *,
    base_url: str,
    token: str = "tok-test",
    registration_id: str | None = None,
    connection_id: str | None = None,
    model: str = "deepseek/deepseek-v4.1-flash",
) -> dict[str, Any]:
    """Write a private bridge descriptor inside the isolated engine directory.

    The descriptor carries the loopback ``/v1`` URL and a token file that
    lives next to it, so configuration loading can verify the same
    isolated engine directory constraint. The ``registration_id`` and
    ``connection_id`` fields pin which engine registration belongs to
    this attachment; the catalog uses them to filter ``engine.v1.models.list``
    results down to a single registration.
    """
    token_path = directory / "bridge.token"
    token_path.write_text(token, encoding="utf-8")
    registration_id = registration_id or str(uuid.uuid4())
    connection_id = connection_id or str(uuid.uuid4())
    descriptor_path = directory / "bridge.descriptor.json"
    descriptor = {
        "schema_version": 1,
        "base_url": base_url,
        "provider_id": "openrouter",
        "model": model,
        "billing_description": "OpenRouter credits",
        "token_path": str(token_path),
        "registration_id": registration_id,
        "connection_id": connection_id,
    }
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    return {
        "descriptor_path": descriptor_path,
        "token_path": token_path,
        "registration_id": registration_id,
        "connection_id": connection_id,
        "descriptor": descriptor,
    }


def _write_rendezvous(directory: Path) -> Path:
    rendezvous = directory / "engine.rendezvous"
    rendezvous.write_text(
        json.dumps(
            {
                "engine_instance_id": str(uuid.uuid4()),
                "instance_nonce": str(uuid.uuid4()),
                "socket_path": str(directory / "engine.sock"),
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    return rendezvous


def _write_credential(directory: Path, *, token: str = "engine-token-xyz") -> Path:
    credential = directory / "engine.credential"
    credential.write_text(token, encoding="utf-8")
    return credential


def _build_isolated_environment(
    tmp: Path, *, base_url: str = "http://127.0.0.1:55142/v1"
) -> dict[str, str]:
    """All three paths live inside ``tmp`` so the same-directory check passes.

    The descriptor and the matching fake-engine registration share the
    same ``registration_id`` / ``connection_id`` so the catalog's exact
    filter passes by default; tests that exercise mismatches override the
    identifiers explicitly.
    """
    rendezvous = _write_rendezvous(tmp)
    credential = _write_credential(tmp)
    descriptor = _write_descriptor(tmp, base_url=base_url)
    return {
        ENGINE_RENDEZVOUS_ENV: str(rendezvous),
        ENGINE_CREDENTIAL_ENV: str(credential),
        BRIDGE_DESCRIPTOR_ENV: str(descriptor["descriptor_path"]),
        "_registration_id": descriptor["registration_id"],
        "_connection_id": descriptor["connection_id"],
    }


# --- Reusable structural fakes for the bridge ------------------------------


class _FakeRouter:
    """Satisfies the structural ``RouterProcess`` shape used by ``AppServerBridge``."""

    base_url = "http://127.0.0.1:4242/backend-api/codex"

    def __init__(self) -> None:
        self.catalog_waits: list[float] = []
        self.started = False
        self.stopped = False

    def codex_arguments(self) -> list[str]:
        return [
            "-c",
            f'openai_base_url="{self.base_url}"',
            "-c",
            "features.enable_request_compression=false",
        ]

    def wait_for_catalog(self, timeout: float) -> bool:
        self.catalog_waits.append(timeout)
        return True

    def cancel_cursor_turn(self, thread_id: str, turn_id: str | None = None) -> None:
        return None

    def price_table(self) -> dict[str, Any]:
        return {}

    def benchmark_lines(self) -> dict[str, Any]:
        return {}

    def remember_thread(self, thread_id: str, cwd: str) -> None:
        return None

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def _fake_process_launcher() -> dict[str, str]:
    """Stand-in: never executed, but satisfies the structural process launcher shape."""
    return {
        "application_path": "/Applications/Codex.app",
        "executable_path": "/Applications/Codex.app/Contents/Resources/codex",
        "bundle_identifier": "com.openai.codex",
    }


def _instruction_builder(registry, price_table=None, benchmark_lines=None) -> str:
    parts = [app_server.ROUTING_INSTRUCTIONS]
    if price_table is not None or benchmark_lines is not None:
        parts.append(
            f"price_table={price_table or {}} benchmark_lines={benchmark_lines or {}}"
        )
    parts.append(app_server.MCP_INSTRUCTIONS)
    return "\n\n".join(parts)


# --- Test scaffolding ------------------------------------------------------


class DesktopAttachmentImportTests(unittest.TestCase):
    """Pin the public surface so a renamed symbol is caught immediately."""

    def test_module_is_independently_importable(self) -> None:
        from model_deck.integrations.hosts.codex import desktop_attachment

        for name in (
            "DesktopAttachmentConfiguration",
            "EngineRegisteredModelCatalog",
            "V2DesktopRouteAdapter",
            "run_desktop_attachment",
        ):
            with self.subTest(symbol=name):
                self.assertTrue(
                    hasattr(desktop_attachment, name),
                    f"desktop_attachment must export {name}",
                )


class ConfigurationLoadingTests(unittest.TestCase):
    """``DesktopAttachmentConfiguration.load(environment)`` accepts only valid isolated setups."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine_dir = Path(self.tmp.name) / "engine"
        self.engine_dir.mkdir()

    def test_load_succeeds_with_loopback_v1_inside_isolated_engine_directory(self) -> None:
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            DesktopAttachmentConfiguration,
        )

        environment = _build_isolated_environment(self.engine_dir)
        config = DesktopAttachmentConfiguration.load(environment)

        # The configuration must surface the three referenced paths plus the parsed
        # tokens and base_url so the catalog, route adapter and bridge composition can
        # reach them without re-reading the environment.
        self.assertTrue(Path(config.engine_rendezvous_path).is_file())
        self.assertTrue(Path(config.engine_credential_path).is_file())
        self.assertTrue(Path(config.codex_bridge_descriptor_path).is_file())
        # Engine credential authenticates EngineRPC only.
        self.assertEqual(config.engine_token, "engine-token-xyz")
        # Bridge descriptor token authenticates CodexResponsesBridge.
        self.assertEqual(config.bridge_token, "tok-test")
        self.assertEqual(config.bridge_base_url, "http://127.0.0.1:55142/v1")

    def test_load_rejects_non_loopback_bridge_base_url(self) -> None:
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            DesktopAttachmentConfiguration,
        )

        environment = _build_isolated_environment(
            self.engine_dir, base_url="http://attacker.example/v1"
        )
        with self.assertRaises(Exception) as caught:
            DesktopAttachmentConfiguration.load(environment)
        self.assertIsNotNone(caught.exception)

    def test_load_rejects_bridge_base_url_without_v1_suffix(self) -> None:
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            DesktopAttachmentConfiguration,
        )

        environment = _build_isolated_environment(
            self.engine_dir, base_url="http://127.0.0.1:55142"
        )
        with self.assertRaises(Exception):
            DesktopAttachmentConfiguration.load(environment)

    def test_load_rejects_rendezvous_outside_isolated_engine_directory(self) -> None:
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            DesktopAttachmentConfiguration,
        )

        # Place the rendezvous file one level above the isolated engine directory; the
        # same-directory invariant must refuse the configuration.
        outsider = Path(self.tmp.name)
        outsider_rendezvous = outsider / "outside.rendezvous"
        outsider_rendezvous.write_text("{}", encoding="utf-8")
        environment = _build_isolated_environment(self.engine_dir)
        environment[ENGINE_RENDEZVOUS_ENV] = str(outsider_rendezvous)
        with self.assertRaises(Exception):
            DesktopAttachmentConfiguration.load(environment)

    def test_load_rejects_missing_required_environment_keys(self) -> None:
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            DesktopAttachmentConfiguration,
        )

        partial = {ENGINE_RENDEZVOUS_ENV: str(self.engine_dir / "x")}
        with self.assertRaises(Exception):
            DesktopAttachmentConfiguration.load(partial)

    def test_load_rejects_empty_engine_credential(self) -> None:
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            DesktopAttachmentConfiguration,
        )

        environment = _build_isolated_environment(self.engine_dir)
        Path(environment[ENGINE_CREDENTIAL_ENV]).write_text("   \n  ", encoding="utf-8")
        with self.assertRaises(Exception):
            DesktopAttachmentConfiguration.load(environment)


class EngineRegisteredModelCatalogTests(unittest.TestCase):
    """The catalog must be a structural ``CatalogProjection`` backed by engine.v1.models.list."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine_dir = Path(self.tmp.name) / "engine"
        self.engine_dir.mkdir()

    def _configuration(self):
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            DesktopAttachmentConfiguration,
        )

        return DesktopAttachmentConfiguration.load(
            _build_isolated_environment(self.engine_dir)
        )

    def test_catalog_queries_engine_v1_models_list_with_registered_collection(self) -> None:
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            EngineRegisteredModelCatalog,
        )

        config = self._configuration()
        engine = _FakeEngine(
            registered=[
                _make_registered_item(
                    provider_model_id="deepseek/deepseek-v4.1-flash",
                    display_name="DeepSeek V4.1 Flash · OpenRouter",
                    registration_id=config.registration_id,
                    connection_id=config.connection_id,
                )
            ]
        )
        catalog = EngineRegisteredModelCatalog(engine, config)
        # The catalog must call the public engine API rather than reach into private state.
        models = catalog.load_models()
        self.assertEqual(engine.list_calls, [{"collection": "registered"}])
        self.assertIn("deepseek/deepseek-v4.1-flash", models)

    def test_catalog_exposes_only_descriptor_registration_with_current_display_name(self) -> None:
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            EngineRegisteredModelCatalog,
        )

        config = self._configuration()
        engine = _FakeEngine(
            registered=[
                _make_registered_item(
                    provider_model_id="deepseek/deepseek-v4.1-flash",
                    display_name="DeepSeek V4.1 Flash · OpenRouter",
                    registration_id=config.registration_id,
                    connection_id=config.connection_id,
                ),
                # Even malformed duplicate data must not expose a model other
                # than the one pinned by the bridge descriptor.
                _make_registered_item(
                    provider_model_id="deepseek/deepseek-v4.1-flash",
                    display_name="DeepSeek V4.1 Flash · OpenRouter",
                    registration_id=config.registration_id,
                    connection_id=config.connection_id,
                ),
            ]
        )
        catalog = EngineRegisteredModelCatalog(engine, config)
        models = catalog.load_models()
        self.assertEqual(list(models), ["deepseek/deepseek-v4.1-flash"])
        registration = models["deepseek/deepseek-v4.1-flash"]
        self.assertEqual(registration["registration_id"], config.registration_id)
        self.assertEqual(registration["connection_id"], config.connection_id)
        self.assertEqual(registration["display_name"], "DeepSeek V4.1 Flash · OpenRouter")

    def test_catalog_filters_out_registrations_with_mismatched_identifiers(self) -> None:
        """Exact registration_id + connection_id matching is the only filter the catalog applies.

        A second registration on the engine that shares neither identifier with
        the descriptor belongs to a different attachment and must never reach
        the Desktop picker, regardless of provider_model_id.
        """
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            EngineRegisteredModelCatalog,
        )

        config = self._configuration()
        engine = _FakeEngine(
            registered=[
                _make_registered_item(
                    provider_model_id="deepseek/deepseek-v4.1-flash",
                    display_name="DeepSeek V4.1 Flash · OpenRouter",
                    registration_id=config.registration_id,
                    connection_id=config.connection_id,
                ),
                _make_registered_item(
                    provider_model_id="deepseek/deepseek-v4.1-flash",
                    display_name="DeepSeek V4.1 Flash · OpenRouter",
                    registration_id="00000000-0000-0000-0000-00000000ffff",
                    connection_id="00000000-0000-0000-0000-00000000ffff",
                ),
            ]
        )
        catalog = EngineRegisteredModelCatalog(engine, config)
        rows = catalog.catalog_entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "deepseek/deepseek-v4.1-flash")
        # Two distinct engine items were offered; only the one whose identifiers
        # match the descriptor survives — proving the filter is exact.
        self.assertEqual(len(engine._registered), 2)
        self.assertEqual(len(rows), 1)

    def test_catalog_filters_by_registration_id_only_when_connection_matches_too(self) -> None:
        """Half-matches must drop the entry; both identifiers are required."""
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            EngineRegisteredModelCatalog,
        )

        config = self._configuration()
        engine = _FakeEngine(
            registered=[
                # Matches registration_id but uses a different connection_id.
                _make_registered_item(
                    provider_model_id="openai/gpt-5.6-sol",
                    display_name="OpenAI GPT-5.6 SOL",
                    registration_id=config.registration_id,
                    connection_id="00000000-0000-0000-0000-00000000aaaa",
                ),
                # The exact match.
                _make_registered_item(
                    provider_model_id="deepseek/deepseek-v4.1-flash",
                    display_name="DeepSeek V4.1 Flash · OpenRouter",
                    registration_id=config.registration_id,
                    connection_id=config.connection_id,
                ),
            ]
        )
        catalog = EngineRegisteredModelCatalog(engine, config)
        rows = catalog.catalog_entries()
        models = [row["model"] for row in rows]
        self.assertEqual(models, ["deepseek/deepseek-v4.1-flash"])

    def test_catalog_builds_desktop_model_list_rows_with_current_display_name(self) -> None:
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            EngineRegisteredModelCatalog,
        )

        config = self._configuration()
        engine = _FakeEngine(
            registered=[
                _make_registered_item(
                    provider_model_id="deepseek/deepseek-v4.1-flash",
                    display_name="DeepSeek V4.1 Flash · OpenRouter",
                    registration_id=config.registration_id,
                    connection_id=config.connection_id,
                )
            ]
        )
        catalog = EngineRegisteredModelCatalog(engine, config)
        rows = catalog.catalog_entries()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(row["displayName"], "DeepSeek V4.1 Flash · OpenRouter")
        # Desktop picker rows must carry a stable identifier so repeated fetches line up.
        self.assertTrue(row.get("id"))

    def test_catalog_rename_appears_on_next_model_list(self) -> None:
        """A rename in the engine must surface on the very next ``catalog_entries`` call.

        This is the exact behaviour that lets Desktop refresh its picker after the
        user renames a registered model in the V2 UI, without restarting the bridge.
        """
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            EngineRegisteredModelCatalog,
        )

        config = self._configuration()
        engine = _FakeEngine(
            registered=[
                _make_registered_item(
                    provider_model_id="deepseek/deepseek-v4.1-flash",
                    display_name="DeepSeek V4.1 Flash · OpenRouter",
                    registration_id=config.registration_id,
                    connection_id=config.connection_id,
                )
            ]
        )
        catalog = EngineRegisteredModelCatalog(engine, config)

        first = catalog.catalog_entries()
        self.assertEqual(first[0]["displayName"], "DeepSeek V4.1 Flash · OpenRouter")

        # Engine-side rename: the fake engine reflects the new display name on its next
        # list call. The catalog must observe that without restarting.
        engine._registered[0]["display_name"] = (
            "DeepSeek V4.1 Flash (renamed) · OpenRouter"
        )

        second = catalog.catalog_entries()
        self.assertEqual(second[0]["displayName"], "DeepSeek V4.1 Flash (renamed) · OpenRouter")
        # Two distinct calls into the engine — one per ``catalog_entries``.
        self.assertEqual(len(engine.list_calls), 2)


class V2DesktopRouteAdapterTests(unittest.TestCase):
    """The route adapter must yield the ``model_deck_v2`` provider with descriptor base_url + token."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine_dir = Path(self.tmp.name) / "engine"
        self.engine_dir.mkdir()

    def _configuration(self):
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            DesktopAttachmentConfiguration,
        )

        return DesktopAttachmentConfiguration.load(
            _build_isolated_environment(self.engine_dir)
        )

    def test_adapter_returns_model_deck_v2_provider_with_descriptor_and_token(self) -> None:
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            V2DesktopRouteAdapter,
        )

        adapter = V2DesktopRouteAdapter(self._configuration())
        route = adapter.route("deepseek/deepseek-v4.1-flash")
        self.assertEqual(route["provider"], CUSTOM_PROVIDER_NAME)
        provider_config = route["config"]["model_providers"][CUSTOM_PROVIDER_NAME]
        self.assertEqual(provider_config["base_url"], "http://127.0.0.1:55142/v1")
        # Codex resolves the bearer from this attachment process's environment;
        # neither private token is serialized into the app-server request.
        self.assertEqual(provider_config["env_key"], "MODEL_DECK_V2_BRIDGE_TOKEN")
        self.assertNotIn("auth", provider_config)
        # Wire protocol must be the OpenAI-compatible responses stream so Desktop can
        # talk to it without a custom transport.
        self.assertEqual(provider_config["wire_api"], "responses")

    def test_adapter_refuses_unrelated_models(self) -> None:
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            V2DesktopRouteAdapter,
        )

        adapter = V2DesktopRouteAdapter(self._configuration())
        with self.assertRaises(BridgeError):
            adapter.route("gpt-5.6-sol")
        with self.assertRaises(BridgeError):
            adapter.route("deepseek/unregistered")


class _RunDesktopAttachmentTestBase(unittest.IsolatedAsyncioTestCase):
    """Compose ``AppServerBridge`` through ``run_desktop_attachment`` and drive JSON-RPC."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine_dir = Path(self.tmp.name) / "engine"
        self.engine_dir.mkdir()

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # Make sure the bridge never sees the real /Applications/ChatGPT.app through
        # the structural ``process_launcher`` injection: the launcher is never called
        # because we monkey-patch ``bridge.request`` to skip the subprocess entirely.
        # We must pre-create the isolated environment first so the fake engine can
        # share the descriptor's registration_id/connection_id and pass the filter.
        self.environment = _build_isolated_environment(self.engine_dir)
        self.engine = _FakeEngine(
            registered=[
                _make_registered_item(
                    provider_model_id="deepseek/deepseek-v4.1-flash",
                    display_name="DeepSeek V4.1 Flash · OpenRouter",
                    registration_id=self.environment["_registration_id"],
                    connection_id=self.environment["_connection_id"],
                )
            ]
        )
        self.router = _FakeRouter()
        self.messages: list[dict[str, Any]] = []
        self.requests: list[tuple[str, dict[str, Any]]] = []

        # Defer the actual import + composition until the test method runs so a
        # missing ``desktop_attachment`` module surfaces as ``ModuleNotFoundError``
        # per test instead of poisoning ``asyncSetUp`` for unrelated cases.
        from model_deck.integrations.hosts.codex.desktop_attachment import (
            DesktopAttachmentConfiguration,
            EngineRegisteredModelCatalog,
            V2DesktopRouteAdapter,
            run_desktop_attachment,
        )

        self._configuration = DesktopAttachmentConfiguration.load(self.environment)
        self._catalog = EngineRegisteredModelCatalog(self.engine, self._configuration)
        self._route_adapter = V2DesktopRouteAdapter(self._configuration)
        self._run = run_desktop_attachment

    async def _bridge_with(self) -> AppServerBridge:
        """Build a bridge via ``run_desktop_attachment`` and monkey-patch its request backend.

        The composition accepts the same V2 ``Engine`` instance and
        ``RouterProcess`` the host adapter already constructed, so the
        catalog observes ``self.engine`` and ``bridge.router is self.router``
        without inventing new transport surfaces.
        """
        bridge = self._run(
            arguments=["app-server"],
            environment=self.environment,
            emit=self.messages.append,
            process_launcher=_fake_process_launcher,
            engine=self.engine,
            router=self.router,
        )
        # ``run_desktop_attachment`` may return either an ``AppServerBridge`` instance
        # directly or an opaque handle that exposes ``handle_client``. Tests accept
        # either, but every observable we assert lives in the standard bridge shape.
        if not isinstance(bridge, AppServerBridge):
            self.fail(
                f"run_desktop_attachment must return an AppServerBridge, got {type(bridge)!r}"
            )
        assert isinstance(bridge, AppServerBridge)
        self.assertTrue(hasattr(bridge, "handle_client"))
        self.assertTrue(hasattr(bridge, "thread_providers"))

        async def _request(method, params):
            self.requests.append((method, json.loads(json.dumps(params))))
            if method == "config/read":
                return {
                    "config": {
                        "model": "gpt-6-astra",
                        "model_reasoning_effort": "ultra",
                        "sandbox_mode": "read-only",
                    },
                    "layers": [],
                }
            if method == "model/list":
                return {
                    "data": [
                        {
                            "id": "gpt-6-astra",
                            "model": "gpt-6-astra",
                            "displayName": "gpt-6-astra",
                            "hidden": False,
                        },
                        {
                            "id": "gpt-5.6-sol",
                            "model": "gpt-5.6-sol",
                            "displayName": "gpt-5.6-sol",
                            "hidden": False,
                        },
                    ],
                    "nextCursor": None,
                }
            if method == "thread/start":
                return {
                    "thread": {"id": params.get("threadId", "t-v2-1"), "cwd": "/work"},
                    "modelProvider": params.get("modelProvider", "openai"),
                }
            if method == "thread/resume":
                return {
                    "thread": {"id": params.get("threadId", "t-v2-1"), "cwd": "/work"},
                    "modelProvider": params.get("modelProvider", "openai"),
                }
            return {}

        bridge.request = _request  # type: ignore[method-assign]
        return bridge


class RunDesktopAttachmentCompositionTests(_RunDesktopAttachmentTestBase):
    """``run_desktop_attachment`` delegates to the existing ``AppServerBridge`` shape."""

    async def test_run_returns_bridge_that_delegates_to_existing_app_server_bridge(self) -> None:
        bridge = await self._bridge_with()
        self.assertIsInstance(bridge, AppServerBridge)
        # The router the catalog was configured with is the one the bridge runs.
        self.assertIs(bridge.router, self.router)

    async def test_thread_start_injects_model_deck_v2_provider_for_v2_model(self) -> None:
        bridge = await self._bridge_with()
        await bridge.handle_client(
            {
                "id": 1,
                "method": "thread/start",
                "params": {"model": "deepseek/deepseek-v4.1-flash"},
            }
        )
        self.assertTrue(self.messages, "expected at least one emitted message")
        result = self.messages[-1].get("result")
        self.assertIsInstance(result, dict)
        self.assertEqual(result["modelProvider"], CUSTOM_PROVIDER_NAME)
        # The bridge remembers the thread→provider identity for continuation.
        self.assertEqual(
            bridge.thread_providers.get(result["thread"]["id"]), CUSTOM_PROVIDER_NAME
        )
        # The request forwarded to the backend must carry the custom provider and
        # the model_providers definition built from the descriptor + bridge token.
        sent = next(p for method, p in self.requests if method == "thread/start")
        self.assertEqual(sent["modelProvider"], CUSTOM_PROVIDER_NAME)
        provider = sent["config"]["model_providers"][CUSTOM_PROVIDER_NAME]
        self.assertEqual(provider["base_url"], "http://127.0.0.1:55142/v1")
        self.assertEqual(provider["env_key"], "MODEL_DECK_V2_BRIDGE_TOKEN")
        self.assertNotIn("auth", provider)

    async def test_thread_resume_preserves_model_deck_v2_provider_on_same_thread(self) -> None:
        bridge = await self._bridge_with()
        await bridge.handle_client(
            {
                "id": 1,
                "method": "thread/start",
                "params": {"model": "deepseek/deepseek-v4.1-flash"},
            }
        )
        start_message = self.messages[-1]
        thread_id = start_message["result"]["thread"]["id"]
        # A second message in the same thread must keep the custom provider; we
        # explicitly omit the model on resume so the bridge uses the saved identity.
        await bridge.handle_client(
            {"id": 2, "method": "thread/resume", "params": {"threadId": thread_id}}
        )
        resume_message = self.messages[-1]
        self.assertEqual(resume_message["result"]["modelProvider"], CUSTOM_PROVIDER_NAME)
        self.assertEqual(bridge.thread_providers.get(thread_id), CUSTOM_PROVIDER_NAME)

    async def test_subscription_model_keeps_openai_provider(self) -> None:
        bridge = await self._bridge_with()
        await bridge.handle_client(
            {"id": 1, "method": "thread/start", "params": {"model": "gpt-5.6-sol"}}
        )
        result = self.messages[-1]["result"]
        self.assertEqual(result["modelProvider"], "openai")
        # Host-bound subscription models must never reach the ``model_deck_v2`` provider,
        # even though the configuration and adapter are wired up.
        thread_id = result["thread"]["id"]
        self.assertNotEqual(bridge.thread_providers.get(thread_id), CUSTOM_PROVIDER_NAME)
        sent = next(p for method, p in self.requests if method == "thread/start")
        self.assertNotIn("modelProvider", sent)
        self.assertNotIn(
            CUSTOM_PROVIDER_NAME, sent.get("config", {}).get("model_providers", {})
        )


class EngineAndConfigurationInteractionTests(_RunDesktopAttachmentTestBase):
    """Combined checks that span configuration, catalog and bridge composition."""

    async def test_catalog_rename_reaches_bridge_model_list_without_restart(self) -> None:
        bridge = await self._bridge_with()
        # First model/list call observes the initial display name.
        await bridge.handle_client({"id": 1, "method": "model/list", "params": {}})
        first = self.messages[-1]["result"]["data"]
        deepseek_rows = [row for row in first if row["model"] == "deepseek/deepseek-v4.1-flash"]
        self.assertEqual(len(deepseek_rows), 1)
        self.assertEqual(
            deepseek_rows[0]["displayName"], "DeepSeek V4.1 Flash · OpenRouter"
        )

        # Rename happens in the engine, no bridge restart. Re-fetching must surface the
        # new display name on the very next call.
        self.engine._registered[0]["display_name"] = (
            "DeepSeek V4.1 Flash (renamed) · OpenRouter"
        )
        await bridge.handle_client({"id": 2, "method": "model/list", "params": {}})
        second = self.messages[-1]["result"]["data"]
        deepseek_rows = [row for row in second if row["model"] == "deepseek/deepseek-v4.1-flash"]
        self.assertEqual(len(deepseek_rows), 1)
        self.assertEqual(
            deepseek_rows[0]["displayName"],
            "DeepSeek V4.1 Flash (renamed) · OpenRouter",
        )

    async def test_run_composition_preserves_existing_bridge_catalog_and_router_shapes(self) -> None:
        """The composition must not invent new transport surfaces; it reuses ``AppServerBridge``."""
        bridge = await self._bridge_with()
        # ``catalog`` and ``router`` must be the structural surfaces ``AppServerBridge`` exposes.
        self.assertTrue(hasattr(bridge, "catalog"))
        self.assertTrue(hasattr(bridge, "registry"))
        self.assertTrue(hasattr(bridge, "router"))
        # ``registry`` is the preserved alias for ``catalog`` in the bridge, both pointing
        # at the same object the composition passed in.
        self.assertIs(bridge.registry, bridge.catalog)
        self.assertIs(bridge.router, self.router)


if __name__ == "__main__":
    unittest.main()
