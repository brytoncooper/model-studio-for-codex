from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LegacyMcpApplicationAdapter(Protocol):
    """Unconverted MCP application behavior (writes and reads without engine use cases)."""

    def list_endpoints(self) -> dict[str, Any]: ...

    def search_models(self, query: str, endpoint: str | None = None, limit: int = 20) -> dict[str, Any]: ...

    def list_added_models(self) -> dict[str, Any]: ...

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class McpRegisteredModelPresentation(Protocol):
    """Host-owned labels and billing metadata for registered model rows."""

    def endpoint_name(self, connection_id: str) -> str: ...

    def billing_for(self, connection_id: str) -> tuple[str, str | None]: ...

    def price_for(self, provider_model_id: str, connection_id: str) -> str: ...

    def host_role(self, provider_model_id: str) -> str | None: ...


@runtime_checkable
class McpCatalogEndpointResolver(Protocol):
    """Maps a saved endpoint name to a connection_id served from catalog cache."""

    def catalog_connection_for_endpoint(self, endpoint: str | None) -> str | None: ...

    def default_catalog_endpoint_name(self) -> str | None: ...


@runtime_checkable
class McpCatalogSearchProvenance(Protocol):
    """Host-owned source/verified/note metadata for cache-backed catalog search envelopes."""

    def catalog_search_metadata(self, connection_id: str) -> tuple[str, bool, str]: ...


@runtime_checkable
class McpEngineTransport(Protocol):
    """One-shot authenticated call into the engine JSON-RPC server.

    A single ``call_engine`` does the hello/auth handshake once per
    open session and reuses it for subsequent calls. Engine error
    envelopes are normalized into :class:`McpEngineError` so the write
    service can translate them.
    """

    def call_engine(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class McpConnectionResolver(Protocol):
    """Deterministic resolver from a saved endpoint identifier to a UUID.

    Implementations fetch ``engine.v1.connections.list`` on demand, cache
    the result for the lifetime of the resolver, and resolve the sole
    connection when ``endpoint`` is ``None``. Multiple connections without
    an explicit endpoint raise :class:`McpWriteError`.
    """

    def resolve(self, endpoint: str | None) -> str: ...


@runtime_checkable
class McpRegisteredModelLocator(Protocol):
    """Authoritative source for an existing registration's id and revision.

    Implementations fetch ``engine.v1.models.list {"collection":
    "registered"}`` and return ``(registration_id, revision)`` for the
    given provider_model_id, or ``None`` when it is not registered.
    """

    def find(self, provider_model_id: str) -> tuple[str, int] | None: ...
