from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class ApiVersion:
    major: int
    minor: int

    @classmethod
    def parse(cls, value: Mapping[str, object]) -> ApiVersion:
        major = value.get("major")
        minor = value.get("minor")
        if not isinstance(major, int) or not isinstance(minor, int):
            raise ValueError("api version major and minor must be integers")
        return cls(major=major, minor=minor)


class NegotiationFailure(str, Enum):
    INCOMPATIBLE_MAJOR = "version_mismatch"
    INSUFFICIENT_MINOR = "version_mismatch"
    MISSING_CAPABILITIES = "unsupported_capability"


@dataclass(frozen=True, slots=True)
class NegotiationResult:
    ok: bool
    failure: NegotiationFailure | None = None
    missing_capabilities: tuple[str, ...] = ()


def evaluate_api_version(offered: ApiVersion, server: ApiVersion) -> NegotiationResult:
    if offered.major != server.major:
        return NegotiationResult(ok=False, failure=NegotiationFailure.INCOMPATIBLE_MAJOR)
    if server.minor < offered.minor:
        return NegotiationResult(ok=False, failure=NegotiationFailure.INSUFFICIENT_MINOR)
    return NegotiationResult(ok=True)


def missing_required_capabilities(
    required: Sequence[str],
    server_features: Mapping[str, str],
) -> tuple[str, ...]:
    missing: list[str] = []
    for name in required:
        state = server_features.get(name)
        if state != "supported":
            missing.append(name)
    return tuple(missing)


def evaluate_required_capabilities(
    required: Sequence[str],
    server_features: Mapping[str, str],
) -> NegotiationResult:
    missing = missing_required_capabilities(required, server_features)
    if missing:
        return NegotiationResult(
            ok=False,
            failure=NegotiationFailure.MISSING_CAPABILITIES,
            missing_capabilities=missing,
        )
    return NegotiationResult(ok=True)


def evaluate_hello_negotiation(
    offered: ApiVersion,
    server: ApiVersion,
    required_capabilities: Sequence[str],
    server_features: Mapping[str, str],
) -> NegotiationResult:
    version_result = evaluate_api_version(offered, server)
    if not version_result.ok:
        return version_result
    return evaluate_required_capabilities(required_capabilities, server_features)


def rendezvous_matches(
    observed_engine_instance_id: str,
    observed_instance_nonce: str,
    expected_engine_instance_id: str,
    expected_instance_nonce: str,
) -> bool:
    return (
        observed_engine_instance_id == expected_engine_instance_id
        and observed_instance_nonce == expected_instance_nonce
    )
