#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
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

IMPLEMENTED_GATES = frozenset(
    {
        "development-guard",
        "contracts",
        "engine",
        "swift",
        "migration",
        "providers",
        "extensions",
    }
)
PENDING_GATES = tuple(g for g in GATES if g not in IMPLEMENTED_GATES and g != "all-local")

ALL_LOCAL_GATES = (
    "development-guard",
    "contracts",
    "engine",
    "swift",
    "migration",
    "providers",
    "extensions",
)

EXIT_GUARD_FAILURE = 1
EXIT_ROOT_REJECT = 2
EXIT_ENVIRONMENT_REJECT = 2
EXIT_INCOMPLETE = 3
EXIT_UNIMPLEMENTED = 3

# Every Python gate runs from `python/` with the engine reached through
# PYTHONPATH only. The process-isolation fixtures spawn `sys.executable -I -c`
# children that must not be able to import `model_deck`; an installed or
# editable distribution breaks that acceptance proof.
PYTHON_PACKAGE_DIRECTORY = "python"
ISOLATION_PROBE = (
    "import importlib.util; "
    "print('present' if importlib.util.find_spec('model_deck') else 'absent')"
)
ISOLATION_REMEDIATION = "uv pip uninstall --python python/.venv/bin/python model-deck"

ENGINE_TEST_DIRECTORIES = (
    "tests/engine",
    "tests/kernel",
    "tests/contracts",
    "tests/scripts",
    "tests/host_codex",
    "tests/integrations",
)
PROVIDER_TEST_DIRECTORIES = (
    "tests/provider_openai_compatible",
    "tests/provider_cursor",
    "tests/providers_continuation",
)
EXTENSION_TEST_DIRECTORIES = ("tests/plugins",)
MIGRATION_SOURCE_DIRECTORIES = ("tests/engine", "tests/integrations")
MIGRATION_FILENAME_KEYWORDS = (
    "repository",
    "outbox",
    "projection",
    "migration",
    "recovery",
    "schema",
    "upgrade",
)

SWIFT_PACKAGE_DIRECTORY = "macos"

_RAN_LINE = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
_FAILED_LINE = re.compile(r"^FAILED \(([^)]*)\)", re.MULTILINE)
_FAILURE_COUNT = re.compile(r"(failures|errors)=(\d+)")
_SWIFT_SUMMARY = re.compile(r"Executed (\d+) tests?, with (\d+) failures?")


@dataclass(frozen=True)
class StepResult:
    label: str
    returncode: int
    tests: int
    failures: int
    errors: int
    seconds: float


@dataclass(frozen=True)
class GateResult:
    name: str
    returncode: int
    tests: int
    failures: int
    errors: int
    seconds: float
    status: str


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


def isolated_import_complaint() -> str | None:
    """Return a remediation message when `python -I` can still see the engine."""
    probe = subprocess.run(
        [sys.executable, "-I", "-c", ISOLATION_PROBE],
        capture_output=True,
        text=True,
        check=False,
    )
    verdict = probe.stdout.strip()
    if verdict == "absent":
        return None
    detail = verdict or probe.stderr.strip() or "probe produced no output"
    return (
        "verify: model_deck is reachable from an isolated interpreter "
        f"({detail}); the process-isolation fixtures cannot prove that external "
        "code runs without private engine imports. Remove the editable install "
        f"and run the gates with PYTHONPATH=src: {ISOLATION_REMEDIATION}"
    )


def python_gate_env(
    root: Path,
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path | None,
) -> dict[str, str]:
    env = isolated_subprocess_env(
        state_root=state_root,
        artifact_root=artifact_root,
        socket_root=socket_root,
    )
    # The engine is imported from the source tree, never from site-packages.
    env["PYTHONPATH"] = str(root / PYTHON_PACKAGE_DIRECTORY / "src")
    # Codex migration-preview fixtures refuse a fixture root that resolves
    # through a symlink; children inherit this resolved temporary directory.
    env["TMPDIR"] = os.path.realpath(env["TMPDIR"])
    return env


