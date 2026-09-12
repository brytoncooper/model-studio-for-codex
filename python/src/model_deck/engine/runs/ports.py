from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from model_deck.engine.routing.ports import RouteSnapshot

SUBSCRIBER_QUEUE_MAX_EVENTS = 256
SUBSCRIBER_QUEUE_MAX_BYTES = 1_048_576
RUN_LIVE_REPLAY_MAX_BYTES = 8_388_608
RUN_LIVE_REPLAY_MAX_DURATION_SECONDS = 60
ADMISSION_RECORD_RETENTION_MIN_HOURS = 24


class RunState(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING_FOR_TOOL = "waiting_for_tool"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ActiveRunState(str, Enum):
    """Non-terminal run states allowed for repository append transitions."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING_FOR_TOOL = "waiting_for_tool"
    CANCELLING = "cancelling"


TERMINAL_RUN_STATES: frozenset[RunState] = frozenset(
    {
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.INTERRUPTED,
    }
)


class TerminalOutcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class TerminalResult:
    outcome: TerminalOutcome
    error: Any = None


@dataclass(frozen=True, slots=True)
class NormalizedRunInput:
    messages: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Host-authorized function advertisement, distinct from an emitted call."""
    name: str
    input_schema: dict[str, Any]
    host_execution_required: bool
    description: str | None = None


class StoredToolDefinitionsCompatibilityError(ValueError):
    """Stored tool advertisements cannot be interpreted by this engine version."""


@dataclass(frozen=True, slots=True)
class ToolCallDescriptor:
    call_id: str
    tool_name: str
    arguments: Any = None


@dataclass(frozen=True, slots=True)
class RunRequest:
    run_id: str
    session_id: str
    client_request_id: str
    idempotency_key: str
    route_snapshot: RouteSnapshot
    input: NormalizedRunInput
    tools: tuple[ToolDefinition, ...] = ()


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    session_id: str
    state: RunState
    client_request_id: str
    registration_id: str
    route_snapshot: RouteSnapshot
    principal_id: str
    authorized_host_context_ref: str | None = None
    terminal_result: TerminalResult | None = None
    last_sequence: int = 0


@dataclass(frozen=True, slots=True)
class ApplicationRunEvent:
    kind: str
    run_id: str
    session_id: str
    sequence: int
    event_schema_version: int
    observed_at: str
    payload: Any = None


@dataclass(frozen=True, slots=True)
class ProviderRunEvent:
    kind: str
    run_id: str
    observed_at: str
    payload: Any = None


@dataclass(frozen=True, slots=True)
class RunAdmissionKey:
    principal_id: str
    operation_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class StartRunCommand:
    admission_key: RunAdmissionKey
    request_hash: str
    session_id: str
    client_request_id: str
    registration_id: str
    route_snapshot: RouteSnapshot
    input: NormalizedRunInput
    tools: tuple[ToolDefinition, ...] = ()
    authorized_host_context_ref: str | None = None


@dataclass(frozen=True, slots=True)
class RunAdmissionResult:
    run: RunRecord
    dispatch_required: bool


@dataclass(frozen=True, slots=True)
class ClaimDispatchCommand:
    """One-time dispatch claim for an accepted run."""

    run_id: str
    dispatch_token: str


@dataclass(frozen=True, slots=True)
class AppendApplicationEventCommand:
    """Append a non-terminal transition with strictly monotonic sequence assignment."""

    run_id: str
    expected_state: ActiveRunState
    new_state: ActiveRunState
    kind: str
    payload: Any = None
    observed_at: str | None = None


@dataclass(frozen=True, slots=True)
class AppendApplicationEventResult:
    run: RunRecord
    event: ApplicationRunEvent


@dataclass(frozen=True, slots=True)
class CancelRunCommand:
    run_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CancelRunResult:
    run: RunRecord
    event: ApplicationRunEvent | None


@dataclass(frozen=True, slots=True)
class SubmitToolResultCommand:
    run_id: str
    call_id: str
    idempotency_key: str
    result: Any
    principal_id: str
    host_context_ref: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitToolResultResult:
    run: RunRecord
    provider_submission_required: bool


@dataclass(frozen=True, slots=True)
class CompleteTerminalCommand:
    """Terminal completion assigns final event and terminal result exactly once."""

    run_id: str
    expected_state: ActiveRunState
    terminal_result: TerminalResult
    final_event_kind: str
    final_event_payload: Any = None
    observed_at: str | None = None


@dataclass(frozen=True, slots=True)
class CompleteTerminalResult:
    run: RunRecord
    event: ApplicationRunEvent


@dataclass(frozen=True, slots=True)
class GetRunCommand:
    run_id: str


@dataclass(frozen=True, slots=True)
class RestartRecoveryResult:
    observed_at: str
    dispatchable_requests: tuple[RunRequest, ...]
    interrupted_run_ids: tuple[str, ...]


@runtime_checkable
class RunRepository(Protocol):
    def lookup_admission(
        self, admission_key: RunAdmissionKey, request_hash: str
    ) -> RunAdmissionResult | None:
        """Return a prior admission for an exact request_hash, or None if absent.

        Raises RunAdmissionRequestHashConflictError when the admission_key already
        exists with a different request_hash.

        Implementations retain admission records for at least
        ADMISSION_RECORD_RETENTION_MIN_HOURS.
        """
        ...

    def admit(self, command: StartRunCommand) -> RunAdmissionResult:
        """Atomically admit or replay by admission_key identity.

        Identity is principal_id + operation_id + idempotency_key only.
        request_hash is compared separately: an exact replay returns the prior
        admitted run (and dispatch_required=False) even when the active
        registration was later removed; the same identity with a different
        request_hash raises RunAdmissionRequestHashConflictError. Concurrent
        races return replay with dispatch_required=False.

        Admission records are retained for at least ADMISSION_RECORD_RETENTION_MIN_HOURS.
        """
        ...

    def claim_dispatch(self, command: ClaimDispatchCommand) -> RunRecord:
        """Atomically claim dispatch for an accepted run exactly once.

        Records the dispatch claim and transitions the run from accepted to running in
        the same atomic step. After a successful claim, the run is never accepted again.
        Recovery therefore treats every claimed non-terminal run as in-flight work to
        interrupt; no claimed run may remain accepted.
        """
        ...

    def append_application_event(
        self, command: AppendApplicationEventCommand
    ) -> AppendApplicationEventResult:
        """Append a non-terminal event; sequence increases strictly monotonically."""
        ...

    def request_cancel(self, command: CancelRunCommand) -> CancelRunResult:
        """Record cancellation intent and return its exact committed event once."""
        ...

    def submit_tool_result(
        self, command: SubmitToolResultCommand
    ) -> SubmitToolResultResult:
        """Submit a tool result when caller authorization matches the run capture.

        Atomically validates principal_id and host_context_ref against the run
        capture, requires the call_id to be outstanding, and applies idempotency
        by (run_id, call_id, idempotency_key). The first accepted submission
        returns provider_submission_required=True. An exact replay with the same
        result returns provider_submission_required=False and the current run
        record. Differing payloads for the same idempotency key raise
        ToolResultIdempotencyConflictError.
        """
        ...

    def complete_terminal(self, command: CompleteTerminalCommand) -> CompleteTerminalResult:
        """Terminalize once and return the exact committed terminal event."""
        ...

    def get(self, command: GetRunCommand) -> RunRecord: ...

    def recover_after_restart(self, observed_at: str) -> RestartRecoveryResult:
        """Atomically recover incomplete runs after process restart.

        Unclaimed accepted runs (never successfully claimed) are returned as
        dispatchable_requests: each RunRequest is reconstructed from durable
        admitted input, tools, idempotency metadata, and the captured
        RouteSnapshot. Recovery does not re-resolve active registrations.
        The application coordinator claims dispatch once per request before
        provider start.

        Every claimed non-terminal run (running, waiting_for_tool, or cancelling) is
        terminalized as interrupted with an assigned final event in one atomic step.
        Because claim_dispatch moves accepted to running while recording the claim,
        no claimed run remains accepted at recovery time. Claimed runs are never
        redispatched and provider work is not restarted by recovery.
        """
        ...


class RunNotFoundError(LookupError):
    pass


class RunStateConflictError(ValueError):
    pass


class RunDispatchClaimError(ValueError):
    pass


class RunTerminalConflictError(ValueError):
    pass


class RunAdmissionRequestHashConflictError(ValueError):
    pass


class ToolResultIdempotencyConflictError(ValueError):
    pass


class RunAuthorizationMismatchError(ValueError):
    pass


class ToolCallNotOutstandingError(ValueError):
    pass


@runtime_checkable
class ApplicationEventPublisher(Protocol):
    def publish_application_event(self, event: ApplicationRunEvent) -> None: ...


@runtime_checkable
class ProviderRunEventSink(Protocol):
    def publish_provider_event(self, event: ProviderRunEvent) -> None: ...


class ProviderCancelTerminationStatus(str, Enum):
    UNKNOWN = "unknown"
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"


class SubmitToolResultProviderOutcome(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class CancelProviderRunResult:
    request_accepted: bool
    termination_status: ProviderCancelTerminationStatus


@dataclass(frozen=True, slots=True)
class SubmitToolResultProviderResult:
    outcome: SubmitToolResultProviderOutcome


@runtime_checkable
class ProviderRunHandle(Protocol):
    def submit_tool_result(
        self, call_id: str, result: Any
    ) -> SubmitToolResultProviderResult: ...

    def request_cancel(self, *, deadline: str) -> CancelProviderRunResult: ...


@runtime_checkable
class ProviderExecutionPort(Protocol):
    def start(self, request: RunRequest, sink: ProviderRunEventSink) -> ProviderRunHandle: ...


class EventReplayOutcome(str, Enum):
    DELIVERED = "delivered"
    SLOW_READER = "slow_reader"
    RESUME_UNAVAILABLE = "resume_unavailable"


@dataclass(frozen=True, slots=True)
class ReplaySubscriptionHandle:
    subscription_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class EventReplayPage:
    events: tuple[ApplicationRunEvent, ...]
    next_sequence: int | None
    outcome: EventReplayOutcome
    bytes_delivered: int
    credit_remaining: int


@dataclass(frozen=True, slots=True)
class EventReplayAckResult:
    outcome: EventReplayOutcome
    credit_remaining: int


@runtime_checkable
class RunEventReplayPort(Protocol):
    """Live run event delivery with bounded subscriber queues and live-only replay.

    Subscriber backpressure uses SUBSCRIBER_QUEUE_MAX_EVENTS and
    SUBSCRIBER_QUEUE_MAX_BYTES independently per subscription. Slow readers receive
    EventReplayOutcome.SLOW_READER without affecting other subscribers.

    Live replay resumes from after_sequence only within the in-memory live buffer
    bounded by RUN_LIVE_REPLAY_MAX_BYTES and RUN_LIVE_REPLAY_MAX_DURATION_SECONDS.
    When the cursor is evicted or expired beyond those limits, subscribe/ack return
    EventReplayOutcome.RESUME_UNAVAILABLE. Stream content is not regenerated or
    persisted solely to satisfy replay. Reconnect and resume never redispatch provider
    execution work.
    """

    def subscribe(
        self,
        run_id: str,
        *,
        after_sequence: int | None,
        grant_credit: int,
    ) -> tuple[ReplaySubscriptionHandle, EventReplayPage]:
        """Open or resume a subscription; may return RESUME_UNAVAILABLE for stale cursors."""
        ...

    def ack(
        self,
        handle: ReplaySubscriptionHandle,
        through_sequence: int,
        return_credit: int,
    ) -> EventReplayAckResult:
        """Cumulatively acknowledge delivery and add return_credit once when the cursor advances."""
        ...

    def read_available(self, handle: ReplaySubscriptionHandle) -> EventReplayPage:
        """Return currently queued events without blocking or granting credit."""
        ...

    def unsubscribe(self, handle: ReplaySubscriptionHandle) -> None:
        """Release a subscription and its per-subscriber queue state."""
        ...
