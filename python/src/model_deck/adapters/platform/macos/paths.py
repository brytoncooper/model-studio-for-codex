from __future__ import annotations

from pathlib import Path

from model_deck_contracts.ports import ApplicationPaths


class IsolatedApplicationPaths(ApplicationPaths):
    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root

    def state_root(self) -> Path:
        return self._state_root

    def cache_root(self) -> Path:
        return self._state_root / "cache"

    def plugins_root(self) -> Path:
        return self._state_root / "plugins"
