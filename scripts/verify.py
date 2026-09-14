#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_SCRIPT_ROOT = Path(__file__).resolve()
_REPO_ROOT = _SCRIPT_ROOT.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from development.guard.env import isolated_subprocess_env
from development.guard.paths import DevelopmentGuardError, repo_root_from_script
from development.guard.roots import validate_isolated_roots

GATES = (
    "development-guard",
    "contracts",
    "engine",
    "swift",
    "migration",
    "providers",
    "extensions",
    "package",
    "all-local",
)

IMPLEMENTED_GATES = frozenset({"development-guard", "contracts"})
PENDING_GATES = tuple(g for g in GATES if g not in IMPLEMENTED_GATES and g != "all-local")

EXIT_GUARD_FAILURE = 1
EXIT_ROOT_REJECT = 2
EXIT_INCOMPLETE = 3
EXIT_UNIMPLEMENTED = 3


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Model Deck verification gates")
    parser.add_argument("gate", choices=GATES)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--socket-root", type=Path, default=None)
    return parser.parse_args(argv)


def repository_root() -> Path:
    return repo_root_from_script(_SCRIPT_ROOT)


def ensure_roots(args: argparse.Namespace) -> tuple[Path, Path, Path | None]:
    source = repository_root()
    return validate_isolated_roots(
        args.state_root,
        args.artifact_root,
        args.socket_root,
        source_root=source,
    )


def run_development_guard(
    root: Path,
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path | None,
) -> int:
    env = isolated_subprocess_env(
        state_root=state_root,
        artifact_root=artifact_root,
        socket_root=socket_root,
    )
    command = [
        sys.executable,
        "-B",
        "-m",
        "unittest",
        "test_development_guard",
        "test_editing_check",
    ]
    completed = subprocess.run(command, cwd=str(root), env=env, check=False)
    return completed.returncode


def run_contracts(
    root: Path,
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path | None,
) -> int:
    # Run the non-mutating contracts check inside an isolated subprocess
    # environment. Always invokes `generate_contracts.py --check`; never
    # propagates writes through this gate.
    env = isolated_subprocess_env(
        state_root=state_root,
        artifact_root=artifact_root,
        socket_root=socket_root,
    )
    command = [
        sys.executable,
        "-B",
        "scripts/generate_contracts.py",
        "--check",
    ]
    completed = subprocess.run(command, cwd=str(root), env=env, check=False)
    return completed.returncode


def run_all_local(
    root: Path,
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path | None,
) -> int:
    g0 = run_development_guard(
        root,
        state_root=state_root,
        artifact_root=artifact_root,
        socket_root=socket_root,
    )
    if g0 != 0:
        return g0
    pending = ", ".join(PENDING_GATES)
    print(
        f"verify: all-local incomplete; implemented gates passed (development-guard) "
        f"but these gates are not implemented yet: {pending}",
        file=sys.stderr,
    )
    return EXIT_INCOMPLETE


def run_unimplemented(gate: str) -> int:
    print(f"verify: gate {gate} is not implemented yet", file=sys.stderr)
    return EXIT_UNIMPLEMENTED


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    try:
        args = parse_args(argv)
        state_root, artifact_root, socket_root = ensure_roots(args)
    except DevelopmentGuardError as exc:
        print(f"verify: {exc}", file=sys.stderr)
        return EXIT_ROOT_REJECT

    root = repository_root()
    if args.gate == "development-guard":
        return run_development_guard(
            root,
            state_root=state_root,
            artifact_root=artifact_root,
            socket_root=socket_root,
        )
    if args.gate == "contracts":
        return run_contracts(
            root,
            state_root=state_root,
            artifact_root=artifact_root,
            socket_root=socket_root,
        )
    if args.gate == "all-local":
        return run_all_local(
            root,
            state_root=state_root,
            artifact_root=artifact_root,
            socket_root=socket_root,
        )
    return run_unimplemented(args.gate)


if __name__ == "__main__":
    raise SystemExit(main())
