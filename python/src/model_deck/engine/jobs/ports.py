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
    resumable: bool = False
    """The operation declared itself resumable, so an INTERRUPTED run may be
    restarted from its last checkpoint. Default False: a job is not resumable
    unless its contributed operation said so."""
    resume_count: int = 0
    """How many times ``begin_resume`` has moved this job back to RUNNING."""


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
    resumable: bool = False
    resume_count: int = 0


@dataclass(frozen=True, slots=True)
class CreateJobCommand:
    owner: JobOwner
    invocation_id: str
    operation_id: str
    origin_principal_id: str
    checkpoint_schema_id: str | None = None
    resumable: bool = False
    """The contributing manifest declared this operation ``resumable: true``.

    Supplied by the supervisor from the manifest, never by the worker: a
    plugin cannot make its own job resumable by asking.
    """


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
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ResumeJobCommand:
    """Public request to restart one interrupted, resumable job.

    Same shape as ``RequestCancelPublicCommand``: the caller is the
    originating principal and the idempotency key is durable per principal,
    so an exact replay returns the stored outcome rather than resuming twice.
    """

    job_id: str
    caller_principal_id: str
    idempotency_key: str
    activation_id: str | None = None
    """The plugin's CURRENT serving activation, from the supervisor.

    The interrupted run's activation is gone, so the durable row is rebound to
    the activation that will actually do the work. None leaves the recorded
    activation untouched (the first-party path, which resumes nothing).
    """
    invocation_id: str | None = None
    """The invocation authority captured for the resume call.

    Worker follow-ups (progress / checkpoint / complete / fail) re-authorize
    against the row's ``invocation_id``, and the original one belongs to an
    activation that no longer exists, so resume rebinds it too.
    """


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


RESUME_OPERATION_SUFFIX = ".resume"
"""How a resumable operation names its resume entry point.

A manifest operation ``"<op>"`` that declares ``resumable: true`` must also
implement the sibling invocation ``"<op>.resume"``. It is not a second
contributed operation: it is never listed, never publicly invocable, and only
the supervisor ever calls it, with the params in
``ResumeInvocationRequest.invocation_params``.
"""


def resume_operation_id(operation_id: str) -> str:
    """The sibling invocation the plugin must implement for ``operation_id``."""
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("operation_id must be a non-empty string")
    return operation_id + RESUME_OPERATION_SUFFIX


@dataclass(frozen=True, slots=True)
class ResumeInvocationRequest:
    """Everything the supervisor needs to hand one job back to its plugin.

    ``checkpoint`` is the DECODED last checkpoint (or None when the job was
    interrupted before saving one); the use case decodes and revalidates the
    stored encoding, so nothing downstream sees a storage representation.
    """

    job_id: str
    plugin_id: str
    operation_id: str
    origin_principal_id: str
    checkpoint_revision: int | None = None
    checkpoint_schema_id: str | None = None
    checkpoint: Any = None

    @property
    def resume_operation_id(self) -> str:
        return resume_operation_id(self.operation_id)

    def invocation_params(self) -> dict[str, Any]:
        """The exact input the plugin's ``"<op>.resume"`` receives."""
        return {
            "job_id": self.job_id,
            "checkpoint_revision": self.checkpoint_revision,
            "checkpoint_schema_id": self.checkpoint_schema_id,
            "checkpoint": self.checkpoint,
        }


@dataclass(frozen=True, slots=True)
class ResumeTarget:
    """The live activation and invocation authority a resume will run under."""

    activation_id: str
    invocation_id: str


