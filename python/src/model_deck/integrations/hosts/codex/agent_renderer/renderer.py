from __future__ import annotations

import hashlib
import re
import tomllib
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tomlkit

AGENT_MARKER = "# Managed by OpenRouter Settings native-agent registration v1"
PROVIDER = "openrouter-settings"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
CURSOR_BASE_URL = "https://api.cursor.com"
SUBSCRIPTION_MODELS = ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.3-codex-spark")
BOUNDED_AGENT_INSTRUCTIONS = (
    "Complete only the bounded task assigned by the parent agent. Obey the parent's scope, "
    "ownership boundaries, fixed decisions, and verification requirements. Preserve other agents' "
    "changes. Report results and unresolved limits to the parent. Do not delegate to additional "
    "agents unless the parent explicitly asks you to do so."
)
ENDPOINT_EFFORTS = ("default", "low", "medium", "high", "xhigh")
SUBSCRIPTION_EFFORTS = ("low", "medium", "high", "xhigh")
AUTH_TIMEOUT_MS = 5000
AUTH_REFRESH_INTERVAL_MS = 300000
LOCAL_HOST_SUFFIXES = (".local", ".localhost", ".lan", ".home", ".internal")

RenderKind = Literal["endpoint", "subscription"]


class RenderError(ValueError):
    """Raised when a render request violates the extracted legacy rules."""


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """Caller-resolved inputs for one managed agent file. No fetching or I/O."""

    kind: RenderKind
    provider_model_id: str
    display_name: str
    endpoint_name: str | None = None
    base_url: str | None = None
    credential_account_id: str | None = None
    token_helper_path: str | None = None
    reasoning_effort: str | None = None
    billing_description: str | None = None


@dataclass(frozen=True, slots=True)
class RenderedAgent:
    """Relative agent filename plus UTF-8 TOML bytes. Identity collisions stay with the consumer."""

    filename: str
    content: bytes


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


def _validate_base_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 512 or any(ord(character) < 33 for character in value):
        raise RenderError("Endpoint URL must be a single-line http(s) URL.")
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        raise RenderError("Endpoint URL must be an http(s) URL without query or fragment.") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        raise RenderError("Endpoint URL must be an http(s) URL without query or fragment.")
    if parts.scheme == "http" and not _is_local_host(parts.hostname):
        raise RenderError("Plain http endpoints are only allowed on this Mac or the local network.")
    return value.rstrip("/")


def _is_cursor_url(value: str) -> bool:
    return isinstance(value, str) and value.rstrip("/") == CURSOR_BASE_URL


def _is_openrouter_url(value: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(value).hostname or "").lower()
    except ValueError:
        return False
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _validate_cursor_route(model: str, base_url: str) -> None:
    cursor = _is_cursor_url(base_url)
    reserved = model.lower().lstrip("~").startswith("cursor/")
    if cursor and (not model.startswith("cursor/") or not model[7:] or "/" in model[7:]):
        raise RenderError("Cursor SDK models require cursor/<SDK model id>.")
    if reserved and not cursor:
        raise RenderError("Cursor model IDs require the fixed Cursor SDK endpoint.")


def _validate_model_id(model: object) -> str:
    if (not isinstance(model, str) or not 0 < len(model) <= 256
            or any(character.isspace() or ord(character) < 32 for character in model) or '"' in model):
        raise RenderError("Enter a valid model ID without spaces.")
    return model


def _validate_account_id(account: object) -> None:
    try:
        if not isinstance(account, str) or str(uuid.UUID(account)) != account.lower():
            raise ValueError()
    except (ValueError, AttributeError):
        raise RenderError("The key account identifier must be a UUID.") from None


def _validate_token_helper(path: object) -> str:
    if (not isinstance(path, str) or not Path(path).is_absolute() or any(ord(character) < 32 for character in path)):
        raise RenderError("The authentication executable must be an absolute path.")
    return path


def _validate_endpoint_name(name: object) -> str:
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 64 or any(ord(character) < 32 for character in name):
        raise RenderError("Give the endpoint a short name.")
    return name.strip()


def _validate_display_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip():
        raise RenderError("Provide a display name for this model.")
    return name.strip()


def _provider_table(account: str | None, executable: str, base_url: str, name: str):
    provider = tomlkit.table()
    provider.update({"name": name, "base_url": base_url, "wire_api": "responses", "supports_websockets": False})
    if account is not None:
        auth = tomlkit.table()
        auth.update({"command": executable, "args": ["--token", account],
                     "timeout_ms": AUTH_TIMEOUT_MS, "refresh_interval_ms": AUTH_REFRESH_INTERVAL_MS})
        provider["auth"] = auth
    return provider


