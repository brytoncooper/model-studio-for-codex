from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from model_deck_contracts.negotiation import ApiVersion, evaluate_api_version


class RendezvousError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RendezvousDescriptor:
    transport: str
    socket_path: Path
    engine_instance_id: str
    instance_nonce: str
    api_profile: ApiVersion


_ALLOWED_KEYS = frozenset(
    {
        "transport",
        "socket_path",
        "engine_instance_id",
        "instance_nonce",
        "api_profile",
    }
)


def build_rendezvous_payload(
    *,
    socket_path: Path,
    engine_instance_id: str,
    instance_nonce: str,
    api_profile: ApiVersion | None = None,
) -> dict[str, Any]:
    profile = api_profile or ApiVersion(1, 0)
    return {
        "transport": "unix",
        "socket_path": str(socket_path.resolve()),
        "engine_instance_id": engine_instance_id,
        "instance_nonce": instance_nonce,
        "api_profile": {"major": profile.major, "minor": profile.minor},
    }


def _validate_engine_instance_uuid(engine_instance_id: str) -> None:
    try:
        parsed = uuid.UUID(engine_instance_id)
    except ValueError as exc:
        raise RendezvousError("engine_instance_id must be a UUID") from exc
    if str(parsed).lower() != engine_instance_id.lower():
        raise RendezvousError("engine_instance_id must be a canonical UUID string")


def parse_rendezvous_mapping(data: Mapping[str, Any]) -> RendezvousDescriptor:
    extra = set(data.keys()) - _ALLOWED_KEYS
    if extra:
        raise RendezvousError(f"unexpected rendezvous fields: {sorted(extra)}")
    transport = data.get("transport")
    if transport != "unix":
        raise RendezvousError("transport must be unix")
    socket_raw = data.get("socket_path")
    if not isinstance(socket_raw, str) or not socket_raw:
        raise RendezvousError("socket_path must be a non-empty string")
    socket_path = Path(socket_raw)
    if not socket_path.is_absolute():
        raise RendezvousError("socket_path must be absolute")
    engine_instance_id = data.get("engine_instance_id")
    instance_nonce = data.get("instance_nonce")
    if not isinstance(engine_instance_id, str) or not engine_instance_id:
        raise RendezvousError("engine_instance_id required")
    if not isinstance(instance_nonce, str) or not instance_nonce:
        raise RendezvousError("instance_nonce required")
    _validate_engine_instance_uuid(engine_instance_id)
    api_raw = data.get("api_profile")
    if not isinstance(api_raw, dict):
        raise RendezvousError("api_profile required")
    offered = ApiVersion.parse(api_raw)
    server = ApiVersion(1, 0)
    if not evaluate_api_version(offered, server).ok:
        raise RendezvousError("api_profile incompatible with engine 1.0")
    return RendezvousDescriptor(
        transport="unix",
        socket_path=socket_path,
        engine_instance_id=engine_instance_id,
        instance_nonce=instance_nonce,
        api_profile=offered,
    )


def load_rendezvous_file(path: Path) -> RendezvousDescriptor:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RendezvousError("rendezvous file must contain a JSON object")
    return parse_rendezvous_mapping(payload)


def publish_rendezvous_file(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2) + chr(10), encoding="utf-8")
