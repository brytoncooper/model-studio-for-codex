"""The evidence refresh job service in isolation.

``test_evidence_refresh_jobs.py`` covers the same operations over the socket.
This module drives ``EvidenceRefreshJobService`` directly against a real job
repository so that the one thing the wire path cannot stage — a worker thread
that refuses to start — becomes an injected fact rather than a race.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.engine.evidence import PRICES_KIND, RefreshEvidenceResult
from model_deck.engine.evidence.refresh_jobs import EvidenceRefreshJobService
from model_deck.engine.jobs.first_party import (
    JOB_KIND_PRICES_REFRESH,
    CreateFirstPartyJobUseCase,
    first_party_owner,
)
from model_deck.engine.jobs.ports import GetJobCommand, JobState
from model_deck.engine.jobs.runner import InProcessJobRunner

ENGINE_INSTANCE_ID = "11111111-1111-4111-8111-111111111111"


class InlineThread:
    """A thread stand-in that runs the target when start() is called."""

    def __init__(self, target, name: str) -> None:
        self._target = target
        self.name = name

    def start(self) -> None:
        self._target()

    def join(self, timeout=None) -> None:
        return None

    def is_alive(self) -> bool:
        return False


class RefusingThreadFactory:
    """Runs work inline, or refuses to make a thread at all.

    ``threading.Thread.start`` raises ``RuntimeError`` when the process cannot
    create another thread. This reproduces that without exhausting the real
    process, and ``refuse`` can be turned off to let the next job run.
    """

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse

    def __call__(self, target, name: str):
        if self.refuse:
            raise RuntimeError("can't start new thread")
        return InlineThread(target, name)


class RecordingCreate:
    """The real create use case, plus a record of the jobs it handed out.

    The service does not return the id of a job it could not start, so the test
    learns it here rather than by reading the runner's thread names.
    """

    def __init__(self, inner: CreateFirstPartyJobUseCase) -> None:
        self._inner = inner
        self.started_job_ids: list[str] = []
        self.abandoned_job_ids: list[str] = []

    def create(self, kind: str, *, idempotency_key: str):
        start = self._inner.create(kind, idempotency_key=idempotency_key)
        if start.started:
            self.started_job_ids.append(start.job_id)
        return start

    def abandon(self, kind: str, *, idempotency_key: str, job_id: str) -> None:
        self.abandoned_job_ids.append(job_id)
        self._inner.abandon(kind, idempotency_key=idempotency_key, job_id=job_id)


class StubRefresh:
    """Stands in for ``RefreshEvidenceUseCase``; only its result is used here."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def refresh(self, kind: str) -> RefreshEvidenceResult:
        self.calls.append(kind)
        return RefreshEvidenceResult(kind=kind, refreshed=True, record_count=1)


class EvidenceRefreshJobServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = SQLitePluginJobRepository(
            Path(temporary.name) / "jobs.sqlite",
            checkpoint_validator=lambda _schema, _value: None,
        )
        self.owner = first_party_owner(ENGINE_INSTANCE_ID)
        self.inner_create = CreateFirstPartyJobUseCase(
            self.repository, owner=self.owner
        )
        self.create = RecordingCreate(self.inner_create)
        self.factory = RefusingThreadFactory()
        self.runner = InProcessJobRunner(
            self.repository, owner=self.owner, thread_factory=self.factory
        )
        self.refresh = StubRefresh()
        self.service = EvidenceRefreshJobService(
            refresh=self.refresh, create_job=self.create, runner=self.runner
        )

    def _state(self, job_id: str) -> JobState:
        return self.repository.get(GetJobCommand(job_id=job_id)).state

    def test_a_refresh_that_cannot_start_a_worker_fails_its_job(self) -> None:
        """A created-and-claimed job must never be left with nobody running it."""
        self.factory.refuse = True
        with self.assertRaises(RuntimeError):
            self.service.start(PRICES_KIND, idempotency_key="refresh-1")

        dead_job_id = self.create.started_job_ids[-1]
        self.assertEqual(self.create.abandoned_job_ids, [dead_job_id])
        self.assertIs(self._state(dead_job_id), JobState.FAILED)
        # Nothing is left claimed-but-unworked, so jobs.get answers terminally
        # now instead of at the next restart, and the source was never reached.
        self.assertEqual(self.repository.list_active_for_activation(self.owner), [])
        self.assertEqual(self.refresh.calls, [])

    def test_the_key_of_a_job_that_never_started_is_reusable(self) -> None:
        self.factory.refuse = True
        with self.assertRaises(RuntimeError):
            self.service.start(PRICES_KIND, idempotency_key="refresh-1")
        dead_job_id = self.create.started_job_ids[-1]

        self.factory.refuse = False
        job_id = self.service.start(PRICES_KIND, idempotency_key="refresh-1")
        # The retry is real work, not a pointer at a job nothing will finish.
        self.assertNotEqual(job_id, dead_job_id)
        self.assertIs(self._state(job_id), JobState.COMPLETED)
        self.assertEqual(self.refresh.calls, [PRICES_KIND])

    def test_abandoning_a_job_that_already_finished_is_not_an_error(self) -> None:
        start = self.inner_create.create(
            JOB_KIND_PRICES_REFRESH, idempotency_key="already-done"
        )
        self.runner.submit(
            start.job_id, lambda _cancelled: {"kind": "prices", "record_count": 0}
        )
        self.assertIs(self._state(start.job_id), JobState.COMPLETED)

        self.inner_create.abandon(
            JOB_KIND_PRICES_REFRESH,
            idempotency_key="already-done",
            job_id=start.job_id,
        )
        # Losing the race to a terminal write is a result, not an error, and the
        # outcome that got there first stands.
        self.assertIs(self._state(start.job_id), JobState.COMPLETED)


if __name__ == "__main__":
    unittest.main()
