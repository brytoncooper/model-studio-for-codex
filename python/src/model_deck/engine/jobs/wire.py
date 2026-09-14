"""Authenticated plugin-job broker adapter for the process runtime wire."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

from ..plugin_authority import ActivationIdentity
from .service import PluginJobBroker

__all__ = [
    "PluginJobWireActivationError",
    "PluginJobWireAdapter",
    "PluginJobWireConfigurationError",
    "PluginJobWireRequestError",
    "PluginJobWireResultError",
]


_METHOD_PREFIX = "plugin.v1.broker.jobs."
_METHOD_NAMES = frozenset({"create", "progress", "complete", "fail", "check_cancelled"})


class PluginJobWireConfigurationError(ValueError):
    def __init__(self) -> None:
        super().__init__("plugin job wire configuration invalid")


class PluginJobWireActivationError(PermissionError):
    def __init__(self) -> None:
        super().__init__("plugin job wire activation denied")


class PluginJobWireRequestError(ValueError):
    def __init__(self) -> None:
        super().__init__("plugin job wire request invalid")


class PluginJobWireResultError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("plugin job wire result invalid")


def _schema_ref(method_name: str, direction: str) -> str:
    return (
        "contracts/plugin.v1/broker/jobs."
        f"{method_name}.{direction}.schema.json"
    )


def _validate_wire_value(
    schema_ref: str,
    value: Any,
    error_type: type[PluginJobWireRequestError] | type[PluginJobWireResultError],
) -> None:
    invalid = False
    try:
        validate_schema_ref(schema_ref, value)
    except (SchemaValidationError, ValueError, TypeError, RecursionError):
        invalid = True
    if invalid:
        raise error_type()


class PluginJobWireAdapter:
    """Map authenticated runtime job broker calls onto one trusted job broker.

    The activation identity is supplied by supervisor composition. The runtime
    supplies only the activation id on each call; parameters can never
    replace the bound identity. Five methods are supported and routed without
    coercion.
    """

    def __init__(
        self,
        *,
        trusted_activation: ActivationIdentity,
        broker: PluginJobBroker,
    ) -> None:
        if type(trusted_activation) is not ActivationIdentity or not isinstance(
            broker, PluginJobBroker
        ):
            raise PluginJobWireConfigurationError()
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
            raise PluginJobWireActivationError()
        if type(method) is not str or not method.startswith(_METHOD_PREFIX):
            raise PluginJobWireRequestError()
        method_name = method.removeprefix(_METHOD_PREFIX)
        if method_name not in _METHOD_NAMES or not isinstance(params, Mapping):
            raise PluginJobWireRequestError()

        _validate_wire_value(
            _schema_ref(method_name, "params"),
            params,
            PluginJobWireRequestError,
        )
        result = self._invoke(method_name, params)
        _validate_wire_value(
            _schema_ref(method_name, "result"),
            result,
            PluginJobWireResultError,
        )
        return result

    def _invoke(
        self, method_name: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        if method_name == "create":
            handle = params["invocation_handle"]
            return self._broker.create(
                handle,
                self._trusted_activation,
                operation_id=params["operation_id"],
                checkpoint_schema_id=(
                    params["checkpoint_schema_id"]
                    if "checkpoint_schema_id" in params
                    else None
                ),
            )
        if method_name == "progress":
            return self._broker.report_progress(
                self._trusted_activation,
                job_id=params["job_id"],
                progress=params["progress"],
            )
        if method_name == "complete":
            # 'output' in params (vs absent) encodes output_present, so the
            # worker can supply an explicit JSON null distinct from "no output".
            output_present = "output" in params
            output = params["output"] if output_present else None
            return self._broker.complete(
                self._trusted_activation,
                job_id=params["job_id"],
                output=output,
                output_present=output_present,
            )
        if method_name == "fail":
            return self._broker.fail(
                self._trusted_activation,
                job_id=params["job_id"],
                error=params["error"],
            )
        if method_name == "check_cancelled":
            return self._broker.check_cancelled(
                self._trusted_activation,
                job_id=params["job_id"],
            )
        raise PluginJobWireRequestError()
