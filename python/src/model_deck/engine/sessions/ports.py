from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from model_deck.engine.routing.ports import ContinuationScope


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    registration_id: str
    revision: int
    host_context_ref: str | None = None
    continuation_scope: ContinuationScope | None = None


@dataclass(frozen=True, slots=True)
class CreateSessionCommand:
    registration_id: str
    host_context_ref: str | None = None
    continuation_scope: ContinuationScope | None = None


@dataclass(frozen=True, slots=True)
class GetSessionCommand:
    session_id: str


@dataclass(frozen=True, slots=True)
class SelectModelCommand:
    session_id: str
    registration_id: str
    expected_revision: int
    continuation_reset: bool = False
    replacement_continuation_scope: ContinuationScope | None = None


@runtime_checkable
class SessionRepository(Protocol):
    def create(self, command: CreateSessionCommand) -> SessionRecord: ...

    def get(self, command: GetSessionCommand) -> SessionRecord: ...

    def select_model(self, command: SelectModelCommand) -> SessionRecord: ...


@runtime_checkable
class SessionContinuationResetPort(Protocol):
    """Two-phase retirement of provider-private state during session reset."""

    def prepare_session_continuation_reset(
        self, session_id: str, continuation_handle: str
    ) -> str | None: ...

    def commit_session_continuation_reset(self, reset_token: str) -> None: ...

    def rollback_session_continuation_reset(self, reset_token: str) -> None: ...


class SessionNotFoundError(LookupError):
    pass


class SessionRevisionConflictError(ValueError):
    pass


class SessionActiveRunConflictError(ValueError):
    pass


class SessionContinuationResetUnavailableError(ValueError):
    pass
