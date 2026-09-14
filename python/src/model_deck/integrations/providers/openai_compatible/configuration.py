"""V2 OpenAI-compatible provider profile loader and composer.

Owns the non-secret seam that lets the engine build an
``OpenAICompatibleExecutionPort`` from a JSON profile document.

The profile loader validates a single ``schema_version=1`` document against
the engine's reverse-domain / UUID / HTTPS URL invariants without touching
credentials. The credential resolver runs a fixed command with a bounded
timeout, captured stdout, no shell, and rejects empty or newline-bearing
secrets with a sanitized error. The endpoint resolver matches only the
declared ref/revision pair and parses the URL into host/port/TLS/path/wire/
vendor. ``compose_openai_compatible_profile`` ties the profile, the
resolvers, and the route definition that the engine consumes.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from model_deck.engine.routing.ports import (
    CapabilityFeature,
    CapabilityTriState,
    ExecutionMode,
)
from model_deck.integrations.providers.openai_compatible.execution import (
    CredentialResolver,
    EndpointResolver,
    OpenAICompatibleEndpointConfig,
    OpenAICompatibleExecutionPort,
    WireMode,
)
from model_deck.integrations.providers.openai_compatible.http_transport import (
    post_stream as default_post_stream,
)

__all__ = [
    "OpenAICompatibleProfile",
    "OpenAICompatibleProfileError",
    "compose_openai_compatible_profile",
    "endpoint_resolver_from_records",
    "subprocess_credential_resolver",
]


_PROFILE_SCHEMA_VERSION = 1
_MAX_TIMEOUT_MS = 30000
_MAX_REF_LENGTH = 128
_ENDPOINT_HOST_MAX = 253
_ENDPOINT_PATH_MAX = 2048

_REVERSE_DOMAIN_PATTERN = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9_-]*)+$")
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class OpenAICompatibleProfileError(ValueError):
    """A profile, record, or resolver cannot be used safely."""


def _reject(message: str = "openai-compatible profile invalid") -> None:
    raise OpenAICompatibleProfileError(message)


@dataclass(frozen=True, slots=True)
class CredentialCommand:
    executable: str
    args: tuple[str, ...]
    timeout_ms: int

    @classmethod
    def from_wire(cls, value: Any) -> CredentialCommand:
        if (
            not isinstance(value, Mapping)
            or type(value.get("executable")) is not str
            or not value["executable"]
            or not Path(value["executable"]).is_absolute()
            or not isinstance(value.get("args"), list)
            or not all(type(arg) is str and arg for arg in value["args"])
            or not isinstance(value.get("timeout_ms"), int)
            or isinstance(value.get("timeout_ms"), bool)
            or value["timeout_ms"] <= 0
            or value["timeout_ms"] > _MAX_TIMEOUT_MS
        ):
            _reject()
        return cls(
            executable=value["executable"],
            args=tuple(value["args"]),
            timeout_ms=value["timeout_ms"],
        )


@dataclass(frozen=True, slots=True)
class EndpointSpec:
    base_url: str
    wire_mode: WireMode
    vendor_id: str

    @classmethod
    def from_wire(cls, value: Any) -> EndpointSpec:
        if (
            not isinstance(value, Mapping)
            or type(value.get("base_url")) is not str
            or type(value.get("wire_mode")) is not str
            or value["wire_mode"] not in {"auto", "responses", "chat_completions"}
            or type(value.get("vendor_id")) is not str
            or not value["vendor_id"]
        ):
            _reject()
        parsed = urllib.parse.urlsplit(value["base_url"])
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname.endswith(".")
            or len(parsed.hostname) > _ENDPOINT_HOST_MAX
            or any(character in parsed.hostname for character in "/\\\r\n?#")
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or not parsed.path.startswith("/")
            or len(parsed.path) > _ENDPOINT_PATH_MAX
            or any(character in parsed.path for character in "\\\r\n")
        ):
            _reject()
        return cls(
            base_url=value["base_url"],
            wire_mode=WireMode(value["wire_mode"]),
            vendor_id=value["vendor_id"],
        )


@dataclass(frozen=True, slots=True)
class OpenAICompatibleProfile:
    schema_version: int
    provider_id: str
    provider_name: str
    connection_id: str
    provider_model_id: str
    display_name: str
    endpoint_config_ref: str
    credential_ref: str
    capability_snapshot_ref: str | None
    endpoint: EndpointSpec
    credential_command: CredentialCommand
    billing_description: str

    @classmethod
    def load(cls, path: Path) -> OpenAICompatibleProfile:
        if not isinstance(path, Path) or not path.is_file():
            _reject()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _reject()
        return cls.from_wire(raw)

    @classmethod
    def from_wire(cls, payload: Any) -> OpenAICompatibleProfile:
        if not isinstance(payload, Mapping):
            _reject()
        forbidden = {
            "api_key",
            "apikey",
            "api_token",
            "token",
            "secret",
            "authorization",
            "bearer",
            "password",
        }
        leaked = set(payload).intersection(forbidden)
        if leaked:
            _reject("literal secret fields are not allowed")
        if (
            payload.get("schema_version") != _PROFILE_SCHEMA_VERSION
            or type(payload.get("provider_id")) is not str
            or not _REVERSE_DOMAIN_PATTERN.fullmatch(payload["provider_id"])
            or type(payload.get("provider_name")) is not str
            or not payload["provider_name"]
            or type(payload.get("connection_id")) is not str
            or not _UUID_PATTERN.fullmatch(payload["connection_id"])
            or type(payload.get("provider_model_id")) is not str
            or not payload["provider_model_id"]
            or len(payload["provider_model_id"]) > 256
            or type(payload.get("display_name")) is not str
            or not payload["display_name"]
            or not _validate_ref(payload.get("endpoint_config_ref"))
            or not _validate_ref(payload.get("credential_ref"))
            or not _validate_optional_ref(payload.get("capability_snapshot_ref"))
            or type(payload.get("billing_description")) is not str
            or not payload["billing_description"]
        ):
            _reject()
        return cls(
            schema_version=_PROFILE_SCHEMA_VERSION,
            provider_id=payload["provider_id"],
            provider_name=payload["provider_name"],
            connection_id=payload["connection_id"],
            provider_model_id=payload["provider_model_id"],
            display_name=payload["display_name"],
            endpoint_config_ref=payload["endpoint_config_ref"],
            credential_ref=payload["credential_ref"],
            capability_snapshot_ref=payload.get("capability_snapshot_ref"),
            endpoint=EndpointSpec.from_wire(payload["endpoint"]),
            credential_command=CredentialCommand.from_wire(
                payload["credential_command"]
            ),
            billing_description=payload["billing_description"],
        )


def _validate_ref(value: Any) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= _MAX_REF_LENGTH
        and "\r" not in value
        and "\n" not in value
        and "\x00" not in value
    )


def _validate_optional_ref(value: Any) -> bool:
    if value is None:
        return True
    return _validate_ref(value)


def _subprocess_run(
    executable: str,
    args: list[str],
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [executable, *args],
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout_seconds,
        shell=False,
        env={},
    )


def subprocess_credential_resolver(
    command: CredentialCommand,
) -> CredentialResolver:
    """Return a ``CredentialResolver`` that invokes ``command`` per call."""

    def resolve(_reference: str) -> str:
        try:
            completed = _subprocess_run(
                command.executable,
                list(command.args),
                command.timeout_ms / 1000.0,
            )
        except subprocess.TimeoutExpired:
            _reject("credential command timed out")
        except FileNotFoundError:
            _reject("credential command executable missing")
        except OSError:
            _reject("credential command could not start")
        except Exception:
            _reject("credential command failed")
        else:
            if completed.returncode != 0:
                _reject("credential command exited non-zero")
            raw_secret = completed.stdout
            if not isinstance(raw_secret, str):
                _reject("credential command produced no secret")
            secret = raw_secret.strip("\r\n")
            if not secret or "\r" in secret or "\n" in secret:
                _reject("credential command produced no secret")
            return secret

    return resolve


def endpoint_resolver_from_records(
    records: Mapping[tuple[str, int], Mapping[str, Any]],
) -> EndpointResolver:
    """Return an ``EndpointResolver`` that matches only the declared pair."""

    normalized: dict[tuple[str, int], dict[str, Any]] = {
        key: dict(value) for key, value in records.items()
    }

    def resolve(endpoint_config_ref: str, connection_revision: int) -> OpenAICompatibleEndpointConfig:
        if (
            type(endpoint_config_ref) is not str
            or not endpoint_config_ref
            or not isinstance(connection_revision, int)
        ):
            _reject()
        record = normalized.get((endpoint_config_ref, connection_revision))
        if record is None:
            _reject()
        spec = EndpointSpec.from_wire(record)
        parsed = urllib.parse.urlsplit(spec.base_url)
        path_prefix = parsed.path or "/v1"
        return OpenAICompatibleEndpointConfig(
            host=parsed.hostname or "",
            port=parsed.port or 443,
            secure=parsed.scheme == "https",
            path_prefix=path_prefix,
            wire_mode=spec.wire_mode,
            vendor_id=spec.vendor_id,
        )

    return resolve


def compose_openai_compatible_profile(
    profile: OpenAICompatibleProfile,
    *,
    post_stream: Callable[..., Any] = default_post_stream,
    endpoint_resolver: EndpointResolver | None = None,
    credential_resolver: CredentialResolver | None = None,
    clock: Callable[[], str] | None = None,
    request_timeout: float | None = None,
    route_definition_factory: Callable[..., Any] | None = None,
) -> tuple[OpenAICompatibleExecutionPort, dict[str, Any]]:
    """Compose an executor and the engine route definitions for one profile.

    The returned ``OpenAICompatibleExecutionPort`` resolves endpoints and
    credentials through the supplied resolvers. The ``ProviderRouteDefinition``
    advertises ``tools`` as supported and ``parallel_tool_calls`` as
    unsupported, matching the executor's invariants.
    """

    if not callable(post_stream):
        _reject()
    if endpoint_resolver is None:
        endpoint_spec = profile.endpoint

        def endpoint_resolver(
            reference: str,
            connection_revision: int,
        ) -> OpenAICompatibleEndpointConfig:
            if (
                reference != profile.endpoint_config_ref
                or type(connection_revision) is not int
                or connection_revision < 0
            ):
                _reject("endpoint configuration reference is unavailable")
            parsed = urllib.parse.urlsplit(endpoint_spec.base_url)
            return OpenAICompatibleEndpointConfig(
                host=parsed.hostname or "",
                port=parsed.port or 443,
                secure=True,
                path_prefix=parsed.path or "/v1",
                wire_mode=endpoint_spec.wire_mode,
                vendor_id=endpoint_spec.vendor_id,
            )
    if not callable(endpoint_resolver):
        _reject()
    command_resolver = credential_resolver or subprocess_credential_resolver(
        profile.credential_command
    )

    def resolver(reference: str) -> str:
        if reference != profile.credential_ref:
            _reject("credential reference is unavailable")
        return command_resolver(reference)
    if not callable(command_resolver):
        _reject()
    kwargs: dict[str, Any] = {"post_stream": post_stream}
    if clock is not None:
        if not callable(clock):
            _reject()
        kwargs["clock"] = clock
    if request_timeout is not None:
        kwargs["request_timeout"] = request_timeout
    port = OpenAICompatibleExecutionPort(
        endpoint_resolver,
        resolver,
        **kwargs,
    )
    if route_definition_factory is None:
        _reject("route definition factory is required")
    definition = route_definition_factory(
        execution_mode=ExecutionMode.RESPONSES,
        capability_features=(
            CapabilityFeature("tools", CapabilityTriState.SUPPORTED),
            CapabilityFeature("parallel_tool_calls", CapabilityTriState.UNSUPPORTED),
        ),
        capability_snapshot_ref=profile.capability_snapshot_ref,
    )
    return port, {profile.provider_id: definition}
