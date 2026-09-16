"""First-party (engine-owned) jobs on the same durable job state as plugin jobs.

There is no public job-create operation. A first-party job exists only because
an engine operation started one, and it reuses ``JobOwner`` with a reserved
``plugin_id`` under ``FIRST_PARTY_PLUGIN_PREFIX`` plus the engine's boot
identity as ``activation_id``. Two rules keep that reservation honest:

* ``guard_external_plugin_id`` refuses any external activation whose plugin_id
  claims the reserved prefix, so a plugin can never have the engine create,
  observe or cancel jobs on its behalf (confused deputy).
* First-party rows record the engine itself as ``origin_principal_id``. The
  engine's principal is not in the client principal namespace, so no client can
  ever be mistaken for the originator of a first-party job.

Because the engine is the originator, any *authenticated* engine client may
observe and cancel these jobs through ``FirstPartyJobDirectory``. One
consequence is deliberate and documented: a jobs.cancel ``idempotency_key`` for
a first-party job is scoped to the engine, not to the calling client, so two
clients that pick the same key for different jobs collide. Keys are caller-
chosen opaque strings (UUIDs in practice), and narrowing this would mean
widening the repository port, which is not worth it here.
"""
from __future__ import annotations

import threading
import uuid as uuid_module
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from model_deck.engine.jobs.ports import (
    TERMINAL_JOB_STATES,
    ClaimJobCommand,
    CreateJobCommand,
    FailJobCommand,
    GetJobCommand,
    JobNotFoundError,
    JobOwner,
    JobRecord,
    JobState,
    JobStateConflictError,
    JobTerminalConflictError,
    PluginJobRepository,
    WorkerCrashResult,
)
from model_deck.engine.jobs.use_cases import (
    CancelJobUseCase,
    GetJobUseCase,
    ResumeJobUseCase,
)

__all__ = [
    "CreateFirstPartyJobUseCase",
    "FIRST_PARTY_JOB_KINDS",
    "FIRST_PARTY_ORIGIN_PRINCIPAL",
    "FIRST_PARTY_PLUGIN_ID",
    "FIRST_PARTY_PLUGIN_PREFIX",
    "FirstPartyJobDirectory",
    "FirstPartyJobKindError",
    "FirstPartyJobOwnerError",
    "FirstPartyJobStart",
    "JOB_KIND_BENCHMARKS_REFRESH",
    "JOB_KIND_PRICES_REFRESH",
    "first_party_owner",
    "guard_external_plugin_id",
    "is_first_party_plugin_id",
    "recover_first_party_jobs",
]

FIRST_PARTY_PLUGIN_PREFIX = "com.modeldeck.engine."
"""Reserved plugin_id namespace. Only the engine may own a job under it."""

FIRST_PARTY_PLUGIN_ID = FIRST_PARTY_PLUGIN_PREFIX + "jobs"

FIRST_PARTY_ORIGIN_PRINCIPAL = "model-deck:engine"
"""The engine's own principal. Disjoint from the client principal namespace."""

JOB_KIND_PRICES_REFRESH = FIRST_PARTY_PLUGIN_PREFIX + "prices.refresh"
JOB_KIND_BENCHMARKS_REFRESH = FIRST_PARTY_PLUGIN_PREFIX + "benchmarks.refresh"

FIRST_PARTY_JOB_KINDS: frozenset[str] = frozenset(
    {JOB_KIND_PRICES_REFRESH, JOB_KIND_BENCHMARKS_REFRESH}
)
"""The engine refresh job kinds frozen in the contracts vocabulary."""

_TERMINAL_STATE_VALUES: frozenset[str] = frozenset(
    state.value for state in TERMINAL_JOB_STATES
)

_MAX_IDEMPOTENCY_KEY = 128

_ABANDONED_FAILURE_CODE = "internal"
"""Recorded for a job whose work could never be started. The reason is engine
side and carries no payload, so the public view says only that it failed."""

_MAX_TRACKED_KEYS = 64
"""Hard bound on remembered idempotency keys.

Only a key whose job is still active is ever answered from the map, and the
engine runs a handful of refresh jobs at a time, so this is far above any
honest working set. It exists because the keys are caller-chosen: the MCP
reader sends a fresh UUID per call, and without a bound every refresh request
would add an entry that is never read again.
"""


class FirstPartyJobOwnerError(PermissionError):
    """A non-engine caller claimed the reserved first-party plugin namespace."""


class FirstPartyJobKindError(ValueError):
    """A job kind outside the frozen first-party vocabulary was requested."""


