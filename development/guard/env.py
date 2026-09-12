from __future__ import annotations

import os
from pathlib import Path


def isolated_subprocess_env(
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path | None = None,
) -> dict[str, str]:
    state_root.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)
    tmp = state_root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    codex_home = state_root / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {}
    for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env["HOME"] = str(state_root)
    env["TMPDIR"] = str(tmp)
    env["USERPROFILE"] = str(state_root)
    env["MODEL_DECK_STATE_ROOT"] = str(state_root)
    env["MODEL_DECK_ARTIFACT_ROOT"] = str(artifact_root)
    env["CODEX_HOME"] = str(codex_home)
    env["XDG_CONFIG_HOME"] = str(state_root / ".config")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if socket_root is not None:
        socket_root.mkdir(parents=True, exist_ok=True)
        env["MODEL_DECK_SOCKET_ROOT"] = str(socket_root)
    return env
