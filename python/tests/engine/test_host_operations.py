"""Engine host operations tests: engine.v1.hosts.list and engine.v1.hosts.prepare.

B10: list/prepare are injected host-integration seams. The engine owns the
JSON-RPC envelopes, schema validation, and frozen-operation discovery; the
host integration owns the actual host enumeration, compatibility probe, and
pure launch preparation. Tests use a temp fake app bundle and a fake
process probe so no live Codex runtime is discovered or launched.
"""

from __future__ import annotations

import unittest
from typing import Any

from model_deck.engine.dispatch import EngineDispatch, principal_id_for_client_name
from model_deck.engine.hosts.ports import (
    HostConflictError,
    HostIntegrationPort,
    HostVersionMismatchError,
)
from model_deck.engine.hosts.service import HostOperationsService
from model_deck_contracts.validator import validate_schema_ref

ENGINE_ID = "550e8400-e29b-41d4-a716-446655440010"
NONCE = "nonce-host-ops"
SUPPORTED_HOST_ID = "com.openai.codex"
SUPPORTED_PROTOCOL = "1.0.0"
UNSUPPORTED_PROTOCOL = "9.9.9"


class _StaticProbe:
    """Deterministic CodexProcessProbe stand-in: never touches the OS."""

    def __init__(self, running: bool) -> None:
        self._running = running
        self.calls = 0

    def is_codex_running(self) -> bool:
        self.calls += 1
        return self._running


class _FakeHostIntegration:
    """In-memory host integration for tests, no live Codex dependency."""

    def __init__(
        self,
        *,
        host_id: str = SUPPORTED_HOST_ID,
        observed_protocol_version: str | None = SUPPORTED_PROTOCOL,
        supported_protocol_versions: frozenset[str] = frozenset({SUPPORTED_PROTOCOL}),
        probe_running: bool = False,
    ) -> None:
        self._host_id = host_id
        self._observed = observed_protocol_version
        self._supported = supported_protocol_versions
        self._probe = _StaticProbe(probe_running)
        self.list_calls = 0
        self.prepare_calls = 0
        self.last_prepare_host_id: str | None = None

    def list_hosts(self) -> list[dict[str, str]]:
        self.list_calls += 1
        if self._observed is None or self._observed not in self._supported:
            raise HostVersionMismatchError()
        descriptor = self._descriptor()
        return [descriptor]

    def prepare(self, host_id: str) -> bool:
        self.prepare_calls += 1
        self.last_prepare_host_id = host_id
        descriptor = self._descriptor()
        if host_id != descriptor["host_id"]:
            return False
        if self._observed is None or self._observed not in self._supported:
            raise HostVersionMismatchError()
        if self._probe.is_codex_running():
            raise HostConflictError("host already running unverified")
        return True

    def _descriptor(self) -> dict[str, str]:
        return {"host_id": self._host_id, "api_profile": f"codex-app-server/{self._observed or 'unknown'}"}


class _StubListModels:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> list[dict[str, Any]]:
        self.calls += 1
        return []


class _StubEnrollment:
    def __init__(self) -> None:
        self._valid_credentials = {(ENGINE_ID, NONCE): "valid"}

    def verify(self, engine_instance_id: str, instance_nonce: str, credential: str) -> bool:
        return self._valid_credentials.get((engine_instance_id, instance_nonce)) == credential


class _Identity:
    engine_instance_id = ENGINE_ID
    instance_nonce = NONCE


def _build_dispatch(host_integration: HostIntegrationPort | None) -> EngineDispatch:
    return EngineDispatch(
        list_models=_StubListModels(),  # type: ignore[arg-type]
        identity=_Identity(),
        enrollment=_StubEnrollment(),
        host_integration=host_integration,
    )


def _authenticate(dispatch: EngineDispatch, session: int) -> None:
    principal = principal_id_for_client_name("host-ops-test", ENGINE_ID)
    dispatch._authenticated_sessions.add(session)
    dispatch._connection_principals[session] = principal


def _list_call(session: int) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "list-1",
        "method": "engine.v1.hosts.list",
        "params": {},
    }


def _prepare_call(host_id: str, request_id: str = "prepare-1") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "engine.v1.hosts.prepare",
        "params": {"host_id": host_id},
    }


