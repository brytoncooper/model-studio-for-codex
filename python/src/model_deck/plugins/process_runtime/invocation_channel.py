"""Public generic invocation view of one authenticated process activation."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from .runtime import ProcessRuntime


BrokerRequestHandler: TypeAlias = Callable[
    [str, str, Mapping[str, Any]], Mapping[str, Any]
]


class InvocationChannel:
    """Obtained from an active runtime; it does not mint invocation authority."""

    def __init__(self, runtime: ProcessRuntime) -> None:
        self._runtime = runtime

    def __repr__(self) -> str:
        return "InvocationChannel()"

    @property
    def activation_id(self) -> str:
        """Return the runtime-bound activation identity."""
        return self._runtime._invocation_activation_id()

    def invoke(
        self,
        operation_id: str,
        input: Any,
        broker_context: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Invoke one operation with authority context supplied by its caller."""
        return self._runtime._invocation_request(
            operation_id,
            input,
            broker_context,
            timeout_s,
        )
