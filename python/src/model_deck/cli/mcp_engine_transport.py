"""Composition-owned MCP transport bound to an isolated engine rendezvous.

The transport reads both the rendezvous file and the credential file from
the filesystem; the caller supplies their paths. It never accepts an
inline credential. Every call opens a fresh session and performs the
hello/auth handshake once so the same connection can issue multiple method
calls. Concrete client-to-adapter wiring stays at the executable composition
boundary rather than inside the MCP client package.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.integrations.clients.mcp.errors import McpEngineError


class UnixSocketMcpEngineTransport:
    """Wrap :class:`UnixSocketEngineClient` with hello authentication.

    The constructor takes the rendezvous socket path and the credential
    file path (not the credential itself). On every call the transport
    opens a session, runs ``engine.v1.hello`` with the supplied
    ``{engine_instance_id, instance_nonce, credential}``, and then
    issues the requested call.
    """

    def __init__(
        self,
        *,
        socket_path: Path,
        credential_path: Path,
        engine_instance_id: str,
        instance_nonce: str,
        client_name: str = "model-deck-mcp",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._socket_path = Path(socket_path)
        self._credential_path = Path(credential_path)
        self._engine_instance_id = engine_instance_id
        self._instance_nonce = instance_nonce
        self._client_name = client_name
        self._timeout_seconds = float(timeout_seconds)
        self._client = UnixSocketEngineClient(self._socket_path, timeout_seconds=self._timeout_seconds)
        self._last_authenticated: bool | None = None

    @classmethod
    def from_paths(
        cls,
        *,
        rendezvous_path: Path,
        credential_path: Path,
    ) -> "UnixSocketMcpEngineTransport":
        descriptor = load_rendezvous_file(Path(rendezvous_path))
        return cls(
            socket_path=descriptor.socket_path,
            credential_path=Path(credential_path),
            engine_instance_id=descriptor.engine_instance_id,
            instance_nonce=descriptor.instance_nonce,
        )

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def credential_path(self) -> Path:
        return self._credential_path

    @property
    def engine_instance_id(self) -> str:
        return self._engine_instance_id

    @property
    def instance_nonce(self) -> str:
        return self._instance_nonce

    @property
    def last_authenticated(self) -> bool | None:
        """Whether the most recent ``call_engine`` ran on an authenticated session."""

        return self._last_authenticated

    def _load_credential(self) -> str:
        text = self._credential_path.read_text(encoding="utf-8")
        credential = text.strip()
        if not credential:
            raise McpEngineError({"error": {"code": "capability_denied", "message": "empty operator credential"}})
        return credential

    def _authenticate(self, session: Any) -> bool:
        credential = self._load_credential()
        response = session.call(
            {
                "jsonrpc": "2.0",
                "id": "mcp-hello",
                "method": "engine.v1.hello",
                "params": {
                    "client_name": self._client_name,
                    "offered_api": {"major": 1, "minor": 0},
                    "authentication": {
                        "engine_instance_id": self._engine_instance_id,
                        "instance_nonce": self._instance_nonce,
                        "credential": credential,
                    },
                },
            }
        )
        return _extract_authenticated(response)

    def call_engine(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(method, str) or not method:
            raise ValueError("engine method must be a non-empty string")
        request_payload = dict(params or {})
        with self._client.session() as session:
            self._last_authenticated = self._authenticate(session)
            if not self._last_authenticated:
                raise McpEngineError(
                    {"error": {"code": "capability_denied", "message": "engine authentication failed"}}
                )
            request = {"jsonrpc": "2.0", "id": _next_request_id(method), "method": method, "params": request_payload}
            response = session.call(request)
        return _unwrap_response(method, response)


def _extract_authenticated(response: dict[str, Any]) -> bool:
    if not isinstance(response, dict):
        return False
    if "error" in response:
        raise McpEngineError(response)
    result = response.get("result")
    if isinstance(result, dict):
        if "error" in result:
            raise McpEngineError(result)
        return bool(result.get("authenticated"))
    return False


def _unwrap_response(method: str, response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise McpEngineError({"error": {"code": "internal", "message": f"engine {method}: non-dict reply"}})
    if "error" in response:
        raise McpEngineError(response)
    result = response.get("result")
    if isinstance(result, dict) and "error" in result:
        raise McpEngineError(result)
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise McpEngineError({"error": {"code": "internal", "message": f"engine {method}: non-dict result"}})
    return result


_REQUEST_COUNTER = {}


def _next_request_id(method: str) -> str:
    _REQUEST_COUNTER[method] = _REQUEST_COUNTER.get(method, 0) + 1
    return f"mcp-{method.replace('.', '-')}-{_REQUEST_COUNTER[method]}"
