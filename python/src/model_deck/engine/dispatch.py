from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any, Protocol

from model_deck_contracts.negotiation import (
    ApiVersion,
    NegotiationFailure,
    evaluate_hello_negotiation,
    rendezvous_matches,
)
from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref
from model_deck.engine.connections.ports import (
    ConnectionIdempotencyConflictError,
    ConnectionRevisionConflictError,
)
from model_deck.engine.connections.use_cases import ListConnectionsUseCase, SaveConnectionUseCase
from model_deck.engine.model_library.ports import (
    CatalogUnavailableError,
    ModelIdempotencyConflictError,
    ModelRegistrationNotFoundError,
    ModelRevisionConflictError,
)
from model_deck.engine.model_library.use_cases import (
    ListModelsUseCase,
    RegisterModelUseCase,
    RemoveModelUseCase,
    RenameModelUseCase,
    UnsupportedCollectionError,
)

SERVER_API = ApiVersion(1, 0)
SERVER_FEATURES = {"tools": "unsupported", "compaction": "unknown"}

_BASE_IMPLEMENTED_METHODS = frozenset(
    {
        "engine.v1.hello",
        "engine.v1.health",
        "engine.v1.operations.list",
        "engine.v1.capabilities.get",
        "engine.v1.models.list",
    }
)

_B07_METHODS = frozenset(
    {
        "engine.v1.models.register",
        "engine.v1.models.rename",
        "engine.v1.models.remove",
        "engine.v1.connections.list",
        "engine.v1.connections.save",
    }
)

_OPERATION_CATALOG: tuple[dict[str, str], ...] = (
    {
        "operation_id": "engine.v1.hello",
        "input_schema_id": "contracts/engine.v1/methods/hello.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/hello.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.health",
        "input_schema_id": "contracts/engine.v1/methods/health.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/health.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.operations.list",
        "input_schema_id": "contracts/engine.v1/methods/operations.list.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/operations.list.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.capabilities.get",
        "input_schema_id": "contracts/engine.v1/methods/capabilities.get.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/capabilities.get.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.models.list",
        "input_schema_id": "contracts/engine.v1/methods/models.list.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/models.list.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.models.register",
        "input_schema_id": "contracts/engine.v1/methods/models.register.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/models.register.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.models.rename",
        "input_schema_id": "contracts/engine.v1/methods/models.rename.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/models.rename.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.models.remove",
        "input_schema_id": "contracts/engine.v1/methods/models.remove.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/models.remove.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.connections.list",
        "input_schema_id": "contracts/engine.v1/methods/connections.list.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/connections.list.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.connections.save",
        "input_schema_id": "contracts/engine.v1/methods/connections.save.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/connections.save.result.schema.json",
        "effect": "write",
    },
)


class EngineInstanceIdentity(Protocol):
    engine_instance_id: str
    instance_nonce: str


class EnrollmentVerifier(Protocol):
    def verify(self, engine_instance_id: str, instance_nonce: str, credential: str) -> bool: ...


