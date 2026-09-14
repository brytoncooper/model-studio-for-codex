"""B06 MCP write seam tests.

These tests exercise ``McpModelWriteService`` with fake transports and
resolvers so the seam is observable without a live engine. The engine
contracts are described in
``python/src/model_deck/engine/dispatch.py`` (operations) and
``python/src/model_deck/engine/model_library/use_cases.py`` (use cases).

The tests do not depend on the live application or any host files; the
service is built with fakes only. ``test_engine_transport.py`` covers
the real ``UnixSocketEngineClient``+``build_engine_server`` path; one
end-to-end test there verifies the converged MCP path against a real
engine server.

Goal: prove red-then-green for register/rename/remove through the B06
write service, with deterministic endpoint resolution, idempotency-key
synthesis, and error mapping from engine domain errors to ``DeckError``.
"""
import unittest
from typing import Any

from model_deck.integrations.clients.mcp.errors import McpWriteError, McpEngineError
from model_deck.integrations.clients.mcp.registry_resolver import EngineConnectionResolver


CONNECTION = "550e8400-e29b-41d4-a716-446655440002"
SECOND_CONNECTION = "660e8400-e29b-41d4-a716-446655440003"
REGISTRATION = "770e8400-e29b-41d4-a716-446655440004"
MODEL_ID = "deepseek/deepseek-v4.1-flash"


