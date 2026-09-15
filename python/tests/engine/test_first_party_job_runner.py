"""In-process runner for engine-owned jobs: cancellation and terminal exclusivity.

The runner is threaded in production, but every test here drives it through an
injected thread factory that runs the work inline, so an assertion never waits
on a timer and a failure names the step that broke rather than a timeout.
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.engine.jobs.first_party import (
    JOB_KIND_PRICES_REFRESH,
    CreateFirstPartyJobUseCase,
    first_party_owner,
)
from model_deck.engine.jobs.ports import (
    CompleteJobCommand,
    GetJobCommand,
    JobState,
    RequestCancelCommand,
)
from model_deck.engine.jobs.runner import (
    InProcessJobRunner,
    JobWorkCancelled,
    JobWorkFailure,
)


class InlineThread:
    """A thread stand-in that runs the target when start() is called."""

    def __init__(self, target, name: str) -> None:
        self._target = target
        self.name = name
        self.started = False

    def start(self) -> None:
        self.started = True
        self._target()

    def join(self, timeout=None) -> None:
        return None

    def is_alive(self) -> bool:
        return False


def inline_thread_factory(target, name):
    return InlineThread(target, name)


class FirstPartyJobRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = SQLitePluginJobRepository(
            Path(temporary.name) / "jobs.sqlite",
            checkpoint_validator=lambda _schema, _value: None,
        )
        self.owner = first_party_owner("11111111-1111-4111-8111-111111111111")
        self.create = CreateFirstPartyJobUseCase(self.repository, owner=self.owner)
        self.runner = InProcessJobRunner(
            self.repository, owner=self.owner, thread_factory=inline_thread_factory
        )

    def _job(self, key: str = "k") -> str:
        return self.create.create(JOB_KIND_PRICES_REFRESH, idempotency_key=key).job_id

    def _state(self, job_id: str) -> JobState:
        return self.repository.get(GetJobCommand(job_id=job_id)).state

    def test_successful_work_completes_the_job_with_its_output(self) -> None:
        job_id = self._job()
        self.runner.submit(job_id, lambda _cancelled: {"kind": "prices", "record_count": 3})
        record = self.repository.get(GetJobCommand(job_id=job_id))
        self.assertIs(record.state, JobState.COMPLETED)
        self.assertTrue(record.output_present)
        self.assertEqual(record.output, {"kind": "prices", "record_count": 3})

    def test_named_failure_code_reaches_durable_state(self) -> None:
        job_id = self._job()

        def work(_cancelled):
            raise JobWorkFailure("provider_unavailable")

        self.runner.submit(job_id, work)
        record = self.repository.get(GetJobCommand(job_id=job_id))
        self.assertIs(record.state, JobState.FAILED)
        self.assertEqual(record.failure_code, "provider_unavailable")

    def test_unexpected_failure_never_leaks_its_reason(self) -> None:
        job_id = self._job()

        def work(_cancelled):
            raise RuntimeError("sensitive upstream detail")

        self.runner.submit(job_id, work)
        record = self.repository.get(GetJobCommand(job_id=job_id))
        self.assertIs(record.state, JobState.FAILED)
        self.assertEqual(record.failure_code, "internal")

    def test_a_cancel_recorded_before_the_work_starts_skips_it(self) -> None:
        job_id = self._job()
        self.repository.request_cancel(
            RequestCancelCommand(job_id=job_id, owner=self.owner)
        )
        ran = []
        self.runner.submit(job_id, lambda _cancelled: ran.append(True))
        self.assertEqual(ran, [])
        self.assertIs(self._state(job_id), JobState.CANCELLED)

    def test_work_that_observes_a_cancel_mid_flight_is_cancelled(self) -> None:
        job_id = self._job()
        observed = []

        def work(is_cancelled):
            # The probe reads durable state, so a cancel that arrived on
            # another connection is visible here.
            observed.append(is_cancelled())
            self.repository.request_cancel(
                RequestCancelCommand(job_id=job_id, owner=self.owner)
            )
            if is_cancelled():
                raise JobWorkCancelled()
            return {"unreachable": True}

        self.runner.submit(job_id, work)
        self.assertEqual(observed, [False])
        self.assertIs(self._state(job_id), JobState.CANCELLED)

    def test_a_late_completion_cannot_overwrite_a_terminal_job(self) -> None:
        job_id = self._job()
        self.repository.complete(
            CompleteJobCommand(job_id=job_id, owner=self.owner, output="first", output_present=True)
        )
        # The runner's terminal write loses the race and stays silent rather
        # than raising into a thread nobody is watching.
        self.runner.submit(job_id, lambda _cancelled: "second")
        record = self.repository.get(GetJobCommand(job_id=job_id))
        self.assertIs(record.state, JobState.COMPLETED)
        self.assertEqual(record.output, "first")

    def test_a_finished_job_stops_being_tracked_whatever_its_outcome(self) -> None:
        """Tracking must not keep one dead thread object per job forever."""

        def fails(_cancelled):
            raise JobWorkFailure("provider_unavailable")

        def cancels(_cancelled):
            raise JobWorkCancelled()

        works = (lambda _cancelled: {"kind": "prices", "record_count": 0}, fails, cancels)
        for index, work in enumerate(works):
            self.runner.submit(self._job(f"k{index}"), work)
            # The inline factory runs the work inside submit(), so by here the
            # terminal write is done and the thread has released itself.
            self.assertEqual(self.runner.active_thread_count, 0)

    def test_a_missing_job_does_not_raise_out_of_the_runner(self) -> None:
        self.runner.submit("00000000-0000-4000-8000-000000000000", lambda _c: "ignored")

    def test_real_threads_run_and_are_joinable(self) -> None:
        """One check that the production path is threads, not the inline stub."""
        runner = InProcessJobRunner(self.repository, owner=self.owner)
        job_id = self._job("threaded")
        release = threading.Event()

        def work(_cancelled):
            release.wait(5)
            return {"kind": "prices", "record_count": 0}

        runner.submit(job_id, work)
        self.assertIs(self._state(job_id), JobState.RUNNING)
        self.assertEqual(runner.active_thread_count, 1)
        release.set()
        self.assertTrue(runner.wait(timeout=5))
        self.assertIs(self._state(job_id), JobState.COMPLETED)

    def test_real_threads_are_released_once_their_jobs_finish(self) -> None:
        """The same bound on the production path, not only on the stub."""
        runner = InProcessJobRunner(self.repository, owner=self.owner)
        job_ids = [self._job(f"threaded-{index}") for index in range(4)]
        for job_id in job_ids:
            runner.submit(job_id, lambda _cancelled: {"kind": "prices", "record_count": 0})
        self.assertTrue(runner.wait(timeout=5))
        # wait() joins every thread it still tracks, and a thread removes
        # itself only after its terminal write, so nothing is left behind.
        self.assertEqual(runner.active_thread_count, 0)
        for job_id in job_ids:
            self.assertIs(self._state(job_id), JobState.COMPLETED)
