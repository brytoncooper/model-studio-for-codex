from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from model_deck_contracts.validator import validate_schema_ref


def _parse(document: str, definition: str, data: dict[str, Any], cls):
    pointer = definition if definition.startswith("/") else f"/{definition}"
    validate_schema_ref(f"{document}#{pointer}", data)
    return cls(raw=dict(data))


@dataclass(frozen=True)
class RunRequest:
    raw: dict[str, Any]

    @classmethod
    def parse(cls, data: dict[str, Any]) -> RunRequest:
        return _parse(
            "contracts/engine.v1/vocabulary.schema.json",
            "/definitions/run_request",
            data,
            cls,
        )


@dataclass(frozen=True)
class RouteSnapshot:
    raw: dict[str, Any]

    @classmethod
    def parse(cls, data: dict[str, Any]) -> RouteSnapshot:
        return _parse(
            "contracts/engine.v1/vocabulary.schema.json",
            "/definitions/route_snapshot",
            data,
            cls,
        )


@dataclass(frozen=True)
class JsonRpcRequest:
    raw: dict[str, Any]

    @classmethod
    def parse(cls, data: dict[str, Any]) -> JsonRpcRequest:
        return _parse(
            "contracts/common/jsonrpc.schema.json",
            "/definitions/request",
            data,
            cls,
        )


@dataclass(frozen=True)
class RunEventRunCompleted:
    raw: dict[str, Any]

    @classmethod
    def parse(cls, data: dict[str, Any]) -> RunEventRunCompleted:
        return _parse(
            "contracts/engine.v1/vocabulary.schema.json",
            "/definitions/run_event_run_completed",
            data,
            cls,
        )


@dataclass(frozen=True)
class ToolCall:
    raw: dict[str, Any]

    @classmethod
    def parse(cls, data: dict[str, Any]) -> ToolCall:
        return _parse(
            "contracts/engine.v1/vocabulary.schema.json",
            "/definitions/tool_call",
            data,
            cls,
        )
