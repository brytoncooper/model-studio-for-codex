from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from model_deck.engine.projections.ports import ProjectionOutboxEvent
from model_deck.integrations.hosts.codex.agent_renderer.renderer import (
    RenderError,
    RenderRequest,
    render_managed_agent,
)
from model_deck.integrations.hosts.codex.agent_materializer.resolution import (
    ConnectionSnapshot,
    ModelSnapshot,
)
from model_deck.integrations.hosts.codex.projection_consumer.consumer import (
    CodexProjectionMaterializationError,
    CodexProjectionWrite,
)


@dataclass(frozen=True, slots=True)
class AgentMaterializerSettings:
    """Explicit caller-supplied paths. No home discovery, no I/O."""

    agents_rel_dir: str
    token_helper_path: str


def _settings_valid(settings: AgentMaterializerSettings) -> bool:
    rel = settings.agents_rel_dir
    if not isinstance(rel, str) or not rel or rel.startswith("/"):
        return False
    parts = Path(rel).parts
    if not parts or any(part in (".", "..") or not part for part in parts):
        return False
    helper = settings.token_helper_path
    if not isinstance(helper, str) or not Path(helper).is_absolute():
        return False
    return not any(ord(c) < 32 for c in helper)


class AgentMaterializer:
    """Actual CodexProjectionMaterializer: snapshot validation plus real renderer."""

    def __init__(
        self,
        connections: ConnectionSnapshot,
        models: ModelSnapshot,
        settings: AgentMaterializerSettings,
        renderer: Callable[[RenderRequest], object] = render_managed_agent,
    ) -> None:
        self._connections = connections
        self._models = models
        self._settings = settings
        self._renderer = renderer

    def materialize(self, event: ProjectionOutboxEvent) -> CodexProjectionWrite:
        try:
            payload = json.loads(event.payload_json)
        except (ValueError, TypeError) as exc:
            raise CodexProjectionMaterializationError("event payload unreadable") from exc
        if not isinstance(payload, dict):
            raise CodexProjectionMaterializationError("event payload unreadable")
        try:
            connection_id = payload["connection_id"]
            provider_model_id = payload["provider_model_id"]
            display_name = payload["display_name"]
            registration_id = payload["registration_id"]
            revision = payload["revision"]
        except KeyError as exc:
            raise CodexProjectionMaterializationError("event payload incomplete") from exc
        if registration_id != event.aggregate_id or revision != event.aggregate_revision:
            raise CodexProjectionMaterializationError("event identity mismatch")
        if not self._settings_valid_wrap():
            raise CodexProjectionMaterializationError("materializer settings invalid")
        try:
            model = self._models.lookup(event.aggregate_id)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise CodexProjectionMaterializationError("model snapshot unavailable") from None
        if model is None:
            raise CodexProjectionMaterializationError("model snapshot missing")
        if (
            model.registration_id != event.aggregate_id
            or model.connection_id != connection_id
            or model.provider_model_id != provider_model_id
            or model.display_name != display_name
            or model.revision != event.aggregate_revision
        ):
            raise CodexProjectionMaterializationError("model snapshot mismatch")
        try:
            connection = self._connections.lookup(connection_id)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise CodexProjectionMaterializationError("connection snapshot unavailable") from None
        if connection is None:
            raise CodexProjectionMaterializationError("connection snapshot missing")
        if connection.connection_id != connection_id:
            raise CodexProjectionMaterializationError("connection snapshot mismatch")
        if connection.kind == "endpoint":
            request = RenderRequest(
                kind="endpoint",
                provider_model_id=model.provider_model_id,
                display_name=model.display_name,
                endpoint_name=connection.endpoint_name,
                base_url=connection.base_url,
                credential_account_id=connection.credential_account_id,
                token_helper_path=self._settings.token_helper_path,
                reasoning_effort=None,
                billing_description=connection.billing_description,
            )
        elif connection.kind == "subscription":
            request = RenderRequest(
                kind="subscription",
                provider_model_id=model.provider_model_id,
                display_name=model.display_name,
            )
        else:
            raise CodexProjectionMaterializationError("connection kind unknown")
        try:
            rendered = self._renderer(request)
        except (KeyboardInterrupt, SystemExit):
            raise
        except RenderError:
            raise CodexProjectionMaterializationError("render invalid") from None
        except Exception:
            raise CodexProjectionMaterializationError("render unavailable") from None
        filename = getattr(rendered, "filename", None)
        content = getattr(rendered, "content", None)
        if not isinstance(filename, str) or not isinstance(content, bytes):
            raise CodexProjectionMaterializationError("render output invalid")
        if "/" in filename or filename in (".", "..") or not filename.endswith(".toml"):
            raise CodexProjectionMaterializationError("render filename unsafe")
        path = Path(self._settings.agents_rel_dir) / filename
        if path.is_absolute() or any(part in (".", "..") for part in path.parts):
            raise CodexProjectionMaterializationError("render path unsafe")
        return CodexProjectionWrite(path=path, data=content)

    def _settings_valid_wrap(self) -> bool:
        return _settings_valid(self._settings)