class EngineDispatch:
    def __init__(
        self,
        list_models: ListModelsUseCase,
        identity: EngineInstanceIdentity,
        enrollment: EnrollmentVerifier,
        *,
        register_model: RegisterModelUseCase | None = None,
        rename_model: RenameModelUseCase | None = None,
        remove_model: RemoveModelUseCase | None = None,
        list_connections: ListConnectionsUseCase | None = None,
        save_connection: SaveConnectionUseCase | None = None,
    ) -> None:
        self._list_models = list_models
        self._identity = identity
        self._enrollment = enrollment
        self._register_model = register_model
        self._rename_model = rename_model
        self._remove_model = remove_model
        self._list_connections = list_connections
        self._save_connection = save_connection
        self._implemented_methods = self._build_implemented_methods()
        self._authenticated_sessions: set[int] = set()
        self._lock = threading.Lock()

    def _build_implemented_methods(self) -> frozenset[str]:
        methods = set(_BASE_IMPLEMENTED_METHODS)
        if self._register_model is not None:
            methods.add("engine.v1.models.register")
        if self._rename_model is not None:
            methods.add("engine.v1.models.rename")
        if self._remove_model is not None:
            methods.add("engine.v1.models.remove")
        if self._list_connections is not None:
            methods.add("engine.v1.connections.list")
        if self._save_connection is not None:
            methods.add("engine.v1.connections.save")
        return frozenset(methods)

    def disconnect(self, connection_id: int) -> None:
        with self._lock:
            self._authenticated_sessions.discard(connection_id)

    def handle(self, frame: dict[str, Any], connection_id: int) -> dict[str, Any] | None:
        if frame.get("jsonrpc") != "2.0":
            return self._error(frame.get("id"), -32600, "invalid request")
        method = frame.get("method")
        if not isinstance(method, str):
            return self._error(frame.get("id"), -32600, "invalid request")
        if method not in self._implemented_methods:
            if method in _B07_METHODS:
                return self._domain_error(
                    frame.get("id"),
                    "unsupported_capability",
                    f"method not implemented: {method}",
                )
            return self._domain_error(
                frame.get("id"),
                "unsupported_capability",
                f"method not implemented: {method}",
            )
        if "params" not in frame:
            params: dict[str, Any] = {}
        else:
            raw_params = frame["params"]
            if raw_params is None:
                return self._error(frame.get("id"), -32602, "invalid params")
            if isinstance(raw_params, dict):
                params = raw_params
            else:
                return self._error(frame.get("id"), -32602, "invalid params")
        if method == "engine.v1.hello":
            return self._hello(frame.get("id"), params, connection_id)
        if not self._is_authenticated(connection_id):
            return self._domain_error(frame.get("id"), "capability_denied", "authentication required")
        if method == "engine.v1.health":
            return self._success(frame.get("id"), {"status": "ok"})
        if method == "engine.v1.operations.list":
            return self._operations_list(frame.get("id"))
        if method == "engine.v1.capabilities.get":
            return self._success(frame.get("id"), {"features": SERVER_FEATURES})
        if method == "engine.v1.models.list":
            return self._models_list(frame.get("id"), params)
        if method == "engine.v1.models.register":
            return self._models_register(frame.get("id"), params)
        if method == "engine.v1.models.rename":
            return self._models_rename(frame.get("id"), params)
        if method == "engine.v1.models.remove":
            return self._models_remove(frame.get("id"), params)
        if method == "engine.v1.connections.list":
            return self._connections_list(frame.get("id"), params)
        if method == "engine.v1.connections.save":
            return self._connections_save(frame.get("id"), params)
        return self._domain_error(frame.get("id"), "internal", "unhandled method")

    def _hello(self, request_id: Any, params: Mapping[str, Any], connection_id: int) -> dict[str, Any]:
        try:
            validate_schema_ref("contracts/engine.v1/methods/hello.params.schema.json", dict(params))
        except SchemaValidationError as exc:
            return self._error(request_id, -32602, str(exc))
        offered = ApiVersion.parse(params["offered_api"])
        required = params.get("required_capabilities") or []
        if not isinstance(required, list):
            required = []
        negotiation = evaluate_hello_negotiation(offered, SERVER_API, required, SERVER_FEATURES)
        if not negotiation.ok:
            code = (
                "version_mismatch"
                if negotiation.failure == NegotiationFailure.INCOMPATIBLE_MAJOR
                else negotiation.failure.value
            )
            return self._domain_error(request_id, code, "hello negotiation failed")
        auth = params.get("authentication")
        if auth is None:
            result = {
                "authenticated": False,
                "api_profile": {"major": SERVER_API.major, "minor": SERVER_API.minor},
                "engine_instance_id": self._identity.engine_instance_id,
                "instance_nonce": self._identity.instance_nonce,
                "capabilities": {"features": SERVER_FEATURES},
            }
        else:
            if not isinstance(auth, dict):
                return self._error(request_id, -32602, "invalid authentication")
            if not self._enrollment.verify(
                str(auth.get("engine_instance_id")),
                str(auth.get("instance_nonce")),
                str(auth.get("credential")),
            ):
                return self._domain_error(request_id, "capability_denied", "invalid enrollment credential")
            if not rendezvous_matches(
                self._identity.engine_instance_id,
                self._identity.instance_nonce,
                str(auth.get("engine_instance_id")),
                str(auth.get("instance_nonce")),
            ):
                return self._domain_error(request_id, "capability_denied", "rendezvous mismatch")
            with self._lock:
                self._authenticated_sessions.add(connection_id)
            result = {
                "authenticated": True,
                "api_profile": {"major": SERVER_API.major, "minor": SERVER_API.minor},
                "engine_instance_id": self._identity.engine_instance_id,
                "instance_nonce": self._identity.instance_nonce,
                "capabilities": {"features": SERVER_FEATURES},
            }
        try:
            validate_schema_ref("contracts/engine.v1/methods/hello.result.schema.json", result)
        except SchemaValidationError as exc:
            return self._error(request_id, -32603, str(exc))
        return self._success(request_id, result)

    def _operations_list(self, request_id: Any) -> dict[str, Any]:
        operations = [
            dict(entry) for entry in _OPERATION_CATALOG if entry["operation_id"] in self._implemented_methods
        ]
        result = {"operations": operations}
        validate_schema_ref("contracts/engine.v1/methods/operations.list.result.schema.json", result)
        return self._success(request_id, result)

    def _models_list(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            validate_schema_ref("contracts/engine.v1/methods/models.list.params.schema.json", dict(params))
        except SchemaValidationError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            result = self._list_models.execute(params)
        except UnsupportedCollectionError as exc:
            return self._domain_error(request_id, "unsupported_capability", str(exc))
        except CatalogUnavailableError as exc:
            return self._domain_error(request_id, "unsupported_capability", str(exc))
        except ValueError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            validate_schema_ref("contracts/engine.v1/methods/models.list.result.schema.json", result)
        except SchemaValidationError as exc:
            return self._error(request_id, -32603, str(exc))
        return self._success(request_id, result)

    def _models_register(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_model_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/models.register.params.schema.json",
            "contracts/engine.v1/methods/models.register.result.schema.json",
            self._register_model,
        )

    def _models_rename(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_model_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/models.rename.params.schema.json",
            "contracts/engine.v1/methods/models.rename.result.schema.json",
            self._rename_model,
        )

    def _models_remove(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_model_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/models.remove.params.schema.json",
            "contracts/engine.v1/methods/models.remove.result.schema.json",
            self._remove_model,
        )

    def _connections_list(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_connection_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/connections.list.params.schema.json",
            "contracts/engine.v1/methods/connections.list.result.schema.json",
            self._list_connections,
        )

    def _connections_save(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_connection_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/connections.save.params.schema.json",
            "contracts/engine.v1/methods/connections.save.result.schema.json",
            self._save_connection,
        )

    def _run_model_mutation(
        self,
        request_id: Any,
        params: Mapping[str, Any],
        params_schema: str,
        result_schema: str,
        use_case: RegisterModelUseCase | RenameModelUseCase | RemoveModelUseCase | None,
    ) -> dict[str, Any]:
        if use_case is None:
            return self._domain_error(request_id, "unsupported_capability", "method not configured")
        try:
            validate_schema_ref(params_schema, dict(params))
        except SchemaValidationError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            result = use_case.execute(params)
        except ModelRevisionConflictError as exc:
            return self._domain_error(request_id, "conflict", str(exc))
        except ModelIdempotencyConflictError as exc:
            return self._domain_error(request_id, "conflict", str(exc))
        except ModelRegistrationNotFoundError as exc:
            return self._domain_error(request_id, "not_found", str(exc))
        except ValueError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            validate_schema_ref(result_schema, result)
        except SchemaValidationError as exc:
            return self._error(request_id, -32603, str(exc))
        return self._success(request_id, result)

    def _run_connection_mutation(
        self,
        request_id: Any,
        params: Mapping[str, Any],
        params_schema: str,
        result_schema: str,
        use_case: ListConnectionsUseCase | SaveConnectionUseCase | None,
    ) -> dict[str, Any]:
        if use_case is None:
            return self._domain_error(request_id, "unsupported_capability", "method not configured")
        try:
            validate_schema_ref(params_schema, dict(params))
        except SchemaValidationError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            result = use_case.execute(params)
        except ConnectionRevisionConflictError as exc:
            return self._domain_error(request_id, "conflict", str(exc))
        except ConnectionIdempotencyConflictError as exc:
            return self._domain_error(request_id, "conflict", str(exc))
        except ValueError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            validate_schema_ref(result_schema, result)
        except SchemaValidationError as exc:
            return self._error(request_id, -32603, str(exc))
        return self._success(request_id, result)

    def _is_authenticated(self, connection_id: int) -> bool:
        with self._lock:
            return connection_id in self._authenticated_sessions

    def _success(self, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _domain_error(self, request_id: Any, code: str, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32000,
                "message": message,
                "data": {"code": code, "message": message, "retryable": False},
            },
        }
