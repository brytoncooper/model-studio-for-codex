from __future__ import annotations


class McpReadError(Exception):
    """Agent-visible MCP read failure; never includes secrets."""


class McpWriteError(Exception):
    """Agent-visible MCP write failure (registration, rename, remove).

    The message is delivered to the calling agent. It never carries
    credentials, raw engine protocol frames, or host paths.
    """


class McpEngineError(Exception):
    """A normalized engine JSON-RPC error payload.

    Holds the parsed ``{"error": {"code", "message"}}`` envelope (and any
    structured ``data``) so the MCP write service can map it to a
    public ``McpWriteError`` or, when called from the root ``Deck``
    shim, a ``DeckError``.
    """

    def __init__(self, envelope: object) -> None:
        super().__init__(str(envelope))
        self.envelope = envelope

    @property
    def code(self) -> object:
        envelope = self.envelope if isinstance(self.envelope, dict) else {}
        error = envelope.get("error") if isinstance(envelope, dict) else None
        if isinstance(error, dict):
            data = error.get("data")
            if isinstance(data, dict) and isinstance(data.get("code"), str):
                return data["code"]
            return error.get("code")
        return None

    @property
    def message(self) -> str:
        envelope = self.envelope if isinstance(self.envelope, dict) else {}
        error = envelope.get("error") if isinstance(envelope, dict) else None
        if isinstance(error, dict):
            data = error.get("data")
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                return data["message"]
            if isinstance(error.get("message"), str):
                return error["message"]
        return str(self.envelope)
