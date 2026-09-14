#!/usr/bin/env python3
"""Create one non-secret V2 OpenRouter profile from a managed Codex agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tomllib
import urllib.parse
import uuid


MANAGED_AGENT_MARKER = "# Managed by OpenRouter Settings native-agent registration v1"
MANAGED_PROVIDER_ID = "openrouter-settings"
V2_PROVIDER_ID = "com.modeldeck.openrouter"
OPENROUTER_HOST = "openrouter.ai"


def _require_regular_file(path: Path, description: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{description} must be a regular non-symbolic-link file")


def _load_managed_agent(path: Path) -> dict[str, object]:
    _require_regular_file(path, "managed agent")
    source = path.read_text(encoding="utf-8")
    if not source.startswith(MANAGED_AGENT_MARKER + "\n"):
        raise ValueError("managed agent marker is missing")
    try:
        document = tomllib.loads(source)
    except tomllib.TOMLDecodeError as error:
        raise ValueError("managed agent TOML is invalid") from error
    if not isinstance(document, dict):
        raise ValueError("managed agent must contain a TOML document")
    return document


def _provider_definition(document: dict[str, object]) -> dict[str, object]:
    if document.get("model_provider") != MANAGED_PROVIDER_ID:
        raise ValueError("managed agent uses an unsupported provider registration")
    providers = document.get("model_providers")
    if not isinstance(providers, dict) or set(providers) != {MANAGED_PROVIDER_ID}:
        raise ValueError("managed agent provider definition is invalid")
    provider = providers[MANAGED_PROVIDER_ID]
    if not isinstance(provider, dict):
        raise ValueError("managed agent provider definition is invalid")
    return provider


def _openrouter_endpoint(provider: dict[str, object]) -> str:
    base_url = provider.get("base_url")
    if not isinstance(base_url, str):
        raise ValueError("managed agent endpoint is missing")
    parsed = urllib.parse.urlsplit(base_url)
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or host != OPENROUTER_HOST
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("managed agent must use the selected HTTP-compatible OpenRouter route")
    if provider.get("wire_api") != "responses" or provider.get("supports_websockets") is not False:
        raise ValueError("managed agent does not describe the supported HTTP-compatible route")
    return base_url.rstrip("/")


def _credential_command(provider: dict[str, object]) -> dict[str, object]:
    authentication = provider.get("auth")
    if not isinstance(authentication, dict):
        raise ValueError("managed agent has no credential reference")
    executable = authentication.get("command")
    arguments = authentication.get("args")
    timeout_ms = authentication.get("timeout_ms")
    if (
        not isinstance(executable, str)
        or not Path(executable).is_absolute()
        or not isinstance(arguments, list)
        or len(arguments) != 2
        or arguments[0] != "--token"
        or not isinstance(arguments[1], str)
        or not isinstance(timeout_ms, int)
        or isinstance(timeout_ms, bool)
        or not 0 < timeout_ms <= 30_000
    ):
        raise ValueError("managed agent credential reference is invalid")
    try:
        uuid.UUID(arguments[1])
    except ValueError as error:
        raise ValueError("managed agent credential account is invalid") from error
    return {
        "executable": executable,
        "args": ["--token", arguments[1]],
        "timeout_ms": timeout_ms,
    }


def _opaque_reference(kind: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"ref:v2.{kind}.{digest}"


def _write_private_json(path: Path, document: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError("output profile already exists")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(document, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def prepare_coding_provider(managed_agent: Path, output: Path) -> None:
    document = _load_managed_agent(managed_agent)
    if output.is_symlink():
        raise ValueError("output profile must not be a symbolic link")
    model = document.get("model")
    if not isinstance(model, str) or not model or model.casefold().startswith("cursor/"):
        raise ValueError("managed agent does not select the supported HTTP-compatible model")
    provider = _provider_definition(document)
    base_url = _openrouter_endpoint(provider)
    credential = _credential_command(provider)
    identity = f"{base_url}\n{model}\n{credential['args'][1]}"
    connection_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
    profile = {
        "schema_version": 1,
        "provider_id": V2_PROVIDER_ID,
        "provider_name": "OpenRouter",
        "connection_id": connection_id,
        "provider_model_id": model,
        "display_name": model,
        "endpoint_config_ref": _opaque_reference("endpoint", identity),
        "credential_ref": _opaque_reference("credential", identity),
        "capability_snapshot_ref": "ref:v2.openrouter.serial-tools",
        "endpoint": {
            "base_url": base_url,
            "wire_mode": "auto",
            "vendor_id": "openrouter",
        },
        "credential_command": credential,
        "billing_description": "OpenRouter API usage consumes OpenRouter credits; it is not ChatGPT subscription usage.",
    }
    _write_private_json(output.resolve(), profile)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a non-secret V2 coding-provider profile from one managed agent."
    )
    parser.add_argument("--managed-agent", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        prepare_coding_provider(arguments.managed_agent, arguments.output)
    except (OSError, UnicodeError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
