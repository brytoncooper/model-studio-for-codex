"""Provider-only public view of one authenticated process activation."""
from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import ProcessRuntime


class ProviderMethod(str, Enum):
    START = "plugin.v1.provider.start"
    SUBMIT_TOOL_RESULT = "plugin.v1.provider.submit_tool_result"
    CANCEL = "plugin.v1.provider.cancel"
    RESUME = "plugin.v1.provider.resume"
    ACK = "plugin.v1.provider.ack"


class ProviderChannel:
    """Obtained from runtime.provider_channel(); no transport ownership transfer."""

    def __init__(self, runtime: ProcessRuntime) -> None:
        self._runtime = runtime

    def __repr__(self) -> str:
        return "ProviderChannel()"

    @property
    def activation_id(self) -> str:
        return self._runtime._provider_activation_id()

    def request(self, method: ProviderMethod | str, params: Mapping[str, Any], *,
                timeout_s: float | None = None) -> dict[str, Any]:
        return self._runtime._provider_request(method, params, timeout_s)

    def receive_event(self, *, timeout_s: float) -> dict[str, Any] | None:
        """Return an event's params; None means polling elapsed without an event."""
        return self._runtime._receive_provider_event(timeout_s)
