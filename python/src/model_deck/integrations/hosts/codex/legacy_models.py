from __future__ import annotations

import json
import tomllib
import uuid
from pathlib import Path

from model_deck.engine.model_library.ports import ModelRepository, RegisteredModelRecord

AGENT_MARKER = "# Managed by OpenRouter Settings native-agent registration v1"
PROVIDER = "openrouter-settings"
_LEGACY_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


class LegacyModelsError(ValueError):
    pass


def _reject_symlinks(path: Path) -> None:
    if path.is_symlink():
        raise LegacyModelsError(f"symlink refused: {path}")


def _stable_registration_id(provider_model_id: str, role: str) -> str:
    return str(uuid.uuid5(_LEGACY_NAMESPACE, f"{role}:{provider_model_id}"))


class LegacyCodexModelRepository(ModelRepository):
    """Read-only managed openrouter_*.toml from explicit fixture directories only."""

    def __init__(
        self,
        agents_dir: Path,
        *,
        default_connection_id: str,
        display_names_path: Path | None = None,
    ) -> None:
        self._agents_dir = agents_dir
        self._default_connection_id = default_connection_id
        self._display_names_path = display_names_path

    def list_registered(self, *, connection_id: str | None = None) -> list[RegisteredModelRecord]:
        if connection_id is not None and connection_id != self._default_connection_id:
            return []
        names = self._load_display_names()
        rows: list[RegisteredModelRecord] = []
        _reject_symlinks(self._agents_dir)
        if not self._agents_dir.is_dir():
            return []
        for path in sorted(self._agents_dir.glob("openrouter_*.toml")):
            _reject_symlinks(path)
            source = path.read_text(encoding="utf-8")
            if not source.startswith(AGENT_MARKER + "\n"):
                continue
            try:
                agent = tomllib.loads(source)
            except tomllib.TOMLDecodeError as exc:
                raise LegacyModelsError(f"invalid managed agent: {path.name}") from exc
            model = agent.get("model")
            role = agent.get("name")
            if not isinstance(model, str) or not isinstance(role, str):
                raise LegacyModelsError(f"managed agent missing model/name: {path.name}")
            if agent.get("model_provider") != PROVIDER:
                raise LegacyModelsError(f"unexpected provider in {path.name}")
            display = names.get(model) or model.split("/", 1)[-1]
            rows.append(
                RegisteredModelRecord(
                    registration_id=_stable_registration_id(model, role),
                    provider_model_id=model,
                    connection_id=self._default_connection_id,
                    display_name=display,
                    revision=1,
                )
            )
        return rows

    def _load_display_names(self) -> dict[str, str]:
        if self._display_names_path is None or not self._display_names_path.is_file():
            return {}
        _reject_symlinks(self._display_names_path)
        try:
            payload = json.loads(self._display_names_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LegacyModelsError("cannot read display names fixture") from exc
        if not isinstance(payload, dict):
            return {}
        return {str(k): str(v) for k, v in payload.items() if isinstance(k, str) and isinstance(v, str)}
