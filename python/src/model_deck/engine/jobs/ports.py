"""Durable plugin job STATE repository contracts (B19 slice)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

CHECKPOINT_MAX_BYTES = 1_048_576


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


FAILURE_CODES: frozenset[str] = frozenset(
    {
        "invalid_argument",
        "unsupported_capability",
        "capability_denied",
        "not_found",
        "conflict",
        "version_mismatch",
        "provider_unavailable",
        "plugin_unavailable",
        "rate_limited",
        "deadline_exceeded",
        "interrupted",
        "resume_unavailable",
        "resource_exhausted",
        "projection_pending",
        "internal",
    }
)
"""Safe failure codes mirrored from contracts/common/types.schema.json#/definitions/domain_error_code."""


TERMINAL_JOB_STATES: frozenset[JobState] = frozenset(
    {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.INTERRUPTED}
)

ACTIVE_JOB_STATES: frozenset[JobState] = frozenset({JobState.QUEUED, JobState.RUNNING})


@dataclass(frozen=True, slots=True)
class JobOwner:
    plugin_id: str
    activation_id: str


@dataclass(frozen=True, slots=True)
class JobIdentity:
    job_id: str
    plugin_id: str
    activation_id: str
    invocation_id: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    plugin_id: str
    activation_id: str
    invocation_id: str
    operation_id: str
    state: JobState
    progress: float
    created_at: str
    cancel_requested: bool = False
    checkpoint_revision: int = 0
    checkpoint_schema_id: str | None = None
    checkpoint_json: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class CreateJobCommand:
    owner: JobOwner
    invocation_id: str
    operation_id: str
    checkpoint_schema_id: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimJobCommand:
    job_id: str
    owner: JobOwner


@dataclass(frozen=True, slots=True)
class ReportProgressCommand:
    job_id: str
    owner: JobOwner
    progress: float


@dataclass(frozen=True, slots=True)
class SaveCheckpointCommand:
    job_id: str
    owner: JobOwner
    checkpoint: Any
    expected_revision: int
    schema_id: str | None = None


@dataclass(frozen=True, slots=True)
class RequestCancelCommand:
    job_id: str
    owner: JobOwner


@dataclass(frozen=True, slots=True)
class ConfirmCancelCommand:
    job_id: str
    owner: JobOwner


@dataclass(frozen=True, slots=True)
class CompleteJobCommand:
    job_id: str
    owner: JobOwner


@dataclass(frozen=True, slots=True)
class FailJobCommand:
    job_id: str
    owner: JobOwner
    failure_code: str


@dataclass(frozen=True, slots=True)
class GetJobCommand:
    job_id: str


@dataclass(frozen=True, slots=True)
class WorkerCrashResult:
    interrupted_job_ids: tuple[str, ...]


@runtime_checkable
class PluginJobRepository(Protocol):
    def create(self, command: CreateJobCommand) -> JobRecord: ...
    def claim(self, command: ClaimJobCommand) -> JobRecord: ...
    def report_progress(self, command: ReportProgressCommand) -> JobRecord: ...
    def save_checkpoint(self, command: SaveCheckpointCommand) -> JobRecord: ...
    def request_cancel(self, command: RequestCancelCommand) -> JobRecord: ...
    def confirm_cancel(self, command: ConfirmCancelCommand) -> JobRecord: ...
    def complete(self, command: CompleteJobCommand) -> JobRecord: ...
    def fail(self, command: FailJobCommand) -> JobRecord: ...
    def get(self, command: GetJobCommand) -> JobRecord: ...
    def list_active_for_activation(self, owner: JobOwner) -> list[JobRecord]: ...
    def mark_worker_crashed(self, owner: JobOwner) -> WorkerCrashResult: ...


class JobNotFoundError(LookupError):
    pass


class JobOwnershipMismatchError(ValueError):
    pass


class JobStateConflictError(ValueError):
    pass


class JobTerminalConflictError(ValueError):
    pass


class JobCheckpointConflictError(ValueError):
    pass


class JobCheckpointValidationError(ValueError):
    pass
