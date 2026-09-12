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


@dataclass(frozen=True, slots=True)
class GetSessionCommand:
    session_id: str


@dataclass(frozen=True, slots=True)
class SelectModelCommand:
    session_id: str
    registration_id: str
    expected_revision: int
    continuation_reset: bool = False


@runtime_checkable
class SessionRepository(Protocol):
    def create(self, command: CreateSessionCommand) -> SessionRecord: ...

    def get(self, command: GetSessionCommand) -> SessionRecord: ...

    def select_model(self, command: SelectModelCommand) -> SessionRecord: ...


class SessionNotFoundError(LookupError):
    pass


class SessionRevisionConflictError(ValueError):
    pass


class SessionActiveRunConflictError(ValueError):
    pass