def is_first_party_plugin_id(plugin_id: object) -> bool:
    """True when this plugin_id sits in the engine's reserved namespace."""
    return isinstance(plugin_id, str) and plugin_id.startswith(FIRST_PARTY_PLUGIN_PREFIX)


def guard_external_plugin_id(plugin_id: object) -> None:
    """Refuse an external plugin that claims the reserved engine namespace.

    Called on every externally authenticated job path, so a plugin whose
    manifest declares a reserved identifier cannot borrow the engine's
    authority over first-party jobs.
    """
    if is_first_party_plugin_id(plugin_id):
        raise FirstPartyJobOwnerError(
            "the plugin namespace reserved for engine jobs cannot be claimed"
        )


def first_party_owner(engine_instance_id: str) -> JobOwner:
    """The single owner every first-party job is created under."""
    if not isinstance(engine_instance_id, str) or not engine_instance_id.strip():
        raise ValueError("engine_instance_id must be a non-empty string")
    return JobOwner(
        plugin_id=FIRST_PARTY_PLUGIN_ID,
        activation_id=engine_instance_id,
    )


def recover_first_party_jobs(
    repository: PluginJobRepository,
    owner: JobOwner,
) -> WorkerCrashResult:
    """Mark every still-active first-party job interrupted, and replay none.

    The in-process runner dies with the engine process, so at startup no
    first-party job can still be executing. Interrupted is a distinct terminal
    outcome the public contract already carries; the caller reruns the work by
    asking for a new refresh, exactly as on the plugin path.
    """
    if not is_first_party_plugin_id(owner.plugin_id):
        raise FirstPartyJobOwnerError("recovery applies only to first-party jobs")
    return repository.mark_worker_crashed(owner)


@dataclass(frozen=True, slots=True)
class FirstPartyJobStart:
    """The job an operation should report, and whether this call created it."""

    job_id: str
    started: bool


class CreateFirstPartyJobUseCase:
    """Create engine-owned jobs. Engine-internal: never a public operation.

    Creation folds the claim in, exactly as ``PluginJobBroker.create`` does, so
    a caller sees a RUNNING job by the time the operation returns. A crash
    between the two leaves the row QUEUED, and startup recovery then marks it
    interrupted rather than replaying it.

    ``idempotency_key`` maps to the same job for as long as that job is not
    terminal. The mapping is process-local by design: after a restart every
    earlier first-party job is already terminal (interrupted), so the same key
    correctly starts fresh work rather than pointing at a dead job.

    The mapping is also bounded by ``_MAX_TRACKED_KEYS``: keys are chosen by the
    caller and are usually a fresh UUID per call, so entries for finished jobs
    are swept before a new one is remembered rather than accumulating for the
    life of the engine process.
    """

    def __init__(
        self,
        repository: PluginJobRepository,
        *,
        owner: JobOwner,
        invocation_factory: Callable[[], str] | None = None,
    ) -> None:
        if not hasattr(repository, "create") or not hasattr(repository, "claim"):
            raise TypeError("repository must implement create and claim")
        if not is_first_party_plugin_id(owner.plugin_id):
            raise FirstPartyJobOwnerError(
                "first-party jobs require the reserved engine plugin namespace"
            )
        self._repository = repository
        self._owner = owner
        self._invocation_factory = invocation_factory or (lambda: str(uuid_module.uuid4()))
        self._lock = threading.Lock()
        self._by_key: dict[tuple[str, str], str] = {}

    def create(self, kind: str, *, idempotency_key: str) -> FirstPartyJobStart:
        if kind not in FIRST_PARTY_JOB_KINDS:
            raise FirstPartyJobKindError("unknown first-party job kind")
        key = _validated_idempotency_key(idempotency_key)
        with self._lock:
            existing = self._by_key.get((kind, key))
            if existing is not None:
                if self._is_active(existing):
                    return FirstPartyJobStart(job_id=existing, started=False)
                del self._by_key[(kind, key)]
            self._prune_tracked_keys_locked()
            record = self._create_running(kind)
            self._by_key[(kind, key)] = record.job_id
            return FirstPartyJobStart(job_id=record.job_id, started=True)

    def abandon(self, kind: str, *, idempotency_key: str, job_id: str) -> None:
        """Fail a job this use case created whose work never started.

        ``create`` hands back a RUNNING row, so a caller that then cannot start
        the work owns a job nothing will ever finish. Failing it here is the
        only way that row reaches a terminal state before the next restart, and
        until it does every retry of the same key is answered with the dead
        job's id.

        Safe to repeat, and safe when something else already terminalized the
        job: a lost race is a result, not an error.
        """
        key = _validated_idempotency_key(idempotency_key)
        with self._lock:
            if self._by_key.get((kind, key)) == job_id:
                del self._by_key[(kind, key)]
        try:
            self._repository.fail(
                FailJobCommand(
                    job_id=job_id,
                    owner=self._owner,
                    failure_code=_ABANDONED_FAILURE_CODE,
                )
            )
        except (JobTerminalConflictError, JobStateConflictError, JobNotFoundError):
            return

    @property
    def tracked_key_count(self) -> int:
        """How many idempotency keys are remembered right now.

        Read by tests and by shutdown diagnostics; never above
        ``_MAX_TRACKED_KEYS``.
        """
        with self._lock:
            return len(self._by_key)

    def _prune_tracked_keys_locked(self) -> None:
        """Keep the key map bounded. The caller already holds the lock.

        An entry whose job has reached a terminal state can never be answered
        again, so it is dead weight. Sweeping only once the map is full keeps
        the cost amortized: one repository read per remembered key, at most
        once every ``_MAX_TRACKED_KEYS`` creates.
        """
        if len(self._by_key) < _MAX_TRACKED_KEYS:
            return
        for entry, job_id in tuple(self._by_key.items()):
            if not self._is_active(job_id):
                del self._by_key[entry]
        # Every remaining job is still active, which means a caller is holding
        # more refreshes open than the engine ever runs honestly. Forgetting
        # the oldest keys costs a repeated key its de-duplication (it starts a
        # second job) and never costs correctness, which is the right trade
        # against letting a client grow engine memory without limit.
        while len(self._by_key) >= _MAX_TRACKED_KEYS:
            self._by_key.pop(next(iter(self._by_key)))

    def _create_running(self, kind: str) -> JobRecord:
        created = self._repository.create(
            CreateJobCommand(
                owner=self._owner,
                invocation_id=self._invocation_factory(),
                operation_id=kind,
                origin_principal_id=FIRST_PARTY_ORIGIN_PRINCIPAL,
            )
        )
        return self._repository.claim(
            ClaimJobCommand(job_id=created.job_id, owner=self._owner)
        )

    def _is_active(self, job_id: str) -> bool:
        try:
            record = self._repository.get(GetJobCommand(job_id=job_id))
        except JobNotFoundError:
            return False
        state = record.state
        value = state.value if isinstance(state, JobState) else str(state)
        return value not in _TERMINAL_STATE_VALUES


