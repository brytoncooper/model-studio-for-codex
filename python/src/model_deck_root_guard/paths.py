from typing import Optional

import os
import pwd
from pathlib import Path


class IsolatedRootGuardError(ValueError):
    """A development path violates protected-runtime rules."""


def actual_user_home() -> Path:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except (KeyError, OSError):
        return Path.home().resolve()


def repo_root_from_script(script_path: Path) -> Path:
    anchor = script_path.resolve().parent
    for candidate in (anchor, *anchor.parents):
        if (candidate / "local_router.py").is_file() and (candidate / "build.sh").is_file():
            return candidate
    raise IsolatedRootGuardError(
        f"Could not locate Model Deck repository root from {script_path}"
    )


def repo_root(start: Optional[Path] = None) -> Path:
    if start is None:
        raise IsolatedRootGuardError(
            "repo_root requires an explicit anchor; scripts must use repo_root_from_script(__file__)"
        )
    anchor = start.resolve()
    for candidate in (anchor, *anchor.parents):
        if (candidate / "local_router.py").is_file() and (candidate / "build.sh").is_file():
            return candidate
    return anchor


def protected_roots(source_root: Optional[Path] = None) -> tuple[Path, ...]:
    if source_root is None:
        raise IsolatedRootGuardError("protected_roots requires source_root from repo_root_from_script")
    root = source_root.resolve()
    home = actual_user_home()
    support = home / "Library" / "Application Support"
    candidates = (
        root.parent / "Model Deck.app",
        support / "Model Deck",
        support / "Codex OpenRouter",
        support / "Model Deck" / "cursor-sdk",
        home / ".codex",
        Path("/Applications") / "Model Deck.app",
        Path("/Applications") / "OpenRouter Settings.app",
    )
    resolved: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            item = path.resolve()
        except OSError:
            item = path
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(item)
    return tuple(resolved)


def canonicalize_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError as exc:
        raise IsolatedRootGuardError(f"Cannot resolve path: {path}") from exc


def is_protected_path(path: Path, *, source_root: Path) -> bool:
    candidate = canonicalize_path(path)
    for protected in protected_roots(source_root):
        try:
            protected_resolved = protected.resolve()
        except OSError:
            protected_resolved = protected
        if candidate == protected_resolved:
            return True
        try:
            candidate.relative_to(protected_resolved)
            return True
        except ValueError:
            continue
    return False


def path_contains_protected_descendant(path: Path, *, source_root: Path) -> bool:
    candidate = canonicalize_path(path)
    for protected in protected_roots(source_root):
        try:
            protected_resolved = protected.resolve()
        except OSError:
            protected_resolved = protected
        if protected_resolved == candidate:
            return True
        try:
            protected_resolved.relative_to(candidate)
            return True
        except ValueError:
            continue
    return False


def legitimate_temp_symlink(path: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if path == Path("/tmp") and str(resolved) == "/private/tmp":
        return True
    if path == Path("/var") and str(resolved) == "/private/var":
        return True
    return False


def permitted_work_subpaths(source_root: Path) -> tuple[Path, ...]:
    root = source_root.resolve()
    return (
        root / "work" / "worktrees",
        root / "work" / "isolated",
    )


def is_permitted_work_subpath(path: Path, *, source_root: Path) -> bool:
    candidate = canonicalize_path(path)
    for prefix in permitted_work_subpaths(source_root):
        try:
            prefix_resolved = prefix.resolve()
        except OSError:
            prefix_resolved = prefix
        if candidate == prefix_resolved:
            return True
        try:
            candidate.relative_to(prefix_resolved)
            return True
        except ValueError:
            continue
    return False


def is_model_deck_source_root(path: Path) -> bool:
    candidate = canonicalize_path(path)
    return (candidate / "local_router.py").is_file() and (candidate / "build.sh").is_file()


def reject_source_tree_usage(path: Path, *, label: str, source_root: Path) -> None:
    candidate = canonicalize_path(path)
    source = source_root.resolve()
    if is_model_deck_source_root(candidate):
        if candidate == source:
            raise IsolatedRootGuardError(
                f"{label} must not use the active source checkout root: {candidate}"
            )
        raise IsolatedRootGuardError(
            f"{label} must not use another source checkout root: {candidate}"
        )

    containing_source: Optional[Path] = None
    for ancestor in (candidate, *candidate.parents):
        if is_model_deck_source_root(ancestor):
            containing_source = ancestor
            break

    if containing_source is None:
        return

    if containing_source != source:
        raise IsolatedRootGuardError(
            f"{label} must not use paths inside another Model Deck source checkout: {candidate}"
        )

    if not is_permitted_work_subpath(candidate, source_root=source_root):
        raise IsolatedRootGuardError(
            f"{label} must not use paths inside the source checkout except work/ subpaths: {candidate}"
        )


def reject_symlink_parent_aliases(path: Path, *, label: str) -> None:
    current = path
    while True:
        parent = current.parent
        if parent == current:
            break
        if parent.is_symlink() and not legitimate_temp_symlink(parent):
            raise IsolatedRootGuardError(
                f"{label} parent path is a symlink alias: {parent}"
            )
        current = parent


def reject_protected_path(path: Path, *, label: str, source_root: Path) -> Path:
    reject_symlink_parent_aliases(path, label=label)
    reject_source_tree_usage(path, label=label, source_root=source_root)
    candidate = canonicalize_path(path)
    if is_protected_path(candidate, source_root=source_root):
        raise IsolatedRootGuardError(
            f"{label} points at protected runtime output or live state: {candidate}"
        )
    if path_contains_protected_descendant(candidate, source_root=source_root):
        raise IsolatedRootGuardError(
            f"{label} is too broad and contains protected runtime paths beneath it: {candidate}"
        )
    return candidate


def unittest_module_name(test_file: Path, repo: Path) -> str:
    rel = test_file.resolve().relative_to(repo.resolve()).with_suffix("")
    return ".".join(rel.parts)
