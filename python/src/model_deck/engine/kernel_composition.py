"""Validate a composed kernel at the authenticated local-operator boundary."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
import math
from typing import Any

from model_deck.kernel import ComposedKernel, CompositionError, GrantDeniedError, OperationDescriptor
from model_deck_contracts.validator import (
    SchemaValidationError, load_schema, normalize_schema_ref, validate_schema_ref,
)


class KernelInputError(ValueError):
    """The operation input does not satisfy its declared schema."""


class KernelInvocationError(RuntimeError):
    """The handler failed or returned an invalid result; details stay private."""


def _copy_json_result(value: Any, depth: int = 0, nodes: list[int] | None = None) -> Any:
    """Detach strict JSON types before schema validation; never coerce keys.

    Depth/node bounds match the contracts validator. The public json_value
    schema subsequently supplies its string, collection and property limits.
    """
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if depth > 64 or nodes[0] > 200_000:
        raise ValueError("JSON result exceeds bounds")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is str:
        value.encode("utf-8")
        return value
    if type(value) is list:
        return [_copy_json_result(item, depth + 1, nodes) for item in value]
    if type(value) is dict:
        copied = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON result keys must be strings")
            key.encode("utf-8")
            copied[key] = _copy_json_result(item, depth + 1, nodes)
        return copied
    raise ValueError("result must contain only finite JSON values")


def _check_schema_reference(reference: str) -> None:
    """Resolve bundled references through the same validator used at invocation."""
    try:
        normalized = normalize_schema_ref(reference)
        load_schema(normalized.partition("#")[0])
        try:
            validate_schema_ref(normalized, None)
        except SchemaValidationError:
            # A resolved schema need not accept this probe value. Unresolved
            # references raise separately and are rejected by the outer guard.
            pass
    except Exception:
        raise CompositionError("operation schema reference is unavailable or invalid") from None


class KernelComposition:
    """Trusted composition; its grant snapshot applies only after authentication.

    This is the shared enrolled-operator boundary, not per-plugin authorization.
    Request frames and caller-selected client names never contribute grants.
    """

    def __init__(self, kernel: ComposedKernel, *, operator_grants: Sequence[str] = ()) -> None:
        if not isinstance(kernel, ComposedKernel):
            raise CompositionError("kernel must be a ComposedKernel")
        if not isinstance(operator_grants, (list, tuple, set, frozenset)) or any(
            not isinstance(grant, str) for grant in operator_grants
        ):
            raise CompositionError("operator grants must be an explicit collection of strings")
        self._kernel = kernel
        self._operator_grants = frozenset(operator_grants)
        self._operations = {descriptor.operation_id: descriptor for descriptor in kernel.operations()}
        for descriptor in self._operations.values():
            _check_schema_reference(descriptor.input_schema_id)
            _check_schema_reference(descriptor.output_schema_id)
        try:
            validate_schema_ref("contracts/engine.v1/methods/operations.list.result.schema.json",
                                {"operations": self.catalog()})
        except Exception:
            raise CompositionError("kernel operation discovery does not satisfy the public contract") from None

    def operations(self) -> tuple[OperationDescriptor, ...]:
        return tuple(self._operations.values())

    def catalog(self) -> list[dict[str, Any]]:
        return [{**asdict(descriptor), "required_grants": list(descriptor.required_grants)}
                for descriptor in self._operations.values()]

    def invoke(self, operation_id: str, params: dict[str, Any]) -> Any:
        descriptor = self._operations[operation_id]
        try:
            validate_schema_ref(descriptor.input_schema_id, params)
        except SchemaValidationError:
            raise KernelInputError("invalid operation params") from None
        except Exception:
            raise KernelInvocationError("operation schema validation failed") from None
        try:
            result = self._kernel.invoke(operation_id, params, self._operator_grants)
        except GrantDeniedError:
            raise GrantDeniedError("operation grant denied") from None
        except Exception:
            raise KernelInvocationError("operation failed") from None
        try:
            result = _copy_json_result(result)
            validate_schema_ref("contracts/common/types.schema.json#/definitions/json_value", result)
            validate_schema_ref(descriptor.output_schema_id, result)
        except Exception:
            raise KernelInvocationError("operation returned an invalid result") from None
        return result
