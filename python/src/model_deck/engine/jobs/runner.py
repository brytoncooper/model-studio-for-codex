"""In-process runner for first-party (engine-owned) jobs.

The plugin path has a worker process; a first-party job has nothing to run it,
so this is the minimum that lets one execute inside the engine: one daemon
thread per job, a cancellation flag read from durable state rather than from
memory, and exactly one terminal write per job.

Three properties matter and are enforced here rather than left to each caller:

* **Terminal exclusivity.** Every terminal write goes through ``_terminalize``,
  which treats "already terminal" as a completed race, not an error. The first
  writer wins, so a cancel confirmed by another path can never be overwritten
  by a late completion.
* **Cancellation is durable.** ``is_cancelled`` re-reads ``cancel_requested``
  from the repository, so a jobs.cancel that arrived on another connection (or
  before this thread started) is observed.
* **Nothing is replayed.** The runner dies with the process. Jobs left active
  are marked interrupted at the next startup by
  ``recover_first_party_jobs``; the runner never resumes them.

First-party jobs are not explicitly resumable either in this wave: they are
created without the resumable flag, so ``engine.v1.jobs.resume`` on one answers
``resume_unavailable``. There is nothing to hand a checkpoint back to, because
the work lived in this process; the way to run it again is to ask for a new
refresh.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from model_deck.engine.jobs.ports import (
    FAILURE_CODES,
    CompleteJobCommand,
    ConfirmCancelCommand,
    FailJobCommand,
    GetJobCommand,
    JobNotFoundError,
    JobOwner,
    JobStateConflictError,
    JobTerminalConflictError,
    PluginJobRepository,
)

__all__ = [
    "CancellationProbe",
    "InProcessJobRunner",
    "JobWork",
    "JobWorkCancelled",
    "JobWorkFailure",
]

CancellationProbe = Callable[[], bool]
JobWork = Callable[[CancellationProbe], Any]

_DEFAULT_FAILURE_CODE = "internal"


class JobWorkCancelled(Exception):
    """Work observed a cancel request and stopped at a safe point."""


class JobWorkFailure(Exception):
    """Work failed with a named failure code from the frozen vocabulary."""

    def __init__(self, failure_code: str) -> None:
        if failure_code not in FAILURE_CODES:
            raise ValueError("failure_code must be a contract failure code")
        super().__init__(failure_code)
        self.failure_code = failure_code


class InProcessJobRunner:
    """Runs first-party job work on daemon threads owned by the engine.

    ``submit`` expects a job the caller has already created and claimed, so the
    row is RUNNING before any thread starts. The work callable receives a
    cancellation probe and returns the job's output; returning ``None`` records
    an explicit JSON null, which the public view reports as a present output.
    """

    def __init__(
        self,
        repository: PluginJobRepository,
        *,
        owner: JobOwner,
        thread_factory: Callable[[Callable[[], None], str], threading.Thread] | None = None,
    ) -> None:
        if not hasattr(repository, "complete") or not hasattr(repository, "fail"):
            raise TypeError("repository must implement complete and fail")
        self._repository = repository
        self._owner = owner
        self._thread_factory = thread_factory or _daemon_thread
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    def submit(self, job_id: str, work: JobWork) -> None:
        """Start ``work`` for an already-running job."""
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id must be a non-empty string")
        if not callable(work):
            raise TypeError("work must be callable")
        thread = self._thread_factory(
            lambda: self._run(job_id, work), f"model-deck-job-{job_id}"
        )
        with self._lock:
            self._threads[job_id] = thread
        thread.start()

    def wait(self, timeout: float | None = None) -> bool:
        """Join every thread still running; True when all of them finished.

        Only the engine's own tests and shutdown need this. It never cancels:
        a caller that wants a job stopped asks through jobs.cancel. A thread
        that has already written its job's terminal outcome has removed itself,
        so there is nothing left to join for it.
        """
        with self._lock:
            threads = tuple(self._threads.values())
        for thread in threads:
            thread.join(timeout)
            if thread.is_alive():
                return False
        return True

    @property
    def active_thread_count(self) -> int:
        """How many submitted jobs have not reached a terminal write yet.

        Read by tests and by shutdown diagnostics. It returns to zero once
        every submitted job has finished, so a long-lived engine does not
        accumulate one dead thread object per refresh.
        """
        with self._lock:
            return len(self._threads)

    def _run(self, job_id: str, work: JobWork) -> None:
        """Run the work, then stop tracking the thread that ran it."""
        try:
            self._execute(job_id, work)
        finally:
            # Released only after the terminal write, so wait() still joins a
            # thread that is mid-write, and a finished job leaves nothing
            # behind: tracking is bounded by the jobs actually running.
            with self._lock:
                self._threads.pop(job_id, None)

    def _execute(self, job_id: str, work: JobWork) -> None:
        if self._cancel_requested(job_id):
            self._terminalize(
                ConfirmCancelCommand(job_id=job_id, owner=self._owner),
                self._repository.confirm_cancel,
            )
            return
        try:
            output = work(lambda: self._cancel_requested(job_id))
        except JobWorkCancelled:
            self._terminalize(
                ConfirmCancelCommand(job_id=job_id, owner=self._owner),
                self._repository.confirm_cancel,
            )
            return
        except JobWorkFailure as failure:
            self._fail(job_id, failure.failure_code)
            return
        except Exception:
            # The reason never reaches durable job state: failure_code is a
            # fixed vocabulary value, so no payload can leak through jobs.get.
            self._fail(job_id, _DEFAULT_FAILURE_CODE)
            return
        self._terminalize(
            CompleteJobCommand(
                job_id=job_id,
                owner=self._owner,
                output=output,
                output_present=True,
            ),
            self._repository.complete,
        )

    def _fail(self, job_id: str, failure_code: str) -> None:
        self._terminalize(
            FailJobCommand(
                job_id=job_id, owner=self._owner, failure_code=failure_code
            ),
            self._repository.fail,
        )

    def _cancel_requested(self, job_id: str) -> bool:
        try:
            record = self._repository.get(GetJobCommand(job_id=job_id))
        except JobNotFoundError:
            return False
        return bool(record.cancel_requested)

    @staticmethod
    def _terminalize(command: Any, write: Callable[[Any], Any]) -> None:
        """Write one terminal outcome; a lost race is a result, not an error."""
        try:
            write(command)
        except (JobTerminalConflictError, JobStateConflictError, JobNotFoundError):
            return


def _daemon_thread(target: Callable[[], None], name: str) -> threading.Thread:
    return threading.Thread(target=target, name=name, daemon=True)