def discover_test_modules(package_root: Path, relative_directory: str) -> tuple[str, ...]:
    """Dotted module names for every `test_*.py` under a test directory."""
    directory = package_root / relative_directory
    if not directory.is_dir():
        return ()
    modules: list[str] = []
    for path in sorted(directory.rglob("test_*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package_root).with_suffix("")
        modules.append(".".join(relative.parts))
    return tuple(modules)


def is_migration_module(module_name: str) -> bool:
    """True when the module filename names migration/recovery bookkeeping."""
    filename = module_name.rsplit(".", 1)[-1].lower()
    return any(keyword in filename for keyword in MIGRATION_FILENAME_KEYWORDS)


def migration_test_modules(package_root: Path) -> tuple[str, ...]:
    selected: list[str] = []
    for relative_directory in MIGRATION_SOURCE_DIRECTORIES:
        for module in discover_test_modules(package_root, relative_directory):
            if is_migration_module(module):
                selected.append(module)
    return tuple(selected)


def example_test_targets(root: Path) -> tuple[tuple[str, Path, tuple[str, ...]], ...]:
    """(label, working directory, module names) for each `examples/*/tests`."""
    examples = root / "examples"
    if not examples.is_dir():
        return ()
    targets: list[tuple[str, Path, tuple[str, ...]]] = []
    for example in sorted(path for path in examples.iterdir() if path.is_dir()):
        tests_directory = example / "tests"
        if not tests_directory.is_dir():
            continue
        modules: list[str] = []
        for path in sorted(tests_directory.rglob("test_*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(example).with_suffix("")
            modules.append(".".join(relative.parts))
        if modules:
            targets.append((f"examples/{example.name}/tests", example, tuple(modules)))
    return tuple(targets)


def _run_captured(command: list[str], *, cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    """Run a gate step, echo its merged output and return (exit code, output).

    The output goes to a temporary file rather than a pipe: a leaked grandchild
    that inherits a pipe would otherwise keep this runner blocked long after the
    direct child exited.
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as sink:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
        sink.seek(0)
        output = sink.read()
    sys.stdout.write(output)
    sys.stdout.flush()
    return completed.returncode, output


def _report_step(result: StepResult) -> StepResult:
    print(
        f"verify:   step {result.label}: tests={result.tests} "
        f"failures={result.failures} errors={result.errors} "
        f"seconds={result.seconds:.1f} rc={result.returncode}",
        flush=True,
    )
    return result


def run_unittest_step(
    label: str,
    *,
    cwd: Path,
    modules: tuple[str, ...],
    env: dict[str, str],
) -> StepResult:
    command = [sys.executable, "-B", "-m", "unittest", *modules]
    started = time.monotonic()
    returncode, output = _run_captured(command, cwd=cwd, env=env)
    seconds = time.monotonic() - started
    tests = 0
    failures = 0
    errors = 0
    ran = _RAN_LINE.search(output)
    if ran is not None:
        tests = int(ran.group(1))
    failed = _FAILED_LINE.search(output)
    if failed is not None:
        for name, value in _FAILURE_COUNT.findall(failed.group(1)):
            if name == "failures":
                failures += int(value)
            else:
                errors += int(value)
    if returncode != 0 and failures == 0 and errors == 0:
        # Import failures and crashes never reach the `FAILED (...)` summary.
        errors = 1
    return _report_step(StepResult(label, returncode, tests, failures, errors, seconds))


def run_command_step(
    label: str,
    *,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    summary: re.Pattern[str] | None = None,
) -> StepResult:
    started = time.monotonic()
    returncode, output = _run_captured(command, cwd=cwd, env=env)
    seconds = time.monotonic() - started
    tests = 0
    failures = 0
    if summary is not None:
        matches = summary.findall(output)
        if matches:
            tests = int(matches[-1][0])
            failures = int(matches[-1][1])
    errors = 1 if returncode != 0 and failures == 0 else 0
    return _report_step(StepResult(label, returncode, tests, failures, errors, seconds))


def missing_directory_step(label: str) -> StepResult:
    print(
        f"verify: no test modules found for {label}; the gate mapping is stale",
        file=sys.stderr,
        flush=True,
    )
    return _report_step(StepResult(label, EXIT_GUARD_FAILURE, 0, 0, 1, 0.0))


def summarize(name: str, steps: list[StepResult]) -> GateResult:
    returncode = 0
    for step in steps:
        if step.returncode != 0:
            returncode = step.returncode
            break
    status = "ok" if returncode == 0 else "failed"
    return GateResult(
        name=name,
        returncode=returncode,
        tests=sum(step.tests for step in steps),
        failures=sum(step.failures for step in steps),
        errors=sum(step.errors for step in steps),
        seconds=sum(step.seconds for step in steps),
        status=status,
    )


def report_gate(result: GateResult) -> GateResult:
    print(
        f"verify: gate={result.name} tests={result.tests} "
        f"failures={result.failures} errors={result.errors} "
        f"seconds={result.seconds:.1f} status={result.status}",
        flush=True,
    )
    return result


def environment_rejected(name: str, complaint: str) -> GateResult:
    print(complaint, file=sys.stderr, flush=True)
    return report_gate(
        GateResult(name, EXIT_ENVIRONMENT_REJECT, 0, 0, 0, 0.0, "environment-rejected")
    )


def run_development_guard(
    root: Path,
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path | None,
) -> GateResult:
    env = isolated_subprocess_env(
        state_root=state_root,
        artifact_root=artifact_root,
        socket_root=socket_root,
    )
    step = run_unittest_step(
        "test_development_guard test_editing_check",
        cwd=root,
        modules=("test_development_guard", "test_editing_check"),
        env=env,
    )
    return report_gate(summarize("development-guard", [step]))


def run_contracts(
    root: Path,
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path | None,
) -> GateResult:
    # Run the non-mutating contracts check inside an isolated subprocess
    # environment. Always invokes `generate_contracts.py --check`; never
    # propagates writes through this gate.
    env = isolated_subprocess_env(
        state_root=state_root,
        artifact_root=artifact_root,
        socket_root=socket_root,
    )
    step = run_command_step(
        "scripts/generate_contracts.py --check",
        command=[sys.executable, "-B", "scripts/generate_contracts.py", "--check"],
        cwd=root,
        env=env,
    )
    return report_gate(summarize("contracts", [step]))


def _python_directory_steps(
    package_root: Path,
    directories: tuple[str, ...],
    env: dict[str, str],
) -> list[StepResult]:
    steps: list[StepResult] = []
    for relative_directory in directories:
        modules = discover_test_modules(package_root, relative_directory)
        if not modules:
            steps.append(missing_directory_step(relative_directory))
            continue
        steps.append(
            run_unittest_step(
                relative_directory, cwd=package_root, modules=modules, env=env
            )
        )
    return steps


def run_engine(
    root: Path,
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path | None,
) -> GateResult:
    complaint = isolated_import_complaint()
    if complaint is not None:
        return environment_rejected("engine", complaint)
    env = python_gate_env(
        root,
        state_root=state_root,
        artifact_root=artifact_root,
        socket_root=socket_root,
    )
    package_root = root / PYTHON_PACKAGE_DIRECTORY
    steps = _python_directory_steps(package_root, ENGINE_TEST_DIRECTORIES, env)
    steps.append(
        run_command_step(
            "scripts/architecture_check.py",
            command=[sys.executable, "-B", "scripts/architecture_check.py"],
            cwd=root,
            env=env,
        )
    )
    return report_gate(summarize("engine", steps))


def run_migration(
    root: Path,
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path | None,
) -> GateResult:
    complaint = isolated_import_complaint()
    if complaint is not None:
        return environment_rejected("migration", complaint)
    env = python_gate_env(
        root,
        state_root=state_root,
        artifact_root=artifact_root,
        socket_root=socket_root,
    )
    package_root = root / PYTHON_PACKAGE_DIRECTORY
    modules = migration_test_modules(package_root)
    print(
        "verify: migration selects modules whose filename contains "
        + ", ".join(MIGRATION_FILENAME_KEYWORDS),
        flush=True,
    )
    for module in modules:
        print(f"verify:   selected {module}", flush=True)
    if not modules:
        return report_gate(summarize("migration", [missing_directory_step("migration")]))
    step = run_unittest_step(
        "migration modules", cwd=package_root, modules=modules, env=env
    )
    return report_gate(summarize("migration", [step]))


def run_providers(
    root: Path,
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path | None,
) -> GateResult:
    complaint = isolated_import_complaint()
    if complaint is not None:
        return environment_rejected("providers", complaint)
    env = python_gate_env(
        root,
        state_root=state_root,
        artifact_root=artifact_root,
        socket_root=socket_root,
    )
    package_root = root / PYTHON_PACKAGE_DIRECTORY
    steps = _python_directory_steps(package_root, PROVIDER_TEST_DIRECTORIES, env)
    return report_gate(summarize("providers", steps))


def run_extensions(
    root: Path,
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path | None,
) -> GateResult:
    complaint = isolated_import_complaint()
    if complaint is not None:
        return environment_rejected("extensions", complaint)
    env = python_gate_env(
        root,
        state_root=state_root,
        artifact_root=artifact_root,
        socket_root=socket_root,
    )
    package_root = root / PYTHON_PACKAGE_DIRECTORY
    steps = _python_directory_steps(package_root, EXTENSION_TEST_DIRECTORIES, env)
    for label, working_directory, modules in example_test_targets(root):
        steps.append(
            run_unittest_step(
                label, cwd=working_directory, modules=modules, env=env
            )
        )
    return report_gate(summarize("extensions", steps))


def run_swift(
    root: Path,
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path | None,
) -> GateResult:
    executable = shutil.which("swift") or "/usr/bin/swift"
    if not Path(executable).exists():
        print(
            "verify: swift toolchain unavailable; gate swift was not run",
            file=sys.stderr,
            flush=True,
        )
        return report_gate(GateResult("swift", EXIT_INCOMPLETE, 0, 0, 0, 0.0, "unavailable"))
    env = python_gate_env(
        root,
        state_root=state_root,
        artifact_root=artifact_root,
        socket_root=socket_root,
    )
    step = run_command_step(
        "swift test",
        command=[executable, "test", "--package-path", SWIFT_PACKAGE_DIRECTORY],
        cwd=root,
        env=env,
        summary=_SWIFT_SUMMARY,
    )
    return report_gate(summarize("swift", [step]))


GATE_RUNNERS = {
    "development-guard": run_development_guard,
    "contracts": run_contracts,
    "engine": run_engine,
    "swift": run_swift,
    "migration": run_migration,
    "providers": run_providers,
    "extensions": run_extensions,
}


def run_all_local(
    root: Path,
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path | None,
) -> int:
    results: list[GateResult] = []
    for gate in ALL_LOCAL_GATES:
        print(f"verify: running gate {gate}", flush=True)
        results.append(
            GATE_RUNNERS[gate](
                root,
                state_root=state_root,
                artifact_root=artifact_root,
                socket_root=socket_root,
            )
        )
    print("verify: all-local summary", flush=True)
    for result in results:
        print(
            f"verify:   {result.name:<18} tests={result.tests:<5} "
            f"failures={result.failures:<3} errors={result.errors:<3} "
            f"seconds={result.seconds:8.1f} status={result.status}",
            flush=True,
        )
    for gate in PENDING_GATES:
        print(f"verify:   {gate:<18} status=pending (B26)", flush=True)
    for result in results:
        if result.returncode != 0:
            print(
                f"verify: all-local failed; first failing gate: {result.name}",
                file=sys.stderr,
                flush=True,
            )
            return result.returncode
    print("verify: all-local passed; gate package remains pending B26", flush=True)
    return 0


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
    if args.gate == "all-local":
        return run_all_local(
            root,
            state_root=state_root,
            artifact_root=artifact_root,
            socket_root=socket_root,
        )
    runner = GATE_RUNNERS.get(args.gate)
    if runner is None:
        return run_unimplemented(args.gate)
    return runner(
        root,
        state_root=state_root,
        artifact_root=artifact_root,
        socket_root=socket_root,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
