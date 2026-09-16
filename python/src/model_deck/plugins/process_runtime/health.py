"""Worker health, worker-loss, and restart-policy records. Records only.

This module holds the vocabulary two later units need and nothing that acts on
it. There is no I/O, no thread, no clock read, and no process handling here;
every timestamp is passed in by the caller as a `time.monotonic()` reading, and
every method returns a new value instead of mutating anything.

Who uses what:

* `ProcessRuntime` accepts `worker_loss_listener` as a keyword argument to its
  constructor. It is a `WorkerLossListener` (or None, the default: no listener,
  and the runtime behaves exactly as it does today). When a worker is lost,
  `_stop` calls `on_worker_lost(event)` exactly once with a `WorkerLossEvent`
  that distinguishes the loss kinds the runtime can already tell apart —
  today's `malformed_eof` EOF and the per-exchange `timeout` end in the same
  silent `_stop`. **U12 implements that call; this module only names it.**
* `RestartPolicy` and `RestartLedger` are owned by the activation lifecycle,
  not by `ProcessRuntime`. A runtime never restarts itself and cannot respawn
  after closing. **U13 owns deciding whether and when to start a replacement
  activation, using a ledger it keeps per activation.**

`WorkerLossCode` deliberately mirrors the distinctions the runtime can make
about a lost child. It is not the domain error vocabulary and does not travel
on the wire; mapping a loss to a domain error is the caller's job.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

MAX_RESTART_ATTEMPTS = 1024
"""Upper bound on `RestartPolicy.max_attempts`.

A policy is a small operator-facing knob, not a counter to run to exhaustion.
Bounding it keeps the backoff computation cheap and finite.
"""


class WorkerHealthState(str, Enum):
    """What the supervisor currently believes about one worker."""

    STARTING = "starting"
    """Spawned, but has not yet proven itself with a first heartbeat."""

    HEALTHY = "healthy"
    """Answered within the heartbeat deadline."""

    UNRESPONSIVE = "unresponsive"
    """Missed at least one heartbeat but the process is still alive."""

    DEAD = "dead"
    """The process is gone; a `WorkerLossEvent` describes why."""


class WorkerLossCode(str, Enum):
    """Why a worker was lost, at the granularity the runtime can actually tell."""

    MALFORMED_EOF = "malformed_eof"
    """Stdout reached EOF mid-stream; the same code the runtime already raises."""

    TIMEOUT = "timeout"
    """A per-exchange deadline elapsed with no response."""

    UNRESPONSIVE = "unresponsive"
    """Heartbeats were missed past the policy's tolerance."""

    EXITED = "exited"
    """The child exited on its own, with or without a non-zero status."""

    KILLED = "killed"
    """The supervisor terminated and reaped the child."""


WORKER_LOSS_CODES: frozenset[str] = frozenset(code.value for code in WorkerLossCode)
"""Every `WorkerLossCode` value, for callers that mirror the vocabulary."""


def _require_finite(name: str, value: object) -> float:
    if type(value) is not float and type(value) is not int:
        raise ValueError(name)
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise ValueError(name)
    return number


@dataclass(frozen=True, slots=True)
class WorkerHealth:
    """One worker's heartbeat standing at a moment in time.

    `last_heartbeat_monotonic` is None while the worker is STARTING and has
    never answered; otherwise it is the `time.monotonic()` reading of the most
    recent answer. `consecutive_missed` counts heartbeats missed in a row since
    the last answer, and resets to zero on any answer.
    """

    state: WorkerHealthState
    last_heartbeat_monotonic: float | None
    consecutive_missed: int

    def __post_init__(self) -> None:
        if not isinstance(self.state, WorkerHealthState):
            raise ValueError("state")
        if self.last_heartbeat_monotonic is not None:
            _require_finite("last_heartbeat_monotonic", self.last_heartbeat_monotonic)
        if type(self.consecutive_missed) is not int or self.consecutive_missed < 0:
            raise ValueError("consecutive_missed")


@dataclass(frozen=True, slots=True)
class WorkerLossEvent:
    """One worker's loss, reported once.

    `activation_id` is None when the worker died before an activation was
    bound, so there is no activation to name. `exit_code` is None whenever the
    supervisor has no status for the child — it was never reaped, or the loss
    was decided from the stream rather than from the process.
    """

    activation_id: str | None
    failure_code: WorkerLossCode
    at_monotonic: float
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if self.activation_id is not None and (
            type(self.activation_id) is not str or not self.activation_id
        ):
            raise ValueError("activation_id")
        if not isinstance(self.failure_code, WorkerLossCode):
            if type(self.failure_code) is str and self.failure_code in WORKER_LOSS_CODES:
                object.__setattr__(
                    self, "failure_code", WorkerLossCode(self.failure_code)
                )
            else:
                raise ValueError("failure_code")
        _require_finite("at_monotonic", self.at_monotonic)
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise ValueError("exit_code")


