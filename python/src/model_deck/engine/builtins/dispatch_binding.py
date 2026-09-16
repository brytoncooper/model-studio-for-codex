"""Bind every built-in operation to the dispatch method that already serves it.

Routing used to stop at the descriptors: the composed kernel owned discovery
while ``EngineDispatch`` kept a literal ``if method == ...`` branch per built-in
operation, because the kernel's generic invocation path flattened every public
domain error (``conflict``, ``not_found``, ``unsupported_capability``, ...) into
one ``internal``. This module closes that gap. Each built-in operation is bound
to the unchanged private dispatch method that serves it, so one registered
handler per descriptor answers whether the call arrives through the kernel or,
when no kernel composed that operation, straight from dispatch.

Two things make the behaviour identical rather than merely similar:

* The bodies are untouched. A bound handler calls the same private method with
  the same arguments, so every error code, message, projection reconcile trigger
  and event publication is whatever that method already did.
* Errors travel whole. A dispatch method answers with a complete JSON-RPC
  envelope, so a bound handler raises ``KernelPassthroughError`` carrying that
  envelope and the kernel route returns it verbatim instead of rewording it.

The private methods are reached on purpose: this module is the seam that turns
them into registered handlers, and copying their bodies here is exactly what
would let the two drift apart.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from model_deck.kernel import FeatureDescriptor
from model_deck.kernel.registry import Handler

from model_deck.engine.builtins.capabilities import BuiltinWiringError
from model_deck.engine.builtins.descriptors import (
    BUILTIN_FEATURE_IDS,
    FEATURE_PROVIDER,
    builtin_operation_ids,
)
from model_deck.engine.builtins.dispatch_adapters import provider_feature_handlers
from model_deck.engine.builtins.handlers import BuiltinCollaborators, BuiltinHandlerRegistry
from model_deck.engine.kernel_composition import (
    DispatchInvocation,
    DispatchInvocationContext,
    KernelPassthroughError,
)

if TYPE_CHECKING:
    from model_deck.engine.dispatch import EngineDispatch

__all__ = [
    "BuiltinDispatchBinding",
    "BuiltinDispatchError",
    "builtin_handler_registry",
]

DispatchCall = Callable[[Mapping[str, Any], DispatchInvocationContext], dict[str, Any]]
"""One built-in operation, called with its params and its invocation context."""

_BUILTIN_OPERATION_IDS = frozenset(builtin_operation_ids())


class BuiltinDispatchError(RuntimeError):
    """A bound built-in operation was invoked with nothing to serve it.

    Either the binding never reached an ``EngineDispatch``, or it was invoked
    through the kernel's plain ``invoke`` path, which carries no connection.
    """


def _dispatch_calls(dispatch: "EngineDispatch") -> dict[str, DispatchCall]:
    """One entry per built-in operation, calling the method that serves it.

    Attribute access happens once, here, so a renamed dispatch method fails at
    startup rather than on the first request for that operation.
    """

    def with_params(method: Callable[..., dict[str, Any]]) -> DispatchCall:
        def call(params: Mapping[str, Any], context: DispatchInvocationContext) -> dict[str, Any]:
            return method(context.request_id, params)

        return call

    def with_connection(method: Callable[..., dict[str, Any]]) -> DispatchCall:
        def call(params: Mapping[str, Any], context: DispatchInvocationContext) -> dict[str, Any]:
            return method(context.request_id, params, context.connection_id)

        return call

    def with_method_name(
        method: Callable[..., dict[str, Any]], operation_id: str
    ) -> DispatchCall:
        def call(params: Mapping[str, Any], context: DispatchInvocationContext) -> dict[str, Any]:
            return method(context.request_id, operation_id, params, context.connection_id)

        return call

    def host_settings(operation: str) -> DispatchCall:
        def call(params: Mapping[str, Any], context: DispatchInvocationContext) -> dict[str, Any]:
            return dispatch._hosts_settings(
                context.request_id, params, context.connection_id, operation
            )

        return call

    calls: dict[str, DispatchCall] = {
        "engine.v1.hello": with_connection(dispatch._hello),
        "engine.v1.health": with_params(dispatch._health),
        "engine.v1.operations.list": with_params(dispatch._operations_list),
        "engine.v1.capabilities.get": with_params(dispatch._capabilities_get),
        "engine.v1.models.list": with_params(dispatch._models_list),
        "engine.v1.models.register": with_params(dispatch._models_register),
        "engine.v1.models.rename": with_params(dispatch._models_rename),
        "engine.v1.models.remove": with_params(dispatch._models_remove),
        "engine.v1.connections.list": with_params(dispatch._connections_list),
        "engine.v1.connections.save": with_params(dispatch._connections_save),
        "engine.v1.sessions.create": with_params(dispatch._sessions_create),
        "engine.v1.sessions.get": with_params(dispatch._sessions_get),
        "engine.v1.sessions.select_model": with_params(dispatch._sessions_select_model),
        "engine.v1.runs.start": with_connection(dispatch._runs_start),
        "engine.v1.runs.get": with_params(dispatch._runs_get),
        "engine.v1.runs.cancel": with_params(dispatch._runs_cancel),
        "engine.v1.runs.submit_tool_result": with_connection(dispatch._runs_submit_tool_result),
        "engine.v1.events.subscribe": with_connection(dispatch._events_subscribe),
        "engine.v1.events.ack": with_connection(dispatch._events_ack),
        "engine.v1.events.unsubscribe": with_connection(dispatch._events_unsubscribe),
        "engine.v1.hosts.settings.read": host_settings("read"),
        "engine.v1.hosts.settings.validate": host_settings("validate"),
        "engine.v1.hosts.settings.preview": host_settings("preview"),
        "engine.v1.hosts.settings.save": host_settings("save"),
        "engine.v1.hosts.projection_status": with_params(dispatch._hosts_projection_status),
        "engine.v1.hosts.list": with_params(dispatch._hosts_list),
        "engine.v1.hosts.prepare": with_params(dispatch._hosts_prepare),
        "engine.v1.jobs.get": with_method_name(dispatch._job_operation, "engine.v1.jobs.get"),
        "engine.v1.jobs.cancel": with_method_name(dispatch._job_operation, "engine.v1.jobs.cancel"),
        "engine.v1.jobs.resume": with_method_name(dispatch._job_operation, "engine.v1.jobs.resume"),
        "engine.v1.usage.query": with_params(dispatch._query_usage),
    }
    # The external extension methods all reach one dispatch method, which reads
    # the operation ID to decide what to ask the extension host for.
    for operation_id in (
        "engine.v1.extensions.install",
        "engine.v1.extensions.enable",
        "engine.v1.extensions.disable",
        "engine.v1.extensions.get",
        "engine.v1.extensions.list",
        "engine.v1.extensions.inspect",
        "engine.v1.extensions.update",
        "engine.v1.extensions.remove",
        "engine.v1.operations.invoke",
        "engine.v1.ui.contributions.list",
        "engine.v1.ui.panel.get",
    ):
        calls[operation_id] = with_method_name(dispatch._external_extension, operation_id)
    return calls


class BuiltinDispatchBinding:
    """The registered handler for every built-in operation, in one object.

    Bootstrap constructs the binding before the kernel is composed, because the
    kernel needs its handlers; ``EngineDispatch`` binds itself to it once its own
    fields are set. An engine with no composed kernel — the legacy escape hatch,
    or a dispatch built directly in a test — still owns a binding and still
    routes its built-ins through it.
    """

    def __init__(self) -> None:
        self._dispatch: "EngineDispatch | None" = None
        self._calls: dict[str, DispatchCall] = {}

    def bind(self, dispatch: "EngineDispatch") -> None:
        """Attach the dispatch whose methods serve the built-in operations."""
        if self._dispatch is not None:
            raise BuiltinWiringError("built-in dispatch binding is already bound")
        calls = _dispatch_calls(dispatch)
        expected = set(_BUILTIN_OPERATION_IDS)
        missing = sorted(expected - set(calls))
        if missing:
            raise BuiltinWiringError(f"built-in operations have no bound dispatch call: {missing}")
        unexpected = sorted(set(calls) - expected)
        if unexpected:
            raise BuiltinWiringError(f"bound dispatch calls name unknown operations: {unexpected}")
        self._dispatch = dispatch
        self._calls = calls

    def is_bound(self) -> bool:
        """Whether a dispatch has claimed this binding."""
        return self._dispatch is not None

    def bound_operations(self) -> frozenset[str]:
        """Operation IDs this binding serves; empty until it is bound."""
        return frozenset(self._calls)

    def dispatch_handler(self, operation_id: str) -> DispatchCall | None:
        """The handler for one operation, or None when it serves no such operation."""
        return self._calls.get(operation_id)

    def invoke(
        self,
        operation_id: str,
        params: Mapping[str, Any],
        context: DispatchInvocationContext,
    ) -> dict[str, Any]:
        """Run one built-in operation and return its whole JSON-RPC response."""
        call = self._calls.get(operation_id)
        if call is None:
            raise BuiltinDispatchError(f"no bound dispatch call for {operation_id}")
        return call(params, context)

    def kernel_handler(self, operation_id: str) -> Handler:
        """The kernel handler for one operation; errors pass through unchanged."""
        if operation_id not in _BUILTIN_OPERATION_IDS:
            raise BuiltinWiringError(f"not a built-in operation: {operation_id}")

        def handler(params: Any, grants: frozenset[str]) -> Any:
            if not isinstance(params, DispatchInvocation):
                raise BuiltinDispatchError(
                    f"{operation_id} needs the dispatch-bound invocation path"
                )
            envelope = self.invoke(operation_id, params.params, params.context)
            if not isinstance(envelope, dict):
                raise BuiltinDispatchError(f"{operation_id} did not answer with a response")
            if "error" in envelope:
                raise KernelPassthroughError(envelope)
            return envelope["result"]

        return handler

    def feature_handlers(
        self,
        descriptor: FeatureDescriptor,
        collaborators: BuiltinCollaborators,
    ) -> dict[str, Handler]:
        """Bind one built-in feature's operations; this is the handler adapter."""
        return {
            operation.operation_id: self.kernel_handler(operation.operation_id)
            for operation in descriptor.operations
        }


def builtin_handler_registry(
    binding: BuiltinDispatchBinding | None = None,
) -> BuiltinHandlerRegistry:
    """One adapter per built-in feature, in a registry the caller owns.

    Pass the binding the engine will hand to ``EngineDispatch`` so the composed
    kernel and dispatch reach the same handlers. Omitting it yields a registry
    whose built-in handlers have nothing to serve, which is what a caller that
    only wants the provider adapter gets.
    """
    if binding is None:
        binding = BuiltinDispatchBinding()
    adapters = {feature_id: binding.feature_handlers for feature_id in BUILTIN_FEATURE_IDS}
    adapters[FEATURE_PROVIDER] = provider_feature_handlers
    return BuiltinHandlerRegistry(adapters)
