"""Bounded replacement of activations whose worker died unexpectedly.

The lifecycle adapter settles a lost worker: it interrupts that activation's
jobs, revokes its authority, unpublishes it, and closes the runtime. What it
deliberately does not do is decide whether to try again. That decision lives
here, in one supervisor that keeps a `RestartLedger` per extension and owns a
single timer thread.

A replacement is a plain new activation. It runs the ordinary validate -> admit
path, so it gets a fresh `ActivationIdentity` and, through admission, a fresh
revocation generation. The dead worker's identity stays revoked forever, which
is what makes a surviving old process harmless: the broker rejects its stale
generation. Nothing about a replacement resumes interrupted work — an
interrupted job stays interrupted.

Giving up is visible rather than silent. After the policy's attempt budget is
spent the ledger latches `gave_up`, the supervisor stops scheduling, and
`report()` says so along with the attempt count and the last failure code. An
operator clears it by disabling and re-enabling the extension, which calls
`reset()`; so does a replacement that stays healthy for the policy's
`reset_after_healthy_s`.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from model_deck.engine.extensions.ports import (
    ExtensionRecord,
    ExtensionStatus,
    SelectedInstallation,
    ValidatedActivation,
)
from model_deck.plugins.activation_lifecycle.adapter import (
    ServingActivation,
    WorkerLossNotice,
)
from model_deck.plugins.process_runtime.health import (
    RestartLedger,
    RestartPolicy,
    WorkerHealthState,
)

DEFAULT_RESTART_POLICY = RestartPolicy(
    max_attempts=3,
    initial_backoff_s=0.5,
    multiplier=2.0,
    max_backoff_s=8.0,
    reset_after_healthy_s=60.0,
)
"""Three tries, 0.5s -> 1s -> 2s, capped at 8s, forgiven after a healthy minute.

Small enough that a plugin crashing on a transient condition recovers without
an operator, bounded enough that one crashing on every start stops burning
processes and says why.
"""

_IDLE_WAIT_S = 3600.0
"""How long the timer thread parks when nothing is scheduled.

