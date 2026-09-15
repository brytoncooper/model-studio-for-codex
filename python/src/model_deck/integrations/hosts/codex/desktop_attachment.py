"""V2 composition for Codex Desktop's app-server attachment seam."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .app_server import AppServerBridge, BridgeError

ENGINE_RENDEZVOUS_ENV = "MODEL_DECK_V2_ENGINE_RENDEZVOUS_PATH"
ENGINE_CREDENTIAL_ENV = "MODEL_DECK_V2_ENGINE_CREDENTIAL_PATH"
BRIDGE_DESCRIPTOR_ENV = "MODEL_DECK_V2_CODEX_BRIDGE_DESCRIPTOR_PATH"
DESKTOP_PROVIDER = "model_deck_v2"
BRIDGE_TOKEN_ENV = "MODEL_DECK_V2_BRIDGE_TOKEN"


def _regular_file(value: str, name: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError(f"{name} must be an absolute regular file")
    return path.resolve()


@dataclass(frozen=True, slots=True)
class DesktopAttachmentConfiguration:
    engine_rendezvous_path: Path
    engine_credential_path: Path
    codex_bridge_descriptor_path: Path
    engine_token: str
    bridge_token: str
    bridge_base_url: str
    provider_id: str
    model: str
    display_name: str
    billing_description: str
    registration_id: str
    connection_id: str

    @classmethod
    def load(cls, environment: Mapping[str, str]) -> "DesktopAttachmentConfiguration":
        try:
            rendezvous = _regular_file(
                environment[ENGINE_RENDEZVOUS_ENV], ENGINE_RENDEZVOUS_ENV
            )
            credential = _regular_file(
                environment[ENGINE_CREDENTIAL_ENV], ENGINE_CREDENTIAL_ENV
            )
            descriptor_path = _regular_file(
                environment[BRIDGE_DESCRIPTOR_ENV], BRIDGE_DESCRIPTOR_ENV
            )
        except KeyError as error:
            raise ValueError(f"missing required environment variable: {error.args[0]}") from error
        engine_directory = descriptor_path.parent
        if rendezvous.parent != engine_directory or credential.parent != engine_directory:
            raise ValueError("Desktop attachment files must share one isolated engine directory")
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Codex bridge descriptor is invalid") from error
        if not isinstance(descriptor, dict) or descriptor.get("schema_version") != 1:
            raise ValueError("Codex bridge descriptor has an unsupported schema")
        base_url = descriptor.get("base_url")
        parsed = urlsplit(base_url if isinstance(base_url, str) else "")
        invalid_bridge_url = (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.path.rstrip("/") != "/v1"
            or parsed.query
            or parsed.fragment
        )
        if invalid_bridge_url:
            raise ValueError("Codex bridge must use an explicit IPv4 loopback /v1 URL")
        token_value = descriptor.get("token_path")
        if not isinstance(token_value, str):
            raise ValueError("Codex bridge token path is missing")
        token_path = _regular_file(token_value, "descriptor token_path")
        if token_path.parent != engine_directory:
            raise ValueError("Codex bridge token is outside the isolated engine directory")
        engine_token = credential.read_text(encoding="utf-8").strip()
        bridge_token = token_path.read_text(encoding="utf-8").strip()
        if not engine_token or "\n" in engine_token or "\r" in engine_token:
            raise ValueError("engine credential is empty or invalid")
        if not bridge_token or "\n" in bridge_token or "\r" in bridge_token:
            raise ValueError("Codex bridge token is empty or invalid")
        required = (
            "provider_id",
            "model",
            "billing_description",
            "registration_id",
            "connection_id",
        )
        for field in required:
            if not isinstance(descriptor.get(field), str) or not descriptor[field].strip():
                raise ValueError(f"Codex bridge descriptor is missing {field}")
        display_name = descriptor.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = str(descriptor["model"])
        return cls(
            rendezvous,
            credential,
            descriptor_path,
            engine_token,
            bridge_token,
            str(base_url),
            str(descriptor["provider_id"]),
            str(descriptor["model"]),
            display_name,
            str(descriptor["billing_description"]),
            str(descriptor["registration_id"]),
            str(descriptor["connection_id"]),
        )


class EngineRegisteredModelCatalog:
    """Project the active bridge registration through the public engine RPC."""

    def __init__(
        self,
        engine: Any,
        configuration: DesktopAttachmentConfiguration,
        route_adapter: Any | None = None,
    ) -> None:
        self._engine = engine
        self._configuration = configuration
        self._route_adapter = route_adapter
        self._selection: dict[str, Any] = {}

    def load_models(self) -> dict[str, dict[str, Any]]:
        response = self._engine.call("engine.v1.models.list", {"collection": "registered"})
        items = response.get("items", []) if isinstance(response, dict) else []
        models: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("registration_id") != self._configuration.registration_id:
                continue
            if item.get("connection_id") != self._configuration.connection_id:
                continue
            if item.get("provider_model_id") != self._configuration.model:
                continue
            models[self._configuration.model] = dict(item)
        return models

    def catalog_entries(self, price_lines: Any = None) -> list[dict[str, Any]]:
        del price_lines
        return [
            {
                "id": model,
                "model": model,
                "displayName": (
                    item.get("display_name") or self._configuration.display_name
                ),
                "description": self._configuration.billing_description,
                "hidden": False,
                "isDefault": self._selection.get("model") == model,
                "defaultReasoningEffort": "low",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "Low"}
                ],
                "inputModalities": ["text"],
                "supportsPersonality": False,
            }
            for model, item in self.load_models().items()
        ]

    def selected(self) -> dict[str, Any]:
        return dict(self._selection)

    def select(self, model: str, effort: Any = None) -> dict[str, Any]:
        if model not in self.load_models() and not model.startswith("gpt-"):
            raise BridgeError(
                "Choose a registered Model Deck V2 model or an available OpenAI model."
            )
        self._selection = {"model": model, "effort": effort}
        return dict(self._selection)

    def desktop_route(self, model: str) -> dict[str, Any] | None:
        if self._route_adapter is None or model not in self.load_models():
            return None
        return self._route_adapter.route(model)

    def desktop_route_for_provider(self, provider: str) -> dict[str, Any] | None:
        if provider != DESKTOP_PROVIDER or self._route_adapter is None:
            return None
        return self._route_adapter.route(self._configuration.model)


class V2DesktopRouteAdapter:
    """Per-process Codex provider definition for the V2 Responses bridge."""

    def __init__(self, configuration: DesktopAttachmentConfiguration) -> None:
        self._configuration = configuration

    def route(self, model: str) -> dict[str, Any]:
        if model != self._configuration.model:
            raise BridgeError(
                "Register this model in the active Model Deck V2 bridge first."
            )
        definition = {
            "name": "Model Deck V2",
            "base_url": self._configuration.bridge_base_url,
            "wire_api": "responses",
            "requires_openai_auth": False,
            "supports_websockets": False,
            "request_max_retries": 0,
            "stream_max_retries": 0,
            "env_key": BRIDGE_TOKEN_ENV,
        }
        return {
            "provider": DESKTOP_PROVIDER,
            "billing": self._configuration.billing_description,
            "config": {"model_providers": {DESKTOP_PROVIDER: definition}},
        }

    def codex_arguments(self) -> list[str]:
        return ["-c", "features.enable_request_compression=false"]

    def wait_for_catalog(self, timeout: float) -> bool:
        del timeout
        return True

    def cancel_cursor_turn(self, thread_id: str, turn_id: str | None = None) -> None:
        del thread_id, turn_id

    def price_table(self) -> dict[str, Any]:
        return {}

    def benchmark_lines(self) -> dict[str, Any]:
        return {}

    def remember_thread(self, thread_id: str, cwd: str) -> None:
        del thread_id, cwd

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


def run_desktop_attachment(
    arguments: list[str],
    environment: Mapping[str, str],
    emit: Any,
    process_launcher: Any | None = None,
    *,
    engine: Any | None = None,
    router: Any | None = None,
) -> AppServerBridge:
    """Compose V2 state with the prototype's package-owned app-server bridge."""
    del arguments
    configuration = DesktopAttachmentConfiguration.load(environment)
    if engine is None:
        raise ValueError("Desktop attachment requires an injected engine client")
    active_engine = engine
    route_adapter = V2DesktopRouteAdapter(configuration)
    catalog = EngineRegisteredModelCatalog(active_engine, configuration, route_adapter)
    options: dict[str, Any] = {}
    if process_launcher is not None:
        options["process_launcher"] = process_launcher
    active_router = router if router is not None else route_adapter
    return AppServerBridge(catalog, emit, active_router, **options)


def _emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main(arguments: list[str] | None = None, *, engine: Any | None = None) -> int:
    argv = list(sys.argv[1:] if arguments is None else arguments)
    if "app-server" not in argv:
        from .runtime import discover_runtime
        executable = str(discover_runtime().executable_path)
        os.execv(executable, [executable, *argv])
    configuration = DesktopAttachmentConfiguration.load(os.environ)
    os.environ[BRIDGE_TOKEN_ENV] = configuration.bridge_token
    bridge = run_desktop_attachment(argv, os.environ, _emit, engine=engine)
    asyncio.run(bridge.run(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DesktopAttachmentConfiguration",
    "EngineRegisteredModelCatalog",
    "V2DesktopRouteAdapter",
    "run_desktop_attachment",
]
