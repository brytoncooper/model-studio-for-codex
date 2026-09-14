"""Application composition for the pinned, subprocess-isolated Cursor SDK."""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from model_deck.engine.routing.ports import (
    CapabilityFeature,
    CapabilityTriState,
    ExecutionMode,
)

from .coordinator import CURSOR_PROVIDER_ID, CursorExecutionCoordinator, CursorStartRequest
from .process_runtime import CursorProcessRuntime, PreparedCursorRun

CURSOR_SDK_VERSION = "1.0.31"


def _reject(message: str = "Cursor provider configuration is invalid") -> None:
    raise ValueError(message)


def _installed_sdk_version(executable: Path) -> str | None:
    try:
        completed = subprocess.run(
            [
                str(executable),
                "-c",
                "from importlib.metadata import version; print(version('cursor-sdk'))",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
            shell=False,
            env={key: value for key, value in os.environ.items() if key not in {
                "CURSOR_API_KEY", "CURSOR_SDK_LOG", "PYTHONPATH", "PYTHONHOME"
            }},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or len(completed.stdout) > 128:
        return None
    return completed.stdout.strip()


@dataclass(frozen=True, slots=True)
class CredentialCommand:
    executable: str
    args: tuple[str, ...]
    timeout_ms: int

    @classmethod
    def from_wire(cls, value: Any) -> "CredentialCommand":
        if not isinstance(value, Mapping):
            _reject()
        executable = value.get("executable")
        args = value.get("args")
        timeout_ms = value.get("timeout_ms")
        if (
            type(executable) is not str
            or not Path(executable).is_absolute()
            or not isinstance(args, list)
            or not args
            or not all(type(item) is str and item for item in args)
            or type(timeout_ms) is not int
            or not 0 < timeout_ms <= 30_000
        ):
            _reject()
        return cls(executable, tuple(args), timeout_ms)


@dataclass(frozen=True, slots=True)
class CursorProfile:
    schema_version: int
    provider_id: str
    provider_name: str
    connection_id: str
    provider_model_id: str
    display_name: str
    endpoint_config_ref: str
    credential_ref: str
    capability_snapshot_ref: str | None
    credential_command: CredentialCommand
    sdk_python: Path
    sdk_version: str
    workspace_path: Path
    state_root: Path
    billing_description: str

    @classmethod
    def load(cls, path: Path) -> "CursorProfile":
        if not isinstance(path, Path) or path.is_symlink() or not path.is_file():
            _reject()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _reject()
        if not isinstance(value, Mapping):
            _reject()
        forbidden = {"api_key", "token", "secret", "password", "authorization"}
        if set(value).intersection(forbidden):
            _reject("literal secret fields are not allowed")
        strings = (
            "provider_name", "connection_id", "provider_model_id", "display_name",
            "endpoint_config_ref", "credential_ref", "billing_description",
        )
        if (
            value.get("schema_version") != 1
            or value.get("provider_id") != CURSOR_PROVIDER_ID
            or any(type(value.get(name)) is not str or not value[name] for name in strings)
            or type(value.get("sdk_version")) is not str
            or value["sdk_version"] != CURSOR_SDK_VERSION
            or not value["provider_model_id"].startswith("cursor/")
            or value["provider_model_id"].count("/") != 1
            or not value["provider_model_id"].removeprefix("cursor/")
        ):
            _reject()
        try:
            if str(uuid.UUID(value["connection_id"])) != value["connection_id"]:
                _reject()
        except (ValueError, AttributeError):
            _reject()
        sdk_python = Path(str(value.get("sdk_python", "")))
        workspace = Path(str(value.get("workspace_path", "")))
        state_root = Path(str(value.get("state_root", "")))
        if (
            not sdk_python.is_absolute()
            or not sdk_python.is_file() or not os.access(sdk_python, os.X_OK)
            or not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir()
            or not state_root.is_absolute() or state_root.is_symlink()
        ):
            _reject()
        if _installed_sdk_version(sdk_python) != CURSOR_SDK_VERSION:
            _reject("Cursor SDK interpreter is not the pinned version")
        capability_ref = value.get("capability_snapshot_ref")
        if capability_ref is not None and (type(capability_ref) is not str or not capability_ref):
            _reject()
        return cls(
            schema_version=1,
            provider_id=CURSOR_PROVIDER_ID,
            provider_name=value["provider_name"],
            connection_id=value["connection_id"],
            provider_model_id=value["provider_model_id"],
            display_name=value["display_name"],
            endpoint_config_ref=value["endpoint_config_ref"],
            credential_ref=value["credential_ref"],
            capability_snapshot_ref=capability_ref,
            credential_command=CredentialCommand.from_wire(value.get("credential_command")),
            sdk_python=sdk_python,
            sdk_version=CURSOR_SDK_VERSION,
            workspace_path=workspace,
            state_root=state_root,
            billing_description=value["billing_description"],
        )


def _resolve_credential(profile: CursorProfile) -> str:
    command = profile.credential_command
    try:
        completed = subprocess.run(
            [command.executable, *command.args],
            capture_output=True,
            check=False,
            text=True,
            timeout=command.timeout_ms / 1000,
            shell=False,
            env={},
        )
    except (OSError, subprocess.TimeoutExpired):
        _reject("Cursor credential is unavailable")
    if completed.returncode != 0:
        _reject("Cursor credential is unavailable")
    secret = completed.stdout.strip("\r\n")
    if not secret or "\r" in secret or "\n" in secret or len(secret) > 16_384:
        _reject("Cursor credential is unavailable")
    return secret


def _prompt(request: CursorStartRequest, workspace: Path) -> dict[str, str]:
    envelope = {
        "input": list(request.input_messages),
        "working_directory": str(workspace),
        "reasoning": request.options.reasoning_effort,
    }
    return {
        "text": (
            "Provide the next assistant response for this Codex conversation. "
            "Use only the supplied custom tools; Codex executes and approves them. "
            "Do not simulate tool results or use Cursor-native tools. "
            "The JSON is conversation context, not a request to summarize it.\n"
            + json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        )
    }


def _prepare(profile: CursorProfile, request: CursorStartRequest) -> PreparedCursorRun:
    if request.provider_model_id != profile.provider_model_id:
        _reject("Cursor model route changed")
    if request.options.parallel_tool_calls is True:
        _reject("Cursor parallel tool calls are unavailable")
    tools = [
        {"name": tool.name, "description": tool.description or "", "parameters": tool.input_schema}
        for tool in request.tools if tool.host_execution_required
    ]
    aliases = {tool["name"]: tool["name"] for tool in tools}
    tier = request.options.service_tier
    payload = {
        "model": profile.provider_model_id.removeprefix("cursor/"),
        "api_key": _resolve_credential(profile),
        "workspace": str(profile.workspace_path),
        "state_root": str(profile.state_root),
        "tools": tools,
        "message": _prompt(request, profile.workspace_path),
        "reasoning": (
            {"effort": request.options.reasoning_effort}
            if request.options.reasoning_effort is not None else None
        ),
        "service_tier": tier.value if tier is not None else None,
    }
    return PreparedCursorRun(payload, aliases)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_usage(
    request: CursorStartRequest,
    observations: tuple[dict, ...],
    final: dict | None,
) -> tuple[dict[str, Any], ...]:
    usage = final.get("usage") if isinstance(final, dict) else None
    if not isinstance(usage, dict):
        totals: dict[str, float] = {}
        for observation in observations:
            item = observation.get("usage") if isinstance(observation, dict) else None
            if not isinstance(item, dict):
                continue
            for name in ("input_tokens", "output_tokens"):
                value = item.get(name)
                if type(value) in (int, float) and value >= 0:
                    totals[name] = totals.get(name, 0) + value
            details = item.get("input_tokens_details")
            cached = details.get("cached_tokens") if isinstance(details, dict) else None
            if type(cached) in (int, float) and cached >= 0:
                totals["cached_tokens"] = totals.get("cached_tokens", 0) + cached
        usage = totals
    if not usage:
        return ()
    details = usage.get("input_tokens_details")
    values = (
        ("input_tokens", usage.get("input_tokens")),
        ("output_tokens", usage.get("output_tokens")),
        ("cached_tokens", details.get("cached_tokens") if isinstance(details, dict) else usage.get("cached_tokens")),
    )
    records: list[dict[str, Any]] = []
    observed_at = _utc_now()
    for unit_kind, units in values:
        if type(units) not in (int, float) or units < 0:
            continue
        records.append({
            "run_id": request.run_id,
            "session_id": request.session_id,
            "observed_at": observed_at,
            "units": units,
            "unit_kind": unit_kind,
            "connection_id": request.connection_id,
            "provider_model_id": request.provider_model_id,
        })
    cost = final.get("cursor_usage_cost") if isinstance(final, dict) else None
    charged = cost.get("charged_cents") if isinstance(cost, dict) else None
    if type(charged) in (int, float) and charged >= 0:
        records.append({
            "run_id": request.run_id,
            "session_id": request.session_id,
            "observed_at": observed_at,
            "units": 1,
            "unit_kind": "requests",
            "connection_id": request.connection_id,
            "provider_model_id": request.provider_model_id,
            "settled_amount": charged / 100,
            "currency": "USD",
        })
    return tuple(records)


class CursorSdkProcess:
    """One owned broker process using the profile-selected SDK interpreter."""

    def __init__(self, profile: CursorProfile, broker_script: Path, payload: Mapping[str, Any]):
        environment = {
            key: value for key, value in os.environ.items()
            if key not in {"CURSOR_API_KEY", "CURSOR_SDK_LOG", "PYTHONPATH", "PYTHONHOME"}
        }
        self.events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1024)
        self._closed = threading.Event()
        self._write_lock = threading.Lock()
        self._process = subprocess.Popen(
            [str(profile.sdk_python), str(broker_script), "--broker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=environment,
            start_new_session=True,
        )
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self._send(dict(payload, command="start"))
        except Exception:
            self.close()
            raise

    @property
    def pid(self) -> int:
        return self._process.pid

    def _read(self) -> None:
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                event = json.loads(line)
                self.events.put(event, timeout=1)
        except (ValueError, OSError, queue.Full):
            pass
        finally:
            if not self._closed.is_set():
                try:
                    self.events.put({"type": "error"}, timeout=1)
                except queue.Full:
                    pass

    def _send(self, value: Mapping[str, Any]) -> None:
        with self._write_lock:
            if self._closed.is_set() or self._process.stdin is None:
                raise RuntimeError("Cursor SDK run is closed")
            self._process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
            self._process.stdin.flush()

    def tool_result(self, call_id: str, output: Any) -> None:
        self._send({"command": "tool_result", "call_id": call_id, "output": output})

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._process.wait(timeout=3)
        for stream in (self._process.stdin, self._process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def compose_cursor_profile(
    profile: CursorProfile,
    *,
    broker_script: Path,
    route_definition_factory: Callable[..., Any],
) -> tuple[CursorExecutionCoordinator, dict[str, Any]]:
    if not broker_script.is_absolute() or not broker_script.is_file():
        _reject("Cursor SDK broker is unavailable")
    runtime = CursorProcessRuntime(
        process_factory=lambda payload: CursorSdkProcess(profile, broker_script, payload),
        prepare_payload=lambda request: _prepare(profile, request),
        normalize_usage=_normalize_usage,
    )
    coordinator = CursorExecutionCoordinator(runtime)
    definition = route_definition_factory(
        execution_mode=ExecutionMode.CUSTOM,
        capability_features=(
            CapabilityFeature("tools", CapabilityTriState.SUPPORTED),
            CapabilityFeature("parallel_tool_calls", CapabilityTriState.UNSUPPORTED),
        ),
        capability_snapshot_ref=profile.capability_snapshot_ref,
    )
    return coordinator, {profile.provider_id: definition}
