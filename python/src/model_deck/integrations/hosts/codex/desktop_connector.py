"""Prepare and launch Codex Desktop with the V2 attachment bridge."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable

from .desktop_attachment import (
    BRIDGE_DESCRIPTOR_ENV,
    ENGINE_CREDENTIAL_ENV,
    ENGINE_RENDEZVOUS_ENV,
    DesktopAttachmentConfiguration,
)
from .runtime import (
    CodexProcessProbe,
    CodexRuntimeError,
    classify_compatibility,
    discover_runtime,
)

CODEX_CLI_PATH_ENV = "CODEX_CLI_PATH"
CODEX_LAUNCHER_EXECUTABLE = Path("/usr/bin/open")


class ConnectorStatus(StrEnum):
    MISSING_CONFIGURATION = "missing_configuration"
    MISSING_RUNTIME = "missing_runtime"
    UNSUPPORTED_HOST = "unsupported_host"
    RESTART_REQUIRED = "restart_required"
    READY = "ready"
    CONNECTED = "connected"


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    executable: Path
    argv: tuple[str, ...]
    application_path: Path


@dataclass(frozen=True, slots=True)
class ConnectorOutcome:
    status: ConnectorStatus
    reason: str
    plan: LaunchPlan | None = None


class RunningCodexApplicationProbe:
    """Exact application-executable probe; it never changes process state."""

    def __init__(self, application_path: Path) -> None:
        application_paths = (
            (application_path / "ChatGPT.app", application_path / "Codex.app")
            if application_path.name == "Applications"
            else (application_path,)
        )
        self._executables = tuple(
            candidate / "Contents" / "MacOS" / candidate.stem
            for candidate in application_paths
        )

    def is_codex_running(self) -> bool:
        completed = subprocess.run(
            ["/bin/ps", "-axo", "command="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            return False
        commands = tuple(line.strip() for line in completed.stdout.splitlines())
        return any(
            command == str(executable) or command.startswith(f"{executable} ")
            for executable in self._executables
            for command in commands
        )


def prepare_connector(
    *,
    rendezvous_path: Path,
    credential_path: Path,
    descriptor_path: Path,
    bridge_script_path: Path,
    applications_dir: Path,
    observed_protocol_version: str,
    supported_protocol_versions: frozenset[str],
    process_probe: CodexProcessProbe | None = None,
) -> ConnectorOutcome:
    environment = {
        ENGINE_RENDEZVOUS_ENV: str(rendezvous_path),
        ENGINE_CREDENTIAL_ENV: str(credential_path),
        BRIDGE_DESCRIPTOR_ENV: str(descriptor_path),
    }
    try:
        DesktopAttachmentConfiguration.load(environment)
    except (OSError, UnicodeError, ValueError) as error:
        return ConnectorOutcome(
            ConnectorStatus.MISSING_CONFIGURATION,
            f"The V2 connection is not ready: {error}",
        )
    if not bridge_script_path.is_absolute() or bridge_script_path.is_symlink() or not bridge_script_path.is_file():
        return ConnectorOutcome(
            ConnectorStatus.MISSING_CONFIGURATION,
            "The packaged Codex Desktop bridge is unavailable. Rebuild V2 and try again.",
        )
    try:
        runtime = discover_runtime(applications_dir)
    except CodexRuntimeError as error:
        return ConnectorOutcome(ConnectorStatus.MISSING_RUNTIME, str(error))
    compatibility = classify_compatibility(
        runtime,
        observed_protocol_version=observed_protocol_version,
        supported_protocol_versions=supported_protocol_versions,
    )
    if compatibility.status != "supported":
        return ConnectorOutcome(
            ConnectorStatus.UNSUPPORTED_HOST,
            compatibility.reason or "This Codex Desktop app-server version is unsupported.",
        )
    active_probe = process_probe or RunningCodexApplicationProbe(runtime.application_path)
    if active_probe.is_codex_running():
        return ConnectorOutcome(
            ConnectorStatus.RESTART_REQUIRED,
            "Codex is already running. Fully quit it, then choose Connect Codex again. Running tasks were not changed.",
        )
    launch_values = {
        CODEX_CLI_PATH_ENV: str(bridge_script_path.resolve()),
        ENGINE_RENDEZVOUS_ENV: str(rendezvous_path.resolve()),
        ENGINE_CREDENTIAL_ENV: str(credential_path.resolve()),
        BRIDGE_DESCRIPTOR_ENV: str(descriptor_path.resolve()),
    }
    arguments: list[str] = [str(CODEX_LAUNCHER_EXECUTABLE), "-a", str(runtime.application_path)]
    for name, value in launch_values.items():
        arguments.extend(["--env", f"{name}={value}"])
    return ConnectorOutcome(
        ConnectorStatus.READY,
        "Codex Desktop is compatible and ready to connect.",
        LaunchPlan(CODEX_LAUNCHER_EXECUTABLE, tuple(arguments), runtime.application_path),
    )


def attach(
    outcome: ConnectorOutcome,
    *,
    process_launcher: Callable[[tuple[str, ...]], int] | None = None,
) -> ConnectorOutcome:
    if outcome.status is not ConnectorStatus.READY or outcome.plan is None:
        raise ValueError("Codex Desktop is not ready to connect")
    launcher = process_launcher or _launch
    launcher(outcome.plan.argv)
    return ConnectorOutcome(
        ConnectorStatus.CONNECTED,
        "Codex Desktop launch requested. Open a new task and choose the V2 model in the picker.",
        outcome.plan,
    )


def _launch(argv: tuple[str, ...]) -> int:
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return process.pid


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare or connect Codex Desktop to Model Deck V2.")
    parser.add_argument("action", choices=("inspect", "connect"))
    parser.add_argument("--engine-rendezvous", required=True, type=Path)
    parser.add_argument("--engine-credential", required=True, type=Path)
    parser.add_argument("--bridge-descriptor", required=True, type=Path)
    parser.add_argument("--bridge-script", required=True, type=Path)
    parser.add_argument("--applications-dir", default="/Applications", type=Path)
    parser.add_argument("--protocol-version", default="codex.app-server.v1")
    options = parser.parse_args(arguments)
    outcome = prepare_connector(
        rendezvous_path=options.engine_rendezvous,
        credential_path=options.engine_credential,
        descriptor_path=options.bridge_descriptor,
        bridge_script_path=options.bridge_script,
        applications_dir=options.applications_dir,
        observed_protocol_version=options.protocol_version,
        supported_protocol_versions=frozenset({"codex.app-server.v1"}),
    )
    if options.action == "connect" and outcome.status is ConnectorStatus.READY:
        outcome = attach(outcome)
    print(json.dumps({"status": outcome.status, "reason": outcome.reason}))
    return 0 if outcome.status in {ConnectorStatus.READY, ConnectorStatus.CONNECTED} else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ConnectorOutcome",
    "ConnectorStatus",
    "LaunchPlan",
    "RunningCodexApplicationProbe",
    "attach",
    "prepare_connector",
]
