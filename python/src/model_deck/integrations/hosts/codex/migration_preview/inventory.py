from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SourceBytes:
    path: str
    data: bytes | None
    sha256: str | None
    is_symlink: bool


def posix_relative(root: Path, target: Path) -> str:
    return target.relative_to(root).as_posix()


def lstat_is_symlink(path: Path) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError:
        return False


def fixture_root_path(raw: Path) -> Path:
    path = Path(os.fspath(raw))
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.normpath(path))
    assert_fixture_root_safe(path)
    return path


def assert_fixture_root_safe(root: Path) -> None:
    current = Path(root.anchor)
    for component in root.parts[1:]:
        current /= component
        if lstat_is_symlink(current):
            raise ValueError("fixture root path cannot contain a symlink")
    if not root.is_dir():
        raise ValueError("fixture root must be an existing directory")


def read_source_bytes(root: Path, relative_path: str) -> SourceBytes:
    target = root / relative_path
    if lstat_is_symlink(target):
        return SourceBytes(path=relative_path, data=None, sha256=None, is_symlink=True)
    if not target.is_file():
        return SourceBytes(path=relative_path, data=None, sha256=None, is_symlink=False)
    data = target.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    return SourceBytes(path=relative_path, data=data, sha256=digest, is_symlink=False)


def list_agent_files(root: Path) -> tuple[tuple[str, bool], ...]:
    agents_dir = root / "agents"
    if lstat_is_symlink(agents_dir):
        return (("agents", True),)
    if not agents_dir.is_dir():
        return ()
    rows: list[tuple[str, bool]] = []
    for entry in sorted(agents_dir.iterdir(), key=lambda item: item.name):
        rel = posix_relative(root, entry)
        rows.append((rel, lstat_is_symlink(entry)))
    return tuple(rows)


def json_store_version(document: object) -> str | int | None:
    if not isinstance(document, dict):
        return None
    for key in ("version", "schema_version"):
        value = document.get(key)
        if type(value) is int:
            return value
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unversioned"