@runtime_checkable
class WorkerLossListener(Protocol):
    """Receives exactly one call per lost worker.

    The call happens on the runtime's own stop path, so an implementation must
    return promptly and must not raise: it records or hands the event off, and
    does not restart anything itself.
    """

    def on_worker_lost(self, event: WorkerLossEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    """How often, and how fast, a lost worker may be replaced.

    `max_attempts` is how many restarts are allowed before the ledger gives up;
    0 means never restart, so the first failure is terminal. Backoff for the
    nth failure is `initial_backoff_s * multiplier ** (n - 1)`, clamped to
    `max_backoff_s`. `reset_after_healthy_s` is how long a replacement must
    stay healthy before its predecessor's failures stop counting.
    """

    max_attempts: int
    initial_backoff_s: float
    multiplier: float
    max_backoff_s: float
    reset_after_healthy_s: float

    def __post_init__(self) -> None:
        if (
            type(self.max_attempts) is not int
            or self.max_attempts < 0
            or self.max_attempts > MAX_RESTART_ATTEMPTS
        ):
            raise ValueError("max_attempts")
        initial = _require_finite("initial_backoff_s", self.initial_backoff_s)
        if initial <= 0:
            raise ValueError("initial_backoff_s")
        multiplier = _require_finite("multiplier", self.multiplier)
        if multiplier < 1.0:
            raise ValueError("multiplier")
        maximum = _require_finite("max_backoff_s", self.max_backoff_s)
        if maximum < initial:
            raise ValueError("max_backoff_s")
        reset_after = _require_finite("reset_after_healthy_s", self.reset_after_healthy_s)
        if reset_after < 0:
            raise ValueError("reset_after_healthy_s")


def backoff_for_attempt(policy: RestartPolicy, attempt: int) -> float:
    """Seconds to wait before the nth restart attempt (1-based), clamped.

    Computed by repeated multiplication with an early clamp rather than by
    exponentiation, so a large attempt count can never produce an infinity.
    """

    if type(attempt) is not int or attempt < 1:
        raise ValueError("attempt")
    backoff = policy.initial_backoff_s
    for _ in range(attempt - 1):
        if backoff >= policy.max_backoff_s:
            return policy.max_backoff_s
        backoff *= policy.multiplier
    return min(backoff, policy.max_backoff_s)


@dataclass(frozen=True, slots=True)
class RestartLedger:
    """One activation's restart history. Every method returns a new ledger.

    A default-constructed ledger is clean: no failures, nothing to wait for,
    and `can_attempt` is True.

    `gave_up` latches. Once the policy's attempt budget is spent, further
    failures change nothing, and only a sustained healthy run through
    `record_healthy` clears the history.
    """

    attempts: int = 0
    next_allowed_at_monotonic: float = 0.0
    last_failure_code: WorkerLossCode | None = None
    gave_up: bool = False

    def __post_init__(self) -> None:
        if type(self.attempts) is not int or self.attempts < 0:
            raise ValueError("attempts")
        _require_finite("next_allowed_at_monotonic", self.next_allowed_at_monotonic)
        if self.last_failure_code is not None and not isinstance(
            self.last_failure_code, WorkerLossCode
        ):
            raise ValueError("last_failure_code")
        if type(self.gave_up) is not bool:
            raise ValueError("gave_up")

    def record_failure(
        self,
        policy: RestartPolicy,
        now: float,
        failure_code: WorkerLossCode | None = None,
    ) -> RestartLedger:
        """Count one failure and schedule the next allowed attempt.

        `failure_code` is optional; when omitted the previously recorded code
        is kept, so a caller that has no code to give does not erase one.

        A ledger that has already given up is terminal: this returns it
        unchanged, so a crash loop cannot inflate the attempt count.
        """

        if not isinstance(policy, RestartPolicy):
            raise ValueError("policy")
        _require_finite("now", now)
        if failure_code is not None and not isinstance(failure_code, WorkerLossCode):
            raise ValueError("failure_code")
        if self.gave_up:
            return self
        attempts = self.attempts + 1
        code = failure_code if failure_code is not None else self.last_failure_code
        if attempts > policy.max_attempts:
            return RestartLedger(
                attempts=attempts,
                next_allowed_at_monotonic=self.next_allowed_at_monotonic,
                last_failure_code=code,
                gave_up=True,
            )
        return RestartLedger(
            attempts=attempts,
            next_allowed_at_monotonic=now + backoff_for_attempt(policy, attempts),
            last_failure_code=code,
            gave_up=False,
        )

    def record_healthy(self, policy: RestartPolicy, now: float) -> RestartLedger:
        """Forget the history once the replacement has stayed healthy long enough.

        The current healthy run is measured from `next_allowed_at_monotonic` —
        the earliest moment the replacement could have started. Once
        `reset_after_healthy_s` has passed beyond that point, this returns a
        clean ledger, clearing the `gave_up` latch with it. Before then, and
        for a ledger that is already clean, it returns the ledger unchanged.
        """

        if not isinstance(policy, RestartPolicy):
            raise ValueError("policy")
        _require_finite("now", now)
        if self.attempts == 0 and not self.gave_up and self.last_failure_code is None:
            return self
        if now >= self.next_allowed_at_monotonic + policy.reset_after_healthy_s:
            return RestartLedger()
        return self

    def can_attempt(self, now: float) -> bool:
        """True when a restart is permitted right now."""

        _require_finite("now", now)
        if self.gave_up:
            return False
        return now >= self.next_allowed_at_monotonic