class _RecordingTransport:
    """Captures every engine JSON-RPC call. Returns whatever the test queued."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses: list[dict[str, Any]] = []
        self._errors: list[Exception] = []
        self._index = 0

    def queue(self, response: dict[str, Any]) -> None:
        self._responses.append(response)

    def queue_error(self, error: Exception) -> None:
        self._errors.append(error)

    def call_engine(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        if self._index < len(self._errors) and isinstance(self._errors[self._index], Exception):
            err = self._errors[self._index]
            self._index += 1
            raise err
        response = self._responses[self._index]
        self._index += 1
        return response


class _StaticTransport:
    """Returns canned responses keyed by method; never raises."""

    def __init__(self, responses: dict[str, dict[str, Any]]):
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_engine(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        if method not in self.responses:
            raise AssertionError(f"unexpected method {method} with {params}")
        return self.responses[method]


class _SoleConnectionResolver:
    def __init__(self, connection_id: str = CONNECTION):
        self.connection_id = connection_id
        self.calls: list[str | None] = []

    def resolve(self, endpoint: str | None) -> str:
        self.calls.append(endpoint)
        return self.connection_id


class _FixedLocator:
    def __init__(self, mapping: dict[str, tuple[str, int]] | None = None) -> None:
        self.mapping = mapping or {}
        self.calls: list[str] = []

    def find(self, provider_model_id: str) -> tuple[str, int] | None:
        self.calls.append(provider_model_id)
        return self.mapping.get(provider_model_id)


class _AnyRegisteredLocator:
    """Returns a registration so rename/remove reach the engine call path."""

    def __init__(self, registration_id: str = REGISTRATION, revision: int = 1) -> None:
        self._registration_id = registration_id
        self._revision = revision
        self.calls: list[str] = []

    def find(self, provider_model_id: str) -> tuple[str, int] | None:
        self.calls.append(provider_model_id)
        return (self._registration_id, self._revision)


class _FakePresentation:
    def endpoint_name(self, connection_id: str) -> str:
        if connection_id == CONNECTION:
            return "OpenRouter"
        return connection_id

    def billing_for(self, connection_id: str) -> tuple[str, str | None]:
        if connection_id == CONNECTION:
            return ("OpenRouter credits", "Pay per token; no ChatGPT subscription impact.")
        return ("none (local)", None)

    def price_for(self, provider_model_id: str, connection_id: str) -> str:
        if provider_model_id == MODEL_ID:
            return "in $0.15/M · out $0.6/M"
        return "n/a"

    def host_role(self, provider_model_id: str) -> str | None:
        if provider_model_id == MODEL_ID:
            return "deepseek_role"
        return None


def _import_service():
    """Late import so this module can be collected before service is implemented (red)."""
    from model_deck.integrations.clients.mcp.writes import McpModelWriteService
    return McpModelWriteService


def _build_service(transport, *, resolver=None, locator=None, presentation=None):
    service_cls = _import_service()
    return service_cls(
        transport=transport,
        connection_resolver=resolver or _SoleConnectionResolver(),
        registered_locator=locator or _FixedLocator(),
        presentation=presentation or _FakePresentation(),
    )


class RegisterTests(unittest.TestCase):
    def test_connection_resolver_reads_fresh_engine_state_for_each_operation(self):
        class _ChangingTransport:
            def __init__(self) -> None:
                self.calls = 0

            def call_engine(self, method, params):
                self.calls += 1
                return {
                    "connections": [{
                        "connection_id": CONNECTION,
                        "provider_id": f"provider-{self.calls}",
                    }]
                }

        transport = _ChangingTransport()
        resolver = EngineConnectionResolver(transport)

        self.assertEqual(resolver.resolve("provider-1"), CONNECTION)
        self.assertEqual(resolver.resolve("provider-2"), CONNECTION)
        self.assertEqual(transport.calls, 2)

    def test_register_model_sends_single_models_register_call_when_resolver_supplies_connection(self):
        transport = _StaticTransport({
            "engine.v1.models.register": {
                "model": {
                    "registration_id": REGISTRATION,
                    "provider_model_id": MODEL_ID,
                    "connection_id": CONNECTION,
                    "display_name": "DeepSeek",
                    "revision": 1,
                }
            }
        })
        service = _build_service(transport, resolver=_SoleConnectionResolver())
        result = service.register_model(MODEL_ID, display_name="DeepSeek")
        self.assertEqual(len(transport.calls), 1)
        method, params = transport.calls[0]
        self.assertEqual(method, "engine.v1.models.register")
        self.assertEqual(params["connection_id"], CONNECTION)
        self.assertEqual(params["provider_model_id"], MODEL_ID)
        self.assertEqual(params["display_name"], "DeepSeek")
        self.assertEqual(params["expected_revision"], 0)
        self.assertEqual(len(params["idempotency_key"]), 36)  # UUID4
        # envelope
        self.assertEqual(result["model"], MODEL_ID)
        self.assertEqual(result["endpoint"], "OpenRouter")
        self.assertEqual(result["name"], "DeepSeek")
        self.assertEqual(result["price"], "in $0.15/M · out $0.6/M")
        self.assertEqual(result["billing"], "OpenRouter credits")
        self.assertEqual(result["role"], "deepseek_role")
        self.assertFalse(result["already_registered"])
        self.assertIn("spawn it with spawn_agent model=", result["message"])

    def test_register_model_resolves_endpoint_via_engine_when_resolver_fetches(self):
        transport = _StaticTransport({
            "engine.v1.connections.list": {"connections": [{"connection_id": CONNECTION, "provider_id": "openrouter"}]},
            "engine.v1.models.register": {
                "model": {"registration_id": REGISTRATION, "provider_model_id": MODEL_ID,
                          "connection_id": CONNECTION, "display_name": "DeepSeek", "revision": 1}
            },
        })
        resolver = EngineConnectionResolver(transport)
        service = _build_service(transport, resolver=resolver)
        result = service.register_model(MODEL_ID, display_name="DeepSeek")
        self.assertEqual(result["ok"], True)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[0][0], "engine.v1.connections.list")
        self.assertEqual(transport.calls[1][0], "engine.v1.models.register")

    def test_register_model_raises_when_endpoint_ambiguous(self):
        class _MultiResolver:
            def resolve(self, endpoint: str | None) -> str:
                raise McpWriteError("more than one connection; pass an explicit endpoint")

        transport = _StaticTransport({})
        service = _build_service(transport, resolver=_MultiResolver())
        with self.assertRaises(McpWriteError):
            service.register_model(MODEL_ID, display_name="DeepSeek")
        self.assertEqual(transport.calls, [])

    def test_register_model_chains_display_name_rename_when_provided(self):
        transport = _StaticTransport({
            "engine.v1.models.register": {
                "model": {"registration_id": REGISTRATION, "provider_model_id": MODEL_ID,
                          "connection_id": CONNECTION, "display_name": "Auto", "revision": 1}
            },
            "engine.v1.models.rename": {
                "model": {"registration_id": REGISTRATION, "provider_model_id": MODEL_ID,
                          "connection_id": CONNECTION, "display_name": "Friendly", "revision": 2}
            },
        })
        locator = _FixedLocator({MODEL_ID: (REGISTRATION, 1)})
        service = _build_service(transport, locator=locator)
        result = service.register_model(MODEL_ID, display_name="Friendly")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[1][0], "engine.v1.models.rename")
        rename_params = transport.calls[1][1]
        self.assertEqual(rename_params["registration_id"], REGISTRATION)
        self.assertEqual(rename_params["display_name"], "Friendly")
        self.assertEqual(rename_params["expected_revision"], 1)
        self.assertEqual(result["name"], "Friendly")


class RenameTests(unittest.TestCase):
    def test_rename_resolves_registration_then_calls_rename_with_current_revision(self):
        transport = _StaticTransport({
            "engine.v1.models.rename": {
                "model": {"registration_id": REGISTRATION, "provider_model_id": MODEL_ID,
                          "connection_id": CONNECTION, "display_name": "Friendly", "revision": 5}
            }
        })
        locator = _FixedLocator({MODEL_ID: (REGISTRATION, 4)})
        service = _build_service(transport, locator=locator)
        result = service.rename_model(MODEL_ID, name="Friendly")
        self.assertEqual(len(transport.calls), 1)
        method, params = transport.calls[0]
        self.assertEqual(method, "engine.v1.models.rename")
        self.assertEqual(params["registration_id"], REGISTRATION)
        self.assertEqual(params["expected_revision"], 4)
        self.assertEqual(len(params["idempotency_key"]), 36)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["model"], MODEL_ID)
        self.assertEqual(result["name"], "Friendly")

    def test_rename_unknown_model_raises_not_found(self):
        transport = _StaticTransport({})
        locator = _FixedLocator({})
        service = _build_service(transport, locator=locator)
        with self.assertRaises(McpWriteError) as ctx:
            service.rename_model(MODEL_ID, name="x")
        self.assertIn("not found", str(ctx.exception).lower())


class RemoveTests(unittest.TestCase):
    def test_remove_resolves_then_calls_remove(self):
        transport = _StaticTransport({
            "engine.v1.models.remove": {"removed": True}
        })
        locator = _FixedLocator({MODEL_ID: (REGISTRATION, 7)})
        service = _build_service(transport, locator=locator)
        result = service.remove_model(MODEL_ID)
        self.assertEqual(len(transport.calls), 1)
        method, params = transport.calls[0]
        self.assertEqual(method, "engine.v1.models.remove")
        self.assertEqual(params["registration_id"], REGISTRATION)
        self.assertEqual(params["expected_revision"], 7)
        self.assertEqual(len(params["idempotency_key"]), 36)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["model"], MODEL_ID)

    def test_remove_unknown_model_raises_not_found(self):
        transport = _StaticTransport({})
        locator = _FixedLocator({})
        service = _build_service(transport, locator=locator)
        with self.assertRaises(McpWriteError):
            service.remove_model(MODEL_ID)


class EngineErrorMappingTests(unittest.TestCase):
    def _service_with_engine_error(self, error_envelope):
        transport = _RecordingTransport()
        transport.queue_error(McpEngineError(error_envelope))
        return _build_service(transport, locator=_AnyRegisteredLocator())

    def test_conflict_maps_to_deck_error_with_conflict_message(self):
        service = self._service_with_engine_error(
            {"error": {"code": "conflict", "message": "revision mismatch"}}
        )
        with self.assertRaises(McpWriteError) as ctx:
            service.rename_model(MODEL_ID, name="x")
        self.assertIn("conflict", str(ctx.exception).lower())

    def test_not_found_maps_to_deck_error_with_not_found_message(self):
        service = self._service_with_engine_error(
            {"error": {"code": "not_found", "message": "registration missing"}}
        )
        with self.assertRaises(McpWriteError) as ctx:
            service.rename_model(MODEL_ID, name="x")
        self.assertIn("not found", str(ctx.exception).lower())

    def test_unsupported_capability_surfaces_as_deck_error(self):
        service = self._service_with_engine_error(
            {"error": {"code": "unsupported_capability", "message": "models.rename not configured"}}
        )
        with self.assertRaises(McpWriteError):
            service.rename_model(MODEL_ID, name="x")

    def test_invalid_params_maps_to_deck_error(self):
        service = self._service_with_engine_error(
            {"error": {"code": -32602, "message": "provider_model_id is required"}}
        )
        with self.assertRaises(McpWriteError) as ctx:
            service.register_model(MODEL_ID)
        self.assertIn("provider_model_id", str(ctx.exception))


class IdempotencyKeyTests(unittest.TestCase):
    def test_idempotency_keys_are_unique_and_bounded(self):
        transport = _StaticTransport({
            "engine.v1.models.register": {
                "model": {"registration_id": REGISTRATION, "provider_model_id": MODEL_ID,
                          "connection_id": CONNECTION, "display_name": "x", "revision": 1}
            }
        })
        service = _build_service(transport)
        seen: list[str] = []
        for _ in range(5):
            transport.calls.clear()
            service.register_model(MODEL_ID, display_name="x")
            seen.append(transport.calls[0][1]["idempotency_key"])
        self.assertEqual(len(set(seen)), 5)
        for key in seen:
            self.assertLessEqual(len(key), 128)


if __name__ == "__main__":
    unittest.main()