class HostOperationsDispatchTests(unittest.TestCase):
    """Engine dispatch wires hosts.list and hosts.prepare through HostOperationsService."""

    def setUp(self) -> None:
        self.integration = _FakeHostIntegration()
        self.dispatch = _build_dispatch(self.integration)
        _authenticate(self.dispatch, 11)

    def test_list_advertises_supported_descriptor(self) -> None:
        response = self.dispatch.handle(_list_call(11), 11)
        self.assertNotIn("error", response)
        result = response["result"]
        self.assertEqual(result["hosts"], [
            {"host_id": SUPPORTED_HOST_ID, "api_profile": f"codex-app-server/{SUPPORTED_PROTOCOL}"}
        ])
        validate_schema_ref(
            "contracts/engine.v1/methods/hosts.list.result.schema.json", result
        )

    def test_prepare_for_known_supported_host_returns_true(self) -> None:
        response = self.dispatch.handle(_prepare_call(SUPPORTED_HOST_ID), 11)
        self.assertNotIn("error", response)
        result = response["result"]
        self.assertEqual(result, {"prepared": True})
        validate_schema_ref(
            "contracts/engine.v1/methods/hosts.prepare.result.schema.json", result
        )
        self.assertEqual(self.integration.prepare_calls, 1)
        self.assertEqual(self.integration.last_prepare_host_id, SUPPORTED_HOST_ID)

    def test_prepare_for_unknown_host_returns_not_found(self) -> None:
        response = self.dispatch.handle(_prepare_call("com.example.unknown"), 11)
        self.assertIn("error", response)
        self.assertEqual(response["error"]["data"]["code"], "not_found")

    def test_list_invalid_params_return_invalid_argument(self) -> None:
        bad = {
            "jsonrpc": "2.0",
            "id": "list-bad",
            "method": "engine.v1.hosts.list",
            "params": {"host_id": "com.openai.codex"},
        }
        response = self.dispatch.handle(bad, 11)
        self.assertIn("error", response)
        self.assertEqual(response["error"]["data"]["code"], "invalid_argument")

    def test_prepare_invalid_params_return_invalid_argument(self) -> None:
        bad = {
            "jsonrpc": "2.0",
            "id": "prep-bad",
            "method": "engine.v1.hosts.prepare",
            "params": {},
        }
        response = self.dispatch.handle(bad, 11)
        self.assertIn("error", response)
        self.assertEqual(response["error"]["data"]["code"], "invalid_argument")

    def test_supported_methods_published_when_host_integration_present(self) -> None:
        implemented = self.dispatch._build_implemented_methods()
        self.assertIn("engine.v1.hosts.list", implemented)
        self.assertIn("engine.v1.hosts.prepare", implemented)

    def test_operation_catalog_includes_hosts_when_integration_present(self) -> None:
        operations = self.dispatch._operation_catalog()
        operation_ids = {entry["operation_id"] for entry in operations}
        self.assertIn("engine.v1.hosts.list", operation_ids)
        self.assertIn("engine.v1.hosts.prepare", operation_ids)
        for entry in operations:
            if entry["operation_id"] in {"engine.v1.hosts.list", "engine.v1.hosts.prepare"}:
                self.assertTrue(entry["input_schema_id"].endswith(".schema.json"))
                self.assertTrue(entry["output_schema_id"].endswith(".schema.json"))
        catalog = {"operations": operations}
        validate_schema_ref(
            "contracts/engine.v1/methods/operations.list.result.schema.json", catalog
        )


class AbsentAdapterOperationDiscoveryTests(unittest.TestCase):
    """When no host integration is supplied, list/prepare are absent."""

    def setUp(self) -> None:
        self.dispatch = _build_dispatch(None)
        _authenticate(self.dispatch, 22)

    def test_methods_absent_when_no_host_integration(self) -> None:
        implemented = self.dispatch._build_implemented_methods()
        self.assertNotIn("engine.v1.hosts.list", implemented)
        self.assertNotIn("engine.v1.hosts.prepare", implemented)

    def test_list_without_integration_returns_unsupported_capability(self) -> None:
        response = self.dispatch.handle(_list_call(22), 22)
        self.assertIn("error", response)
        self.assertEqual(response["error"]["data"]["code"], "unsupported_capability")

    def test_prepare_without_integration_returns_unsupported_capability(self) -> None:
        response = self.dispatch.handle(_prepare_call(SUPPORTED_HOST_ID), 22)
        self.assertIn("error", response)
        self.assertEqual(response["error"]["data"]["code"], "unsupported_capability")


class HostOperationsServiceVersionTests(unittest.TestCase):
    """The service surfaces version mismatch and conflict errors consistently."""

    def _make(self, *, observed: str | None, running: bool = False) -> _FakeHostIntegration:
        return _FakeHostIntegration(observed_protocol_version=observed, probe_running=running)

    def test_unknown_protocol_maps_to_version_mismatch(self) -> None:
        service = HostOperationsService(integration=self._make(observed=None))
        with self.assertRaises(HostOperationsService.ListError) as ctx:
            service.list_hosts()
        self.assertEqual(ctx.exception.code, "version_mismatch")

    def test_unsupported_protocol_maps_to_version_mismatch(self) -> None:
        service = HostOperationsService(integration=self._make(observed=UNSUPPORTED_PROTOCOL))
        with self.assertRaises(HostOperationsService.ListError) as ctx:
            service.list_hosts()
        self.assertEqual(ctx.exception.code, "version_mismatch")

    def test_already_running_maps_to_conflict(self) -> None:
        service = HostOperationsService(integration=self._make(observed=SUPPORTED_PROTOCOL, running=True))
        with self.assertRaises(HostOperationsService.PrepareError) as ctx:
            service.prepare(SUPPORTED_HOST_ID)
        self.assertEqual(ctx.exception.code, "conflict")

    def test_unknown_host_maps_to_not_found(self) -> None:
        service = HostOperationsService(integration=self._make(observed=SUPPORTED_PROTOCOL))
        with self.assertRaises(HostOperationsService.PrepareError) as ctx:
            service.prepare("com.example.missing")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_unsupported_host_id_in_list_is_unsupported_capability(self) -> None:
        service = HostOperationsService(integration=self._make(observed=SUPPORTED_PROTOCOL))
        with self.assertRaises(HostOperationsService.PrepareError) as ctx:
            service.prepare("not-a-reverse-domain")
        self.assertEqual(ctx.exception.code, "unsupported_capability")


class HostOperationsServiceSchemaTests(unittest.TestCase):
    """Service returns schema-valid envelopes on the happy path."""

    def test_list_envelope_matches_schema(self) -> None:
        service = HostOperationsService(integration=_FakeHostIntegration())
        result = service.list_hosts_envelope()
        validate_schema_ref(
            "contracts/engine.v1/methods/hosts.list.result.schema.json", result
        )

    def test_prepare_envelope_matches_schema(self) -> None:
        service = HostOperationsService(integration=_FakeHostIntegration())
        result = service.prepare_envelope(SUPPORTED_HOST_ID)
        validate_schema_ref(
            "contracts/engine.v1/methods/hosts.prepare.result.schema.json", result
        )


if __name__ == "__main__":
    unittest.main()
