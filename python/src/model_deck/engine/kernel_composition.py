"""Validate a composed kernel at the authenticated local-operator boundary."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
import math
from typing import Any

from model_deck.kernel import ComposedKernel, CompositionError, GrantDeniedError, OperationDescriptor
from model_deck_contracts.validator import (
    SchemaValidationError, load_schema, normalize_schema_ref, validate_schema_ref,
)

_DOMAIN_ERROR_CODES_REF = "contracts/common/types.schema.json"


def domain_error_codes() -> frozenset[str]:
    """The public domain error vocabulary, read from the frozen contract."""
    global _DOMAIN_ERROR_CODES
    if _DOMAIN_ERROR_CODES is None:
        schema = load_schema(_DOMAIN_ERROR_CODES_REF)
        _DOMAIN_ERROR_CODES = frozenset(schema["definitions"]["domain_error_code"]["enum"])
    return _DOMAIN_ERROR_CODES


_DOMAIN_ERROR_CODES: frozenset[str] | None = None


class KernelInputError(ValueError):
    """The operation input does not satisfy its declared schema."""


class KernelInvocationError(RuntimeError):
    """The handler failed or returned an invalid result; details stay private."""


class KernelDomainError(Exception):
    """A handler classifies its failure publicly; it never writes the public text.

    Only a code from the frozen ``domain_error_code`` vocabulary crosses this
    boundary. The sentence the caller reads is chosen by dispatch from a fixed
    table, so a handler can say what *kind* of failure this was without
    publishing a path, an identifier or a provider response alongside it — which
    is exactly what the single redacted ``internal`` error exists to prevent. A
    handler with nothing public to say raises anything else and is redacted to
    ``internal`` as before.
    """

    def __init__(self, code: str) -> None:
        if code not in domain_error_codes():
            raise ValueError(f"unknown domain error code: {code!r}")
        # The code is the entire payload, and ``str(exc)`` is that code, so no
        # handler-authored text is ever within reach of a caller reading this.
        super().__init__(code)
        self.code = code


class KernelPassthroughError(Exception):
    """A dispatch-bound handler already produced the complete error response.

    The built-in handlers answer with whole JSON-RPC envelopes because their
    bodies are the engine's own dispatch methods. Carrying that envelope here
    keeps the public error code, message and JSON-RPC code byte-identical to
    what the same method returned before routing moved to the kernel.
    """

    def __init__(self, envelope: dict[str, Any]) -> None:
        if not isinstance(envelope, dict) or "error" not in envelope:
            raise ValueError("passthrough envelope must be a JSON-RPC error response")
        super().__init__("dispatch-bound operation returned an error response")
        self.envelope = envelope


@dataclass(frozen=True, slots=True)
class DispatchInvocationContext:
    """What a dispatch-bound handler needs beyond its params.

    The connection is the transport connection the frame arrived on, which is
    what connection-scoped state (subscriptions, notification queues) is keyed
    by; ``principal_id`` is the authenticated principal for that connection, and
    ``request_id`` is the JSON-RPC id the response must carry.
    """

    connection_id: int
    principal_id: str | None = None
    request_id: Any = None


@dataclass(frozen=True, slots=True)
class DispatchInvocation:
    """The params a dispatch-bound handler receives, with its call context.

    ``ComposedKernel`` forwards params to a handler untouched, so this is how the
    context reaches a handler without widening the kernel's handler signature or
    hiding the context in ambient state.
    """

    params: Mapping[str, Any] = field(default_factory=dict)
    context: DispatchInvocationContext = field(
        default_factory=lambda: DispatchInvocationContext(connection_id=-1)
    )


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

    def __init__(
        self,
        kernel: ComposedKernel,
        *,
        operator_grants: Sequence[str] = (),
        dispatch_bound_features: Sequence[str] = (),
    ) -> None:
        if not isinstance(kernel, ComposedKernel):
            raise CompositionError("kernel must be a ComposedKernel")
        if not isinstance(operator_grants, (list, tuple, set, frozenset)) or any(
            not isinstance(grant, str) for grant in operator_grants
        ):
            raise CompositionError("operator grants must be an explicit collection of strings")
        if not isinstance(dispatch_bound_features, (list, tuple, set, frozenset)) or any(
            not isinstance(feature_id, str) for feature_id in dispatch_bound_features
        ):
            raise CompositionError("dispatch-bound features must be an explicit collection of strings")
        self._kernel = kernel
        self._operator_grants = frozenset(operator_grants)
        self._operations = {descriptor.operation_id: descriptor for descriptor in kernel.operations()}
        bound_features = frozenset(dispatch_bound_features)
        self._dispatch_bound_operations = frozenset(
            operation.operation_id
            for feature in kernel.features()
            if feature.feature_id in bound_features
            for operation in feature.operations
        )
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

    def operation_owners(self) -> dict[str, str]:
        """Each composed operation ID mapped to the feature that declared it.

        Callers use this to tell an engine built-in apart from a foreign feature
        composed alongside it, without learning anything about the handlers.
        """
        return {
            operation.operation_id: feature.feature_id
            for feature in self._kernel.features()
            for operation in feature.operations
        }

    def degraded_optional_capabilities(self) -> tuple[tuple[str, str], ...]:
        """Sorted (feature ID, capability) pairs whose optional capability is absent."""
        return self._kernel.unavailable_optional_capabilities()

    def catalog(self) -> list[dict[str, Any]]:
        return [{**asdict(descriptor), "required_grants": list(descriptor.required_grants)}
                for descriptor in self._operations.values()]

    def dispatch_bound_operations(self) -> frozenset[str]:
        """Operation IDs whose handler is one of the engine's own dispatch methods.

        These are the operations the caller declared ``dispatch_bound_features``
        for. They travel the ``invoke_dispatch_bound`` path, which carries the
        connection context and the handler's own public error codes.
        """
        return self._dispatch_bound_operations

    def invoke_dispatch_bound(
        self,
        operation_id: str,
        params: dict[str, Any],
        context: DispatchInvocationContext,
    ) -> Any:
        """Invoke one dispatch-bound operation, context and error codes intact.

        The composition's own input and output validation is deliberately skipped
        here: the bound handler is an engine dispatch method that already
        validates both against the same schema references, and validating twice
        would turn one contract failure into two different public errors.
        """
        if operation_id not in self._dispatch_bound_operations:
            raise KernelInvocationError("operation is not dispatch bound")
        if not isinstance(context, DispatchInvocationContext):
            raise KernelInvocationError("dispatch-bound invocation needs a DispatchInvocationContext")
        invocation = DispatchInvocation(params=params, context=context)
        try:
            return self._kernel.invoke(operation_id, invocation, self._operator_grants)
        except GrantDeniedError:
            raise GrantDeniedError("operation grant denied") from None
        except (KernelPassthroughError, KernelDomainError):
            raise
        except Exception:
            raise KernelInvocationError("operation failed") from None

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
        except KernelDomainError:
            # A feature that chose a public code says so deliberately; everything
            # else stays redacted below.
            raise
        except Exception:
            raise KernelInvocationError("operation failed") from None
        try:
            result = _copy_json_result(result)
            validate_schema_ref("contracts/common/types.schema.json#/definitions/json_value", result)
            validate_schema_ref(descriptor.output_schema_id, result)
        except Exception:
            raise KernelInvocationError("operation returned an invalid result") from None
        return result
