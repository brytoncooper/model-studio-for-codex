from __future__ import annotations

import tomllib
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from model_deck.integrations.hosts.codex.legacy_models import AGENT_MARKER, PROVIDER
from model_deck.integrations.hosts.codex.migration_preview.redaction import find_secret_field_names

CURSOR_BASE_URL = "https://api.cursor.com"
LOCAL_HOST_SUFFIXES = (".local", ".localhost", ".lan", ".home", ".internal")


def _valid_model(model: object) -> bool:
    if not isinstance(model, str) or not 0 < len(model) <= 256:
        return False
    if any(character.isspace() or ord(character) < 32 for character in model):
        return False
    lowered = model.lower().lstrip("~")
    if lowered.startswith(("openai/", "gpt-", "codex-")):
        return False
    return True


def _is_local_host(host: str) -> bool:
    host = (host or "").lower().strip("[]")
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if host.endswith(LOCAL_HOST_SUFFIXES):
        return True
    octets = host.split(".")
    if len(octets) == 4 and all(part.isdigit() for part in octets):
        first, second = int(octets[0]), int(octets[1])
        return first == 10 or (first == 192 and second == 168) or (first == 172 and 16 <= second <= 31)
    return False


def _validate_base_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 512 or any(ord(character) < 33 for character in value):
        return None
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        return None
    if parts.scheme == "http" and not _is_local_host(parts.hostname):
        return None
    return value.rstrip("/")


def _is_cursor_url(value: str) -> bool:
    return value.rstrip("/") == CURSOR_BASE_URL


def _is_openrouter_url(value: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(value).hostname or "").lower()
    except ValueError:
        return False
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _validate_cursor_route(model: str, base_url: str) -> bool:
    cursor = _is_cursor_url(base_url)
    reserved = model.lower().lstrip("~").startswith("cursor/")
    if cursor and (not model.startswith("cursor/") or not model[7:] or "/" in model[7:]):
        return False
    if reserved and not cursor:
        return False
    return True


def _validate_auth(auth: object) -> tuple[str | None, str | None]:
    if not isinstance(auth, dict) or set(auth) != {"command", "args", "timeout_ms", "refresh_interval_ms"}:
        return None, "managed provider auth shape invalid"
    command = auth.get("command")
    if not isinstance(command, str) or not Path(command).is_absolute():
        return None, "managed provider auth command invalid"
    if any(ord(character) < 32 for character in command):
        return None, "managed provider auth command invalid"
    args = auth.get("args")
    if not isinstance(args, list) or len(args) != 2 or args[0] != "--token" or not isinstance(args[1], str):
        return None, "managed provider auth args invalid"
    try:
        if str(uuid.UUID(args[1])) != args[1].lower():
            return None, "managed provider auth uuid invalid"
    except ValueError:
        return None, "managed provider auth uuid invalid"
    for key in ("timeout_ms", "refresh_interval_ms"):
        if type(auth.get(key)) is not int or auth[key] <= 0:
            return None, "managed provider auth timing invalid"
    return args[1].lower(), None


def _validate_provider(provider: object) -> tuple[dict[str, Any] | None, str | None]:
    allowed = {"name", "base_url", "wire_api", "supports_websockets", "auth"}
    if not isinstance(provider, dict) or not {"name", "base_url", "wire_api", "supports_websockets"} <= set(provider):
        return None, "managed provider fields invalid"
    if not set(provider) <= allowed:
        return None, "managed provider has unexpected fields"
    if provider.get("wire_api") != "responses" or provider.get("supports_websockets") is not False:
        return None, "managed provider transport invalid"
    name = provider.get("name")
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 64:
        return None, "managed provider name invalid"
    if any(ord(character) < 32 for character in name):
        return None, "managed provider name invalid"
    base_url = _validate_base_url(provider.get("base_url"))
    if base_url is None:
        return None, "managed provider base_url invalid"
    account_id = None
    if "auth" in provider:
        account_id, auth_error = _validate_auth(provider["auth"])
        if auth_error is not None:
            return None, auth_error
    elif _is_openrouter_url(base_url) or _is_cursor_url(base_url):
        return None, "managed provider auth required"
    return (
        {
            "provider": provider,
            "account_id": account_id,
            "base_url": base_url,
            "endpoint_name": name.strip(),
        },
        None,
    )


@dataclass(frozen=True, slots=True)
class ManagedAgentView:
    path: str
    role: str
    model: str
    account_id: str | None
    base_url: str
    endpoint_name: str


@dataclass(frozen=True, slots=True)
class AgentParseResult:
    classification: str
    detail: str | None
    managed: ManagedAgentView | None


def classify_agent_file(path: str, filename: str, raw: bytes) -> AgentParseResult:
    if not filename.endswith(".toml"):
        return AgentParseResult("foreign", "non-toml agent entry", None)
    if not filename.startswith("openrouter_"):
        return AgentParseResult("foreign", "agent filename not managed pattern", None)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return AgentParseResult("malformed", "agent utf-8 invalid", None)
    if not text.startswith(AGENT_MARKER + "\n"):
        return AgentParseResult("foreign", "agent missing managed marker", None)
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return AgentParseResult("malformed", "agent toml invalid", None)
    secret_fields = find_secret_field_names(document)
    if secret_fields:
        return AgentParseResult("malformed", f"secret field name: {secret_fields[0]}", None)
    role = document.get("name")
    model = document.get("model")
    if not isinstance(role, str) or role != Path(filename).stem:
        return AgentParseResult("malformed", "agent name filename mismatch", None)
    if document.get("model_provider") != PROVIDER:
        return AgentParseResult("malformed", "agent provider mismatch", None)
    if not _valid_model(model):
        return AgentParseResult("malformed", "agent model invalid", None)
    assert isinstance(model, str)
    providers = document.get("model_providers")
    if not isinstance(providers, dict) or set(providers) != {PROVIDER}:
        return AgentParseResult("malformed", "agent model_providers invalid", None)
    provider_view, provider_error = _validate_provider(providers[PROVIDER])
    if provider_error is not None:
        return AgentParseResult("malformed", provider_error, None)
    assert provider_view is not None
    base_url = provider_view["base_url"]
    if not _validate_cursor_route(model, base_url):
        return AgentParseResult("malformed", "agent cursor route invalid", None)
    if _is_openrouter_url(base_url) and ("/" not in model or not all(model.split("/", 1))):
        return AgentParseResult("malformed", "agent openrouter model invalid", None)
    return AgentParseResult(
        "managed_match",
        None,
        ManagedAgentView(
            path=path,
            role=role,
            model=model,
            account_id=provider_view["account_id"],
            base_url=base_url,
            endpoint_name=provider_view["endpoint_name"],
        ),
    )
