from __future__ import annotations

from pathlib import Path

from development.guard.paths import (
    DevelopmentGuardError,
    canonicalize_path,
    reject_protected_path,
    reject_symlink_parent_aliases,
)


def validate_isolated_roots(
    state_root: Path | str,
    artifact_root: Path | str,
    socket_root: Path | str | None = None,
    *,
    source_root: Path,
) -> tuple[Path, Path, Path | None]:
    state = Path(state_root)
    artifact = Path(artifact_root)
    if not state.is_absolute() or not artifact.is_absolute():
        raise DevelopmentGuardError("state_root and artifact_root must be absolute paths")

    state_real = _require_directory_root(state, label="state_root", source_root=source_root)
    artifact_real = _require_directory_root(artifact, label="artifact_root", source_root=source_root)

    if state_real == artifact_real:
        raise DevelopmentGuardError("state_root and artifact_root must not be the same path")

    if _one_contains_other(state_real, artifact_real):
        raise DevelopmentGuardError(
            "state_root and artifact_root must not nest inside one another"
        )

    socket_real: Path | None = None
    if socket_root is not None:
        socket = Path(socket_root)
        if not socket.is_absolute():
            raise DevelopmentGuardError("socket_root must be an absolute path when provided")
        socket_real = _require_directory_root(socket, label="socket_root", source_root=source_root)
        if socket_real in (state_real, artifact_real) or _one_contains_other(state_real, socket_real) or _one_contains_other(artifact_real, socket_real):
            raise DevelopmentGuardError("socket_root must be disjoint from state_root and artifact_root")

    return state_real, artifact_real, socket_real


def _require_directory_root(path: Path, *, label: str, source_root: Path) -> Path:
    if path.is_symlink():
        raise DevelopmentGuardError(f"{label} must not be a symlink alias: {path}")
    reject_symlink_parent_aliases(path, label=label)
    if path.exists() and not path.is_dir():
        raise DevelopmentGuardError(f"{label} must be a directory, not an existing file: {path}")
    resolved = reject_protected_path(path, label=label, source_root=source_root)
    if resolved.is_symlink():
        raise DevelopmentGuardError(f"{label} resolves through a symlink alias: {path}")
    return resolved


def _one_contains_other(left: Path, right: Path) -> bool:
    left_c = canonicalize_path(left)
    right_c = canonicalize_path(right)
    try:
        left_c.relative_to(right_c)
        return True
    except ValueError:
        pass
    try:
        right_c.relative_to(left_c)
        return True
    except ValueError:
        return False
