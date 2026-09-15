"""Evidence refreshes as first-party jobs.

``prices.refresh`` and ``benchmarks.refresh`` do not fetch inline. They create
an engine-owned job, hand the work to the in-process runner and return the job
id, so the caller observes progress through ``jobs.get`` and stops it through
``jobs.cancel`` like any other job. A read path never reaches a source, so this
is the only place a refresh becomes work.
"""
from __future__ import annotations

from collections.abc import Mapping

from model_deck.engine.evidence.ports import (
    BENCHMARKS_KIND,
    PRICES_KIND,
    REFRESH_FETCH_FAILED,
    REFRESH_INVALID_PAYLOAD,
    REFRESH_TOO_MANY_RECORDS,
)
from model_deck.engine.evidence.use_cases import RefreshEvidenceUseCase
from model_deck.engine.jobs.first_party import (
    JOB_KIND_BENCHMARKS_REFRESH,
    JOB_KIND_PRICES_REFRESH,
    CreateFirstPartyJobUseCase,
)
from model_deck.engine.jobs.runner import (
    CancellationProbe,
    InProcessJobRunner,
    JobWorkCancelled,
    JobWorkFailure,
)

__all__ = [
    "EVIDENCE_JOB_KINDS",
    "EvidenceRefreshJobService",
    "REFRESH_FAILURE_CODES",
]

EVIDENCE_JOB_KINDS: Mapping[str, str] = {
    PRICES_KIND: JOB_KIND_PRICES_REFRESH,
    BENCHMARKS_KIND: JOB_KIND_BENCHMARKS_REFRESH,
}
"""Evidence kind to the frozen job_kind string the contracts vocabulary names."""

REFRESH_FAILURE_CODES: Mapping[str, str] = {
    REFRESH_FETCH_FAILED: "provider_unavailable",
    REFRESH_INVALID_PAYLOAD: "provider_unavailable",
    REFRESH_TOO_MANY_RECORDS: "resource_exhausted",
}
"""Cache failure code to the public job failure code. Both are fixed vocabularies.

The public job view carries no failure code, so this only shapes durable job
state. The operator-visible reason stays on the cache snapshot, which a query
reports as ``stale`` plus ``last_refresh_error``.
"""

_UNKNOWN_FAILURE_CODE = "internal"


class EvidenceRefreshJobService:
    """Starts refresh jobs and keeps one job per live idempotency key."""

    def __init__(
        self,
        *,
        refresh: RefreshEvidenceUseCase,
        create_job: CreateFirstPartyJobUseCase,
        runner: InProcessJobRunner,
    ) -> None:
        self._refresh = refresh
        self._create_job = create_job
        self._runner = runner

    def start(self, kind: str, *, idempotency_key: str) -> str:
        """Return the job id running this refresh, starting it when needed.

        Repeating a key while its job is still active returns that job id and
        starts no second fetch. Once the job is terminal the key is free again,
        so a later retry is genuinely new work rather than a stale answer.
        """
        job_kind = EVIDENCE_JOB_KINDS[kind]
        start = self._create_job.create(job_kind, idempotency_key=idempotency_key)
        if start.started:
            try:
                self._runner.submit(start.job_id, self._work(kind))
            except BaseException:
                # The job is already RUNNING, and a submit that raised left no
                # worker to finish it: starting a thread can fail. An unfinished
                # job would hold this key for the life of the process and answer
                # every retry with an id that never reaches a terminal state, so
                # end it here and let the caller see the failure.
                self._create_job.abandon(
                    job_kind,
                    idempotency_key=idempotency_key,
                    job_id=start.job_id,
                )
                raise
        return start.job_id

    def _work(self, kind: str):
        def run(is_cancelled: CancellationProbe) -> dict[str, object]:
            if is_cancelled():
                raise JobWorkCancelled()
            outcome = self._refresh.refresh(kind)
            if is_cancelled():
                # The cache write already happened and is kept: a cancel does
                # not roll back evidence the engine successfully fetched.
                raise JobWorkCancelled()
            if not outcome.refreshed:
                raise JobWorkFailure(
                    REFRESH_FAILURE_CODES.get(outcome.error_code, _UNKNOWN_FAILURE_CODE)
                )
            return {"kind": outcome.kind, "record_count": outcome.record_count}

        return run
