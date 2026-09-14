"""B06 MCP write seam.

The write service routes ``add_model`` / ``remove_model`` /
``set_display_name`` through the authenticated engine JSON-RPC API when
an isolated engine rendezvous and credential are configured, and
preserves the legacy application adapter path otherwise.

The service translates engine domain errors into agent-visible
``DeckError`` messages so callers see the same shapes they did before
the migration. Mapping is late-bound to avoid the circular import
between this module and ``model_deck_mcp``.
"""
from __future__ import annotations

import uuid
from typing import Any

from model_deck.integrations.clients.mcp.errors import McpEngineError, McpWriteError
from model_deck.integrations.clients.mcp.ports import (
    McpConnectionResolver,
    McpEngineTransport,
    McpRegisteredModelLocator,
    McpRegisteredModelPresentation,
)


from model_deck.integrations.clients.mcp.presenters import (
    present_register_result,
    present_remove_result,
    present_rename_result,
)


_REGISTER = "engine.v1.models.register"
_RENAME = "engine.v1.models.rename"
_REMOVE = "engine.v1.models.remove"
_LIST = "engine.v1.models.list"


class McpModelWriteService:
    """High-level write seam that adapts engine results to legacy MCP envelopes."""

    def __init__(
        self,
        *,
        transport: McpEngineTransport,
        connection_resolver: McpConnectionResolver,
        registered_locator: McpRegisteredModelLocator,
        presentation: McpRegisteredModelPresentation,
    ) -> None:
        self._transport = transport
        self._resolver = connection_resolver
        self._locator = registered_locator
        self._presentation = presentation

    # -- public surface ------------------------------------------------------------------------

    def register_model(
        self,
        provider_model_id: str,
        *,
        endpoint: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        connection_id = self._resolver.resolve(endpoint)
        register_params = {
            "connection_id": connection_id,
            "provider_model_id": provider_model_id,
            "display_name": display_name or provider_model_id,
            "expected_revision": 0,
            "idempotency_key": _new_idempotency_key(),
        }
        try:
            register_result = self._transport.call_engine(_REGISTER, register_params)
        except McpEngineError as exc:
            raise self._map_register_error(provider_model_id, display_name, exc) from exc

        record = register_result.get("model", {}) if isinstance(register_result, dict) else {}
        actual_name = str(record.get("display_name") or display_name or provider_model_id)
        already_registered = False

        if display_name and display_name != actual_name:
            registration_id = record.get("registration_id")
            revision = record.get("revision", 0)
            if isinstance(registration_id, str) and isinstance(revision, int):
                rename_params = {
                    "registration_id": registration_id,
                    "display_name": display_name,
                    "expected_revision": revision,
                    "idempotency_key": _new_idempotency_key(),
                }
                try:
                    rename_result = self._transport.call_engine(_RENAME, rename_params)
                except McpEngineError as exc:
                    raise self._map_rename_error(provider_model_id, exc) from exc
                register_result = rename_result
                record = rename_result.get("model", {}) if isinstance(rename_result, dict) else {}
                actual_name = str(record.get("display_name") or display_name)

        endpoint_name = self._presentation.endpoint_name(connection_id)
        billing, billing_note = self._presentation.billing_for(connection_id)
        price = self._presentation.price_for(provider_model_id, connection_id)
        role = self._presentation.host_role(provider_model_id)
        return present_register_result(
            register_result,
            endpoint=endpoint_name,
            billing=billing,
            billing_note=billing_note,
            price=price,
            already_registered=already_registered,
            role=role,
        )

    def rename_model(self, provider_model_id: str, *, name: str | None) -> dict[str, Any]:
        located = self._locator.find(provider_model_id)
        if located is None:
            raise McpWriteError(f"model not found: {provider_model_id}")
        registration_id, revision = located
        rename_params = {
            "registration_id": registration_id,
            "display_name": name if isinstance(name, str) and name.strip() else "",
            "expected_revision": revision,
            "idempotency_key": _new_idempotency_key(),
        }
        try:
            result = self._transport.call_engine(_RENAME, rename_params)
        except McpEngineError as exc:
            raise self._map_rename_error(provider_model_id, exc) from exc
        return present_rename_result(result, requested_name=str(name or ""))

    def remove_model(self, provider_model_id: str) -> dict[str, Any]:
        located = self._locator.find(provider_model_id)
        if located is None:
            raise McpWriteError(f"model not found: {provider_model_id}")
        registration_id, revision = located
        remove_params = {
            "registration_id": registration_id,
            "expected_revision": revision,
            "idempotency_key": _new_idempotency_key(),
        }
        try:
            result = self._transport.call_engine(_REMOVE, remove_params)
        except McpEngineError as exc:
            raise self._map_remove_error(provider_model_id, exc) from exc
        return present_remove_result(result, provider_model_id=provider_model_id)

    # -- error mapping -------------------------------------------------------------------------

    def map_engine_error(self, error: McpEngineError, *, action: str) -> McpWriteError:
        code = error.code
        message = error.message
        if code == "conflict":
            return McpWriteError(f"conflict while {action}: {message}")
        if code == "not_found":
            return McpWriteError(f"not found while {action}: {message}")
        if code == "unsupported_capability":
            return McpWriteError(f"engine capability missing for {action}: {message}")
        if code == "capability_denied":
            return McpWriteError(f"engine rejected {action}: {message}")
        if code == -32602 or code == "invalid_params":
            return McpWriteError(f"invalid request for {action}: {message}")
        return McpWriteError(f"engine error during {action}: {message}")

    def _map_register_error(self, provider_model_id: str, display_name: str | None, exc: McpEngineError) -> McpWriteError:
        if exc.code == "conflict" and isinstance(exc.message, str) and "already" in exc.message.lower():
            return self._recover_already_registered(provider_model_id, display_name)
        return self.map_engine_error(exc, action=f"registering {provider_model_id}")

    def _map_rename_error(self, provider_model_id: str, exc: McpEngineError) -> McpWriteError:
        return self.map_engine_error(exc, action=f"renaming {provider_model_id}")

    def _map_remove_error(self, provider_model_id: str, exc: McpEngineError) -> McpWriteError:
        return self.map_engine_error(exc, action=f"removing {provider_model_id}")

    def _recover_already_registered(self, provider_model_id: str, display_name: str | None) -> McpWriteError:
        # Conflict during register most commonly means the model is already
        # registered. Re-fetch authoritative state so the caller can decide.
        try:
            response = self._transport.call_engine(_LIST, {"collection": "registered"})
        except McpEngineError as exc:
            return self.map_engine_error(exc, action=f"re-checking {provider_model_id}")
        items = response.get("items", []) if isinstance(response, dict) else []
        for row in items:
            if isinstance(row, dict) and row.get("provider_model_id") == provider_model_id:
                connection_id = str(row.get("connection_id", ""))
                endpoint_name = self._presentation.endpoint_name(connection_id)
                billing, billing_note = self._presentation.billing_for(connection_id)
                price = self._presentation.price_for(provider_model_id, connection_id)
                payload = present_register_result(
                    {"model": row},
                    endpoint=endpoint_name,
                    billing=billing,
                    billing_note=billing_note,
                    price=price,
                    already_registered=True,
                    role=self._presentation.host_role(provider_model_id),
                )
                # The raise carries a structured payload via args; tools
                # translate to DeckError and surface the same JSON.
                return McpWriteError(_payload_message(payload))
        return McpWriteError(f"conflict while registering {provider_model_id}")


def _new_idempotency_key() -> str:
    return str(uuid.uuid4())


def _payload_message(payload: dict[str, Any]) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False)