@runtime_checkable
class JobResumeInvoker(Protocol):
    """Supervisor-side transport for handing an interrupted job back.

    Two steps on purpose. ``prepare`` resolves the plugin's *current* serving
    activation and captures a fresh invocation authority for
    ``"<op>.resume"``; the durable row is rebound to that pair before anything
    is invoked, so the worker's first progress call already authorizes.
    ``invoke`` then runs the plugin. Neither step lives in the engine: the
    engine only names the seam.
    """

    def prepare(self, request: ResumeInvocationRequest) -> ResumeTarget | None:
        """Bind the current serving activation, or None when not serving."""
        ...

    def invoke(
        self, request: ResumeInvocationRequest, target: ResumeTarget
    ) -> None:
        """Invoke ``request.resume_operation_id`` on that activation."""
        ...


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
    def read_checkpoint(
        self, owner: JobOwner, job_id: str
    ) -> tuple[int, str | None, str | None] | None:
        """Return ``(revision, schema_id, checkpoint_json)`` or None.

        None means the job exists and is owned by ``owner`` but has never
        saved a checkpoint. ``checkpoint_json`` is the stored encoding, not a
        decoded value: the domain never sees storage encodings, so the caller
        decodes and revalidates it. A job that does not exist, or that belongs
        to another owner, raises rather than returning None.
        """
        ...

    def begin_resume(self, command: ResumeJobCommand) -> JobRecord:
        """Atomically move one INTERRUPTED resumable job back to RUNNING.

        In a single transaction: verify the caller is the originating
        principal, verify the job is INTERRUPTED and ``resumable``, set
        ``state`` to RUNNING, increment ``resume_count`` by one, and keep the
        owning ``plugin_id``/``activation_id`` and the stored checkpoint
        exactly as they are. Progress and checkpoint revision are preserved;
        nothing is replayed here.

        Raises ``JobResumeUnsupportedError`` when the job is not resumable or
        is not INTERRUPTED, and ``JobResumeConflictError`` when the
        idempotency key was already bound to a different request.
        """
        ...

    def rollback_resume(
        self, job_id: str, caller_principal_id: str, idempotency_key: str
    ) -> JobRecord:
        """Undo one ``begin_resume`` whose plugin invocation never landed.

        ``begin_resume`` commits before the worker is touched, because the
        worker's first call has to authorize against a RUNNING row. When the
        invocation then fails, that committed row describes a job nobody is
        working on, and no worker ever dies to make recovery notice it. This
        is how the caller's failed attempt is taken back.

        In one transaction: verify the caller is the originating principal,
        and if the job is still RUNNING from that resume, return it to
        INTERRUPTED, decrement ``resume_count`` back, and delete the
        ``(principal, idempotency_key)`` receipt so the failure is not
        replayed as a success. A job that is no longer RUNNING is left
        exactly as it is and returned unchanged: the worker evidently did
        receive the invocation, and its outcome outranks the transport error.
        """
        ...

    def read_resume_receipt(
        self, caller_principal_id: str, idempotency_key: str
    ) -> tuple[str, int | None] | None:
        """Return ``(job_id, resumed_from_revision)`` for a settled resume.

        The durable record of what one ``(principal, idempotency_key)`` pair
        already did, so an exact replay answers with the original outcome
        instead of resuming a second time. None when that pair has never
        resumed anything.
        """
        ...


class JobNotFoundError(LookupError):
    pass


class JobOwnershipMismatchError(ValueError):
    pass


class JobOriginMismatchError(PermissionError):
    """Public caller is not the originating principal."""


class JobIdempotencyConflictError(ValueError):
    """A public idempotency key was already bound to another request."""


class JobStateConflictError(ValueError):
    pass


class JobTerminalConflictError(ValueError):
    pass


class JobCheckpointConflictError(ValueError):
    pass


class JobCheckpointValidationError(ValueError):
    pass


class JobResumeUnsupportedError(ValueError):
    """This job cannot be resumed; maps to domain error ``resume_unavailable``.

    Raised when the job's operation is not resumable, or when the job is in
    any state other than INTERRUPTED. It is a permanent answer for that job in
    that state, not a retry hint.
    """


class JobResumeConflictError(ValueError):
    """A resume idempotency key was already bound elsewhere; maps to ``conflict``.

    Same rule as public cancellation: a ``(principal, idempotency_key)`` pair
    binds to exactly one job, and reusing it for another job is a conflict.
    """


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