def _validated_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("idempotency_key must be a non-empty string")
    if len(value) > _MAX_IDEMPOTENCY_KEY:
        raise ValueError("idempotency_key must be at most 128 characters")
    return value


class FirstPartyJobDirectory:
    """jobs.get / jobs.cancel over the engine's own job repository.

    Deliberately shaped like the external extension host's job methods so one
    dispatch path serves both. ``principal`` is accepted and not used for
    authorization: every row in this repository is engine-owned, and an
    authenticated engine client is entitled to observe and cancel engine work.
    """

    def __init__(self, repository: PluginJobRepository) -> None:
        self._get_job = GetJobUseCase(repository)
        self._cancel_job = CancelJobUseCase(repository)
        # No invoker: an engine job's work lives in this process and dies with
        # it, so there is nothing to hand a checkpoint back to. The use case
        # still validates params, reports an unknown id as not found so
        # dispatch keeps looking, and answers resume_unavailable for a job
        # this directory does own.
        self._resume_job = ResumeJobUseCase(repository)

    def job_get(self, params: Mapping[str, Any], *, principal: str) -> dict[str, Any]:
        return self._get_job.execute(
            params, caller_principal_id=FIRST_PARTY_ORIGIN_PRINCIPAL
        )

    def job_cancel(self, params: Mapping[str, Any], *, principal: str) -> dict[str, Any]:
        return self._cancel_job.execute(
            params, caller_principal_id=FIRST_PARTY_ORIGIN_PRINCIPAL
        )

    def job_resume(self, params: Mapping[str, Any], *, principal: str) -> dict[str, Any]:
        """Always refuses: first-party jobs are not resumable in this wave.

        They are created without the resumable flag, so the use case reaches
        ``resume_unavailable`` on its own rather than by a special case here.
        """
        return self._resume_job.execute(
            params, caller_principal_id=FIRST_PARTY_ORIGIN_PRINCIPAL
        )
