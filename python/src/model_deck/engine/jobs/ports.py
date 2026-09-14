"""Durable plugin job STATE repository contracts (B19 slice)."""
from __future__ import annotations

import math
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
    """Internal durable-job snapshot.

    ``output`` carries the decoded bounded JSON value. ``output_present`` is
    True iff a complete() call wrote an output value (including JSON null).
    Adapters are responsible for serializing/deserializing; the domain never
    sees the storage encoding.
    """

    job_id: str
    plugin_id: str
    activation_id: str
    invocation_id: str
    operation_id: str
    state: JobState
    progress: float
    created_at: str
    origin_principal_id: str
    cancel_requested: bool = False
    checkpoint_revision: int = 0
    checkpoint_schema_id: str | None = None
    checkpoint_json: str | None = None
    output: Any | None = None
    output_present: bool = False
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class JobPublicView:
    """Application-owned public view of a job.

    ``output_present`` mirrors the durable storage: True iff a complete()
    call wrote an output value (including explicit JSON null). When mapping
    to the public wire payload, ``output`` is included only when
    ``output_present`` is True; this preserves the distinction between
    "absent" and "explicit null" that the contract allows.
    """

    job_id: str
    state: JobState
    progress: float
    output_present: bool = False
    output: Any = None


@dataclass(frozen=True, slots=True)
class CreateJobCommand:
    owner: JobOwner
    invocation_id: str
    operation_id: str
    origin_principal_id: str
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
class RequestCancelPublicCommand:
    job_id: str
    caller_principal_id: str


@dataclass(frozen=True, slots=True)
class GetPublicCommand:
    job_id: str
    caller_principal_id: str


@dataclass(frozen=True, slots=True)
class ConfirmCancelCommand:
    job_id: str
    owner: JobOwner


@dataclass(frozen=True, slots=True)
class CompleteJobCommand:
    """Worker-driven completion.

    ``output_present`` mirrors public-schema semantics: True iff the worker
    passed an ``output`` value (including explicit JSON null). The adapter
    only persists output when this flag is True; an absent ``output`` is a
    permanent durable choice, distinct from an explicit null.
    """

    job_id: str
    owner: JobOwner
    output: Any = None
    output_present: bool = False


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
    def request_cancel_public(
        self, command: RequestCancelPublicCommand
    ) -> bool: ...
    def confirm_cancel(self, command: ConfirmCancelCommand) -> JobRecord: ...
    def complete(self, command: CompleteJobCommand) -> JobRecord: ...
    def fail(self, command: FailJobCommand) -> JobRecord: ...
    def get(self, command: GetJobCommand) -> JobRecord: ...
    def get_public(self, command: GetPublicCommand) -> JobPublicView: ...
    def list_active_for_activation(self, owner: JobOwner) -> list[JobRecord]: ...
    def mark_worker_crashed(self, owner: JobOwner) -> WorkerCrashResult: ...


class JobNotFoundError(LookupError):
    pass


class JobOwnershipMismatchError(ValueError):
    pass


class JobOriginMismatchError(PermissionError):
    """Public caller is not the originating principal."""


class JobStateConflictError(ValueError):
    pass


class JobTerminalConflictError(ValueError):
    pass


class JobCheckpointConflictError(ValueError):
    pass


class JobCheckpointValidationError(ValueError):
    pass


# --- Shared bounded json_value validation -----------------------------------
# These limits are the single source of truth for application-owned JSON
# validation; the broker and storage adapter MUST go through
# ``validate_bounded_json_value`` instead of duplicating policy.
JSON_VALUE_MAX_BYTES = 1_048_576
JSON_VALUE_MAX_DEPTH = 64
JSON_VALUE_MAX_NODES = 200_000
JSON_VALUE_MAX_ITEMS = 4096
JSON_VALUE_MAX_PROPERTIES = 1024
JSON_VALUE_MAX_STRING_BYTES = 1_048_576


class BoundedJsonValidationError(ValueError):
    """A JSON value failed application-owned bounded validation."""


def validate_bounded_json_value(value: Any) -> None:
    """Validate a value against the canonical bounded json_value policy.

    The helper is deliberately permissive about types but rejects: NaN /
    infinity floats, non-string dict keys, oversized strings, paths deeper
    than ``JSON_VALUE_MAX_DEPTH``, nodes beyond ``JSON_VALUE_MAX_NODES``,
    arrays larger than ``JSON_VALUE_MAX_ITEMS`` and objects with more than
    ``JSON_VALUE_MAX_PROPERTIES`` keys. Errors never echo the offending
    payload (a corrupted path can still carry worker data); only the
    generic message ``"bounded json_value violation"`` is raised.
    """

    seen: list[int] = []
    nodes: list[int] = [0]

    def visit(node: Any, depth: int) -> None:
        nodes[0] += 1
        if nodes[0] > JSON_VALUE_MAX_NODES or depth > JSON_VALUE_MAX_DEPTH:
            raise BoundedJsonValidationError("bounded json_value violation")
        if node is None or type(node) is bool:
            return
        if type(node) is int:
            return
        if type(node) is float:
            if math.isnan(node) or math.isinf(node):
                raise BoundedJsonValidationError("bounded json_value violation")
            return
        if type(node) is str:
            if len(node.encode("utf-8")) > JSON_VALUE_MAX_STRING_BYTES:
                raise BoundedJsonValidationError("bounded json_value violation")
            return
        if type(node) is list:
            if len(node) > JSON_VALUE_MAX_ITEMS:
                raise BoundedJsonValidationError("bounded json_value violation")
            if id(node) in seen:
                raise BoundedJsonValidationError("bounded json_value violation")
            seen.append(id(node))
            try:
                for item in node:
                    visit(item, depth + 1)
            finally:
                seen.pop()
            return
        if type(node) is dict:
            if len(node) > JSON_VALUE_MAX_PROPERTIES:
                raise BoundedJsonValidationError("bounded json_value violation")
            if id(node) in seen:
                raise BoundedJsonValidationError("bounded json_value violation")
            seen.append(id(node))
            try:
                for key, item in node.items():
                    if type(key) is not str:
                        raise BoundedJsonValidationError("bounded json_value violation")
                    visit(item, depth + 1)
            finally:
                seen.pop()
            return
        raise BoundedJsonValidationError("bounded json_value violation")

    try:
        visit(value, 0)
    except RecursionError:
        raise BoundedJsonValidationError("bounded json_value violation") from None