def _render_endpoint(request: RenderRequest, display_name: str) -> RenderedAgent:
    if request.endpoint_name is None or request.base_url is None or request.token_helper_path is None:
        raise RenderError("Endpoint registration requires endpoint_name, base_url, and token_helper_path.")
    if request.billing_description is None or not request.billing_description.strip():
        raise RenderError("Provide the caller-resolved billing description for this endpoint.")
    model = _validate_model_id(request.provider_model_id)
    base_url = _validate_base_url(request.base_url)
    is_openrouter = _is_openrouter_url(base_url)
    _validate_cursor_route(model, base_url)
    if is_openrouter and ("/" not in model or not all(model.split("/", 1))):
        raise RenderError("Enter a valid OpenRouter model ID, including its provider and a slash, without spaces.")
    if model.lower().lstrip("~").startswith(("gpt-", "codex-")):
        raise RenderError("Names starting with gpt- or codex- are reserved for OpenAI models.")
    if model.lower().startswith(("openai/", "~openai/")):
        raise RenderError("Use your normal Codex subscription for OpenAI models. Register a non-OpenAI model here.")
    if request.credential_account_id is not None or is_openrouter or _is_cursor_url(base_url):
        _validate_account_id(request.credential_account_id)
    executable = _validate_token_helper(request.token_helper_path)
    endpoint_name = _validate_endpoint_name(request.endpoint_name)
    effort = request.reasoning_effort if request.reasoning_effort is not None else "default"
    if effort not in ENDPOINT_EFFORTS:
        raise RenderError("Unsupported reasoning effort.")
    slug = re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")[:50] or "model"
    digest = hashlib.sha256(model.encode("utf-8")).hexdigest()[:8]
    name = f"openrouter_{slug}_{digest}"
    agent = tomlkit.document()
    agent.add(tomlkit.comment(AGENT_MARKER[2:]))
    agent["name"] = name
    agent["description"] = f"Bounded task worker using {model} through {endpoint_name}. {request.billing_description.strip()}"
    agent["developer_instructions"] = BOUNDED_AGENT_INSTRUCTIONS
    agent["model"] = model
    agent["model_reasoning_effort"] = "low" if effort == "default" else effort
    agent["model_provider"] = PROVIDER
    agent["model_providers"] = tomlkit.table()
    agent["model_providers"][PROVIDER] = _provider_table(request.credential_account_id, executable, base_url, endpoint_name)
    content = tomlkit.dumps(agent).encode("utf-8")
    tomllib.loads(content.decode("utf-8"))
    return RenderedAgent(filename=f"{name}.toml", content=content)


def _render_subscription(request: RenderRequest, display_name: str) -> RenderedAgent:
    if request.endpoint_name is not None or request.base_url is not None:
        raise RenderError("Subscription registration takes no endpoint_name or base_url.")
    if request.credential_account_id is not None or request.token_helper_path is not None:
        raise RenderError("Subscription registration takes no credential account or token helper.")
    if request.billing_description is not None:
        raise RenderError("Subscription registration takes no billing description.")
    model = request.provider_model_id
    if not isinstance(model, str) or model not in SUBSCRIPTION_MODELS:
        raise RenderError("Choose a supported bare OpenAI subscription model ID.")
    effort = request.reasoning_effort if request.reasoning_effort is not None else "low"
    if effort not in SUBSCRIPTION_EFFORTS:
        raise RenderError("Unsupported subscription agent reasoning effort.")
    name = "subscription_" + re.sub(r"[^a-z0-9]+", "_", model)
    agent = tomlkit.document()
    agent.add(tomlkit.comment(AGENT_MARKER[2:]))
    agent["name"] = name
    agent["description"] = f"Bounded task worker using {model} through the OpenAI subscription connection."
    agent["developer_instructions"] = BOUNDED_AGENT_INSTRUCTIONS
    agent["model"] = model
    agent["model_provider"] = "openai"
    agent["model_reasoning_effort"] = effort
    content = tomlkit.dumps(agent).encode("utf-8")
    tomllib.loads(content.decode("utf-8"))
    return RenderedAgent(filename=f"{name}.toml", content=content)


def render_managed_agent(request: RenderRequest) -> RenderedAgent:
    """Render one managed agent file from caller-resolved inputs without I/O."""
    display_name = _validate_display_name(request.display_name)
    if request.kind == "endpoint":
        return _render_endpoint(request, display_name)
    if request.kind == "subscription":
        return _render_subscription(request, display_name)
    raise RenderError("Registration kind must be endpoint or subscription.")
