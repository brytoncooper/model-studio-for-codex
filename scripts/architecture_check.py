#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve()
_REPO_ROOT = _SCRIPT.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from development.architecture.baseline import load_baseline
from development.architecture.checker import ArchitectureChecker
from development.guard.paths import repo_root_from_script

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_USAGE = 2


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Static Python import architecture checks (AST only; not a runtime sandbox)."
        )
    )
    parser.add_argument(
        "--roots",
        nargs="+",
        type=Path,
        default=[Path("python/src")],
        help="Directories or files to scan (default: python/src)",
    )
    parser.add_argument(
        "--include-baseline-legacy",
        action="store_true",
        help="Also scan repo-root modules listed in baseline_legacy.json",
    )
    parser.add_argument(
        "--fail-on-warnings",
        action="store_true",
        help="Treat baseline report-only violations as failures",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv if argv is not None else sys.argv[1:]))
    repo_root = repo_root_from_script(_SCRIPT)
    roots: list[Path] = []
    for root in args.roots:
        candidate = root if root.is_absolute() else (repo_root / root)
        roots.append(candidate)
    if args.include_baseline_legacy:
        for entry in load_baseline():
            roots.append(repo_root / entry.relpath)

    checker = ArchitectureChecker(repo_root=repo_root)
    files = checker.discover_python_files(roots)
    if not files:
        print("architecture_check: no Python files discovered", file=sys.stderr)
        return EXIT_USAGE

    result = checker.check_paths(files)
    failing = checker.failing_violations(result)
    warnings = [v for v in result.violations if v.severity == "warning"]

    for violation in result.violations:
        location = f"{violation.path}"
        if violation.line is not None:
            location += f":{violation.line}"
        print(f"{violation.severity.upper()} [{violation.rule_id}] {location}: {violation.message}")

    print(
        f"architecture_check: checked {result.files_checked} file(s), "
        f"{len(failing)} error(s), {len(warnings)} warning(s)",
        file=sys.stderr,
    )

    if failing:
        return EXIT_VIOLATION
    if args.fail_on_warnings and warnings:
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
