#!/usr/bin/env python3
"""Run the actual Codex CLI against one isolated Model Deck V2 bridge."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import TextIO
import urllib.parse


BRIDGE_TOKEN_ENVIRONMENT_VARIABLE = "MODEL_DECK_V2_BRIDGE_TOKEN"


@dataclass(frozen=True, slots=True)
class IsolatedCodexLaunch:
    codex_home: Path
    environment: dict[str, str]
    model: str


def _load_bridge_descriptor(path: Path, state_root: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Codex bridge descriptor is unavailable")
    descriptor = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(descriptor, dict) or descriptor.get("schema_version") != 1:
        raise ValueError("Codex bridge descriptor is invalid")
    base_url = descriptor.get("base_url")
    parsed = urllib.parse.urlsplit(base_url if isinstance(base_url, str) else "")
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.path.rstrip("/") != "/v1"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Codex bridge must use an explicit IPv4 loopback URL")
    for field in ("provider_id", "model", "billing_description", "token_path"):
        if not isinstance(descriptor.get(field), str) or not descriptor[field]:
            raise ValueError("Codex bridge descriptor is incomplete")
    expected_engine_root = (state_root / "application-state" / "engine").resolve()
    token_path = Path(str(descriptor["token_path"]))
    if token_path.resolve().parent != expected_engine_root:
        raise ValueError("Codex bridge token is outside the isolated engine state")
    return descriptor


def _write_codex_config(path: Path, *, base_url: str, model: str) -> None:
    lines = [
        f"model = {json.dumps(model)}",
        'model_provider = "model_deck_v2"',
        'approval_policy = "never"',
        'sandbox_mode = "workspace-write"',
        'web_search = "disabled"',
        "",
        "[features]",
        "multi_agent = false",
        "",
        "[model_providers.model_deck_v2]",
        'name = "Model Deck V2"',
        f"base_url = {json.dumps(base_url)}",
        f"env_key = {json.dumps(BRIDGE_TOKEN_ENVIRONMENT_VARIABLE)}",
        'wire_api = "responses"',
        "requires_openai_auth = false",
        "supports_websockets = false",
        "request_max_retries = 0",
        "stream_max_retries = 0",
        "",
    ]
    encoded = "\n".join(lines)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def prepare_isolated_codex_environment(
    *,
    state_root: Path,
    descriptor_path: Path,
) -> IsolatedCodexLaunch:
    state_root = state_root.resolve()
    descriptor = _load_bridge_descriptor(descriptor_path.resolve(), state_root)
    token_path = Path(str(descriptor["token_path"]))
    if token_path.is_symlink() or not token_path.is_file():
        raise ValueError("Codex bridge token is unavailable")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token or "\r" in token or "\n" in token:
        raise ValueError("Codex bridge token is invalid")

    isolated_root = state_root / "codex-harness"
    home = isolated_root / "home"
    codex_home = isolated_root / "codex-home"
    xdg_config_home = isolated_root / "xdg-config"
    temporary_directory = isolated_root / "temporary"
    for directory in (isolated_root, home, codex_home, xdg_config_home, temporary_directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise ValueError("isolated Codex directory must not be a symbolic link")

    _write_codex_config(
        codex_home / "config.toml",
        base_url=str(descriptor["base_url"]),
        model=str(descriptor["model"]),
    )
    safe_path = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    environment = {
        "PATH": safe_path,
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "XDG_CONFIG_HOME": str(xdg_config_home),
        "TMPDIR": str(temporary_directory),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "SHELL": os.environ.get("SHELL", "/bin/zsh"),
        BRIDGE_TOKEN_ENVIRONMENT_VARIABLE: token,
    }
    return IsolatedCodexLaunch(
        codex_home=codex_home,
        environment=environment,
        model=str(descriptor["model"]),
    )


def _conversation_id_path(state_root: Path) -> Path:
    return state_root / "codex-harness" / "conversation-id"


def _stream_codex_output(
    process: subprocess.Popen[str],
    *,
    output: TextIO,
    on_thread_started: Callable[[], None] | None = None,
) -> str | None:
    conversation_id = None
    assert process.stdout is not None
    for line in process.stdout:
        output.write(line)
        output.flush()
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            candidate = event.get("thread_id")
            if isinstance(candidate, str) and candidate:
                conversation_id = candidate
                if on_thread_started is not None:
                    on_thread_started()
                    on_thread_started = None
    return conversation_id


def _start_interrupt_timer(process: subprocess.Popen[str], delay_seconds: float) -> threading.Timer:
    timer = threading.Timer(
        delay_seconds,
        lambda: process.send_signal(signal.SIGINT) if process.poll() is None else None,
    )
    timer.daemon = True
    timer.start()
    return timer


def run_codex(
    *,
    action: str,
    state_root: Path,
    project: Path,
    prompt: str,
    codex_executable: Path,
    cancel_after_seconds: float | None = None,
) -> int:
    if not project.is_absolute() or not project.is_dir() or project.is_symlink():
        raise ValueError("project must be an absolute, regular directory")
    if not codex_executable.is_absolute() or not os.access(codex_executable, os.X_OK):
        raise ValueError("Codex executable must be an absolute executable path")
    descriptor_path = state_root / "application-state" / "engine" / "codex-bridge.json"
    launch = prepare_isolated_codex_environment(
        state_root=state_root,
        descriptor_path=descriptor_path,
    )
    common_arguments = ["--json", "--strict-config"]
    if action == "resume":
        conversation_path = _conversation_id_path(state_root)
        if conversation_path.is_symlink() or not conversation_path.is_file():
            raise ValueError("no isolated Codex conversation is available to resume")
        conversation_id = conversation_path.read_text(encoding="utf-8").strip()
        arguments = [
            str(codex_executable),
            "exec",
            "resume",
            *common_arguments,
            conversation_id,
            prompt,
        ]
    else:
        arguments = [
            str(codex_executable),
            "exec",
            *common_arguments,
            "-C",
            str(project),
            "-s",
            "workspace-write",
            "-m",
            launch.model,
            prompt,
        ]
    process = subprocess.Popen(
        arguments,
        cwd=project,
        env=launch.environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        encoding="utf-8",
    )
    timer: threading.Timer | None = None

    def start_cancellation_timer() -> None:
        nonlocal timer
        if cancel_after_seconds is not None:
            timer = _start_interrupt_timer(process, cancel_after_seconds)

    try:
        conversation_id = _stream_codex_output(
            process,
            output=sys.stdout,
            on_thread_started=(
                start_cancellation_timer
                if cancel_after_seconds is not None
                else None
            ),
        )
        return_code = process.wait()
    finally:
        if timer is not None:
            timer.cancel()
    if action == "start" and return_code == 0 and conversation_id is not None:
        conversation_path = _conversation_id_path(state_root)
        conversation_path.write_text(conversation_id + "\n", encoding="utf-8")
        os.chmod(conversation_path, 0o600)
    return return_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one actual Codex CLI conversation through Model Deck V2."
    )
    parser.add_argument("action", choices=("start", "resume", "cancel"))
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--prompt", required=True)
    parser.add_argument(
        "--codex-executable",
        type=Path,
        default=Path(shutil.which("codex") or ""),
    )
    parser.add_argument("--cancel-after-seconds", type=float, default=0.5)
    arguments = parser.parse_args()
    cancel_after_seconds = (
        arguments.cancel_after_seconds if arguments.action == "cancel" else None
    )
    try:
        return run_codex(
            action=arguments.action,
            state_root=arguments.state_root.resolve(),
            project=arguments.project.resolve(),
            prompt=arguments.prompt,
            codex_executable=arguments.codex_executable.resolve(),
            cancel_after_seconds=cancel_after_seconds,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