It is woken by `notify_all` on every state change, so this is only a ceiling.
"""

SUPERVISION_HEALTHY = "healthy"
SUPERVISION_RESTARTING = "restarting"
SUPERVISION_DEGRADED = "degraded"
SUPERVISION_IDLE = "idle"


@runtime_checkable
class SupervisedActivationLifecycle(Protocol):
    """The lifecycle capability a replacement needs, and nothing more."""

    def serving(self, extension_id: str) -> ServingActivation | None: ...

    def validate(
        self,
        operation_id: str,
        candidate: SelectedInstallation,
    ) -> ValidatedActivation: ...

    def admit(
        self,
        operation_id: str,
        record: ExtensionRecord,
        activation: ValidatedActivation | None,
        *,
        expected_data_revision: int,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ExtensionSupervisionReport:
    """One extension's worker health and restart history, for an operator.

    `supervision_status` is the single word to show: healthy while an
    activation is serving, restarting while a replacement is scheduled,
    degraded once the supervisor gave up, idle when nothing is running and
    nothing is owed. `next_attempt_in_s` is None unless a restart is pending.
    """

    extension_id: str
    supervision_status: str
    worker_state: WorkerHealthState
    restart_attempts: int
    last_failure_code: str | None
    gave_up: bool
    jobs_interrupted: int
    next_attempt_in_s: float | None


class ActivationRestartSupervisor:
    """Replace lost activations with backoff, and stop when it is not working.

    One timer thread serves every supervised extension. It holds no lifecycle
    lock while it sleeps, and performs each replacement outside this object's
    own lock, so a slow spawn never blocks a concurrent loss report.
    """

    def __init__(
        self,
        *,
        lifecycle: SupervisedActivationLifecycle,
        record_reader: Callable[[str], ExtensionRecord | None],
        policy: RestartPolicy = DEFAULT_RESTART_POLICY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(lifecycle, SupervisedActivationLifecycle):
            raise ValueError("lifecycle")
        if not callable(record_reader):
            raise ValueError("record_reader")
        if not isinstance(policy, RestartPolicy):
            raise ValueError("policy")
        if not callable(clock):
            raise ValueError("clock")
        self._lifecycle = lifecycle
        self._read_record = record_reader
        self._policy = policy
        self._clock = clock
        self._wake = threading.Condition(threading.Lock())
        self._ledgers: dict[str, RestartLedger] = {}
        self._restart_due: dict[str, float] = {}
        self._health_due: dict[str, float] = {}
        self._notices: dict[str, WorkerLossNotice] = {}
        self._stopped = False
        self._thread = threading.Thread(
            target=self._serve,
            name="model-deck-activation-restart",
            daemon=True,
        )
        self._thread.start()

    @property
    def policy(self) -> RestartPolicy:
        return self._policy

    def on_worker_loss_settled(self, notice: WorkerLossNotice) -> None:
        """Count one loss and schedule the next allowed replacement.

        Called by the lifecycle adapter after it finished settling. This never
        starts a replacement inline: it records and returns, so the adapter's
        supervision thread is free again immediately.
        """

        if not isinstance(notice, WorkerLossNotice):
            return
        now = self._clock()
        with self._wake:
            if self._stopped:
                return
            ledger = self._ledgers.get(notice.extension_id, RestartLedger())
            ledger = ledger.record_failure(self._policy, now, notice.failure_code)
            self._ledgers[notice.extension_id] = ledger
            self._notices[notice.extension_id] = notice
            self._health_due.pop(notice.extension_id, None)
            if ledger.gave_up:
                self._restart_due.pop(notice.extension_id, None)
            else:
                self._restart_due[notice.extension_id] = (
                    ledger.next_allowed_at_monotonic
                )
            self._wake.notify_all()

    def reset(self, extension_id: str) -> None:
        """Forget one extension's restart history.

        The operator's way out of a degraded extension: disable and re-enable
        it. Also used when an extension is removed, so its ledger does not
        outlive it.
        """

        with self._wake:
            self._ledgers.pop(extension_id, None)
            self._restart_due.pop(extension_id, None)
            self._health_due.pop(extension_id, None)
            self._notices.pop(extension_id, None)
            self._wake.notify_all()

    def note_healthy_start(self, extension_id: str) -> None:
        """Schedule the healthy-run check for an activation started elsewhere.

        A replacement this supervisor performed schedules its own check. This
        is for the other path into serving: an operator enable, or startup
        recovery, after a history of failures.
        """

        with self._wake:
            ledger = self._ledgers.get(extension_id)
            if ledger is None:
                return
            self._health_due[extension_id] = self._healthy_deadline(ledger)
            self._wake.notify_all()

    def report(self, extension_id: str) -> ExtensionSupervisionReport:
        """Describe one extension's supervision state right now."""

        now = self._clock()
        with self._wake:
            ledger = self._ledgers.get(extension_id, RestartLedger())
            notice = self._notices.get(extension_id)
            due_at = self._restart_due.get(extension_id)
        serving = self._lifecycle.serving(extension_id) is not None
        if ledger.gave_up:
            status = SUPERVISION_DEGRADED
            worker_state = WorkerHealthState.DEAD
        elif due_at is not None:
            status = SUPERVISION_RESTARTING
            worker_state = WorkerHealthState.DEAD
        elif serving:
            status = SUPERVISION_HEALTHY
            worker_state = WorkerHealthState.HEALTHY
        else:
            status = SUPERVISION_IDLE
            worker_state = (
                WorkerHealthState.DEAD
                if notice is not None
                else WorkerHealthState.STARTING
            )
        return ExtensionSupervisionReport(
            extension_id,
            status,
            worker_state,
            ledger.attempts,
            None if ledger.last_failure_code is None else ledger.last_failure_code.value,
            ledger.gave_up,
            0 if notice is None else notice.jobs_interrupted,
            None if due_at is None else max(0.0, due_at - now),
        )

    def supervised_extension_ids(self) -> tuple[str, ...]:
        """Every extension this supervisor holds any history for."""

        with self._wake:
            return tuple(sorted(set(self._ledgers) | set(self._notices)))

    def close(self) -> None:
        """Stop scheduling and join the timer thread."""

        with self._wake:
            if self._stopped:
                return
            self._stopped = True
            self._restart_due.clear()
            self._health_due.clear()
            self._wake.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)

    def _healthy_deadline(self, ledger: RestartLedger) -> float:
        # `RestartLedger.record_healthy` measures the healthy run from the
        # earliest moment the replacement could have started, so waking any
        # sooner than that would always find the ledger unchanged.
        return ledger.next_allowed_at_monotonic + self._policy.reset_after_healthy_s

    def _serve(self) -> None:
        while True:
            with self._wake:
                if self._stopped:
                    return
                due_at = self._earliest_due()
                if due_at is None:
                    self._wake.wait(_IDLE_WAIT_S)
                    continue
                delay = due_at - self._clock()
                if delay > 0:
                    self._wake.wait(delay)
                    continue
                now = self._clock()
                restarts = tuple(
                    extension_id
                    for extension_id, at in self._restart_due.items()
                    if at <= now
                )
                for extension_id in restarts:
                    self._restart_due.pop(extension_id, None)
                checks = tuple(
                    extension_id
                    for extension_id, at in self._health_due.items()
                    if at <= now
                )
                for extension_id in checks:
                    self._health_due.pop(extension_id, None)
            for extension_id in restarts:
                self._attempt_replacement(extension_id)
            for extension_id in checks:
                self._check_healthy_run(extension_id)

    def _earliest_due(self) -> float | None:
        times = (*self._restart_due.values(), *self._health_due.values())
        return min(times) if times else None

    def _attempt_replacement(self, extension_id: str) -> None:
        record = self._enabled_record(extension_id)
        if record is None:
            # Disabled, removed, or unreadable: there is nothing to replace,
            # and an operator enable will register its own fresh activation.
            return
        if self._lifecycle.serving(extension_id) is not None:
            self._on_replacement_serving(extension_id)
            return
        operation_id = str(uuid.uuid4())
        try:
            validated = self._lifecycle.validate(operation_id, record.selected)
            self._lifecycle.admit(
                operation_id,
                record,
                validated,
                expected_data_revision=validated.validated_data_revision,
            )
        except Exception:
            # The replacement itself failed to come up. That is another failure
            # against the same budget, not a separate kind of problem.
            self._record_attempt_failure(extension_id)
            return
        self._on_replacement_serving(extension_id)

    def _enabled_record(self, extension_id: str) -> ExtensionRecord | None:
        try:
            record = self._read_record(extension_id)
        except Exception:
            return None
        if record is None or record.status is not ExtensionStatus.ENABLED:
            return None
        return record

    def _record_attempt_failure(self, extension_id: str) -> None:
        now = self._clock()
        with self._wake:
            if self._stopped:
                return
            ledger = self._ledgers.get(extension_id, RestartLedger())
            ledger = ledger.record_failure(self._policy, now)
            self._ledgers[extension_id] = ledger
            if not ledger.gave_up:
                self._restart_due[extension_id] = ledger.next_allowed_at_monotonic
            self._wake.notify_all()

    def _on_replacement_serving(self, extension_id: str) -> None:
        with self._wake:
            if self._stopped:
                return
            ledger = self._ledgers.get(extension_id)
            if ledger is None:
                return
            self._health_due[extension_id] = self._healthy_deadline(ledger)
            self._wake.notify_all()

    def _check_healthy_run(self, extension_id: str) -> None:
        serving = self._lifecycle.serving(extension_id) is not None
        now = self._clock()
        with self._wake:
            if self._stopped:
                return
            ledger = self._ledgers.get(extension_id)
            if ledger is None:
                return
            if not serving:
                return
            refreshed = ledger.record_healthy(self._policy, now)
            if refreshed is ledger:
                self._health_due[extension_id] = self._healthy_deadline(ledger)
                self._wake.notify_all()
                return
            self._ledgers.pop(extension_id, None)
            self._notices.pop(extension_id, None)
            self._health_due.pop(extension_id, None)


__all__ = [
    "ActivationRestartSupervisor",
    "DEFAULT_RESTART_POLICY",
    "ExtensionSupervisionReport",
    "SUPERVISION_DEGRADED",
    "SUPERVISION_HEALTHY",
    "SUPERVISION_IDLE",
    "SUPERVISION_RESTARTING",
    "SupervisedActivationLifecycle",
]
