"""Authenticated plugin-data broker adapter for the process runtime wire."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

from ..plugin_authority import ActivationIdentity
from .service import PluginDataBroker

__all__ = [
    "PluginDataWireActivationError",
    "PluginDataWireAdapter",
    "PluginDataWireConfigurationError",
    "PluginDataWireRequestError",
    "PluginDataWireResultError",
]


_METHOD_PREFIX = "plugin.v1.broker.storage."
_METHOD_NAMES = frozenset({"get", "list", "put", "delete"})


class PluginDataWireConfigurationError(ValueError):
    def __init__(self) -> None:
        super().__init__("plugin data wire configuration invalid")


class PluginDataWireActivationError(PermissionError):
    def __init__(self) -> None:
        super().__init__("plugin data wire activation denied")


class PluginDataWireRequestError(ValueError):
    def __init__(self) -> None:
        super().__init__("plugin data wire request invalid")


class PluginDataWireResultError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("plugin data wire result invalid")


def _schema_ref(method_name: str, direction: str) -> str:
    return (
        "contracts/plugin.v1/broker/storage."
        f"{method_name}.{direction}.schema.json"
    )


def _validate_wire_value(
    schema_ref: str,
    value: Any,
    error_type: type[PluginDataWireRequestError] | type[PluginDataWireResultError],
) -> None:
    invalid = False
    try:
        validate_schema_ref(schema_ref, value)
    except (SchemaValidationError, ValueError, TypeError, RecursionError):
        invalid = True
    if invalid:
        raise error_type()


class PluginDataWireAdapter:
    """Map authenticated runtime broker calls onto one trusted data broker.

    The activation identity is supplied by supervisor composition. Worker
    parameters contain only the frozen storage method fields and can never
    replace that identity.
    """

    def __init__(
        self,
        *,
        trusted_activation: ActivationIdentity,
        broker: PluginDataBroker,
    ) -> None:
        if type(trusted_activation) is not ActivationIdentity or not isinstance(
            broker, PluginDataBroker
        ):
            raise PluginDataWireConfigurationError()
        self._trusted_activation = trusted_activation
        self._broker = broker

    def __call__(
        self,
        authenticated_activation_id: str,
        method: str,
        params: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if (
            type(authenticated_activation_id) is not str
            or authenticated_activation_id != self._trusted_activation.activation_id
        ):
            raise PluginDataWireActivationError()
        if type(method) is not str or not method.startswith(_METHOD_PREFIX):
            raise PluginDataWireRequestError()
        method_name = method.removeprefix(_METHOD_PREFIX)
        if method_name not in _METHOD_NAMES or not isinstance(params, Mapping):
            raise PluginDataWireRequestError()

        _validate_wire_value(
            _schema_ref(method_name, "params"),
            params,
            PluginDataWireRequestError,
        )
        result = self._invoke(method_name, params)
        _validate_wire_value(
            _schema_ref(method_name, "result"),
            result,
            PluginDataWireResultError,
        )
        return result

    def _invoke(
        self, method_name: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        handle = params["invocation_handle"]
        namespace = params["namespace"]
        if method_name == "get":
            return self._broker.get(
                handle,
                self._trusted_activation,
                namespace=namespace,
                key=params["key"],
            )
        if method_name == "list":
            return self._broker.list(
                handle,
                self._trusted_activation,
                namespace=namespace,
                prefix=params["prefix"] if "prefix" in params else "",
                limit=params["limit"] if "limit" in params else 200,
            )
        if method_name == "put":
            return self._broker.put(
                handle,
                self._trusted_activation,
                namespace=namespace,
                key=params["key"],
                value=params["value"],
                expected_revision=(
                    params["expected_revision"]
                    if "expected_revision" in params
                    else None
                ),
            )
        if method_name == "delete":
            return self._broker.delete(
                handle,
                self._trusted_activation,
                namespace=namespace,
                key=params["key"],
                expected_revision=(
                    params["expected_revision"]
                    if "expected_revision" in params
                    else None
                ),
            )
        raise PluginDataWireRequestError()
