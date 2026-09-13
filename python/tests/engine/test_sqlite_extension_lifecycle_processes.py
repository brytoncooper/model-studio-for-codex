"""Cross-process exclusion and replay durability for the SQLite B20 lifecycle.

These tests intentionally spawn fresh OS processes so that the claim,
admission guard, and receipt durability are exercised across independent
SQLite connections. They only consume the public
``SQLiteExtensionLifecycleRepository`` and ``engine.extensions.ports``
contracts and reuse helpers from ``test_sqlite_extension_lifecycle``.

There is no live application, provider, network, Git or build step.
Each test allocates its own temporary database and tears down all
worker processes under a hard bounded deadline before the test exits.
"""
from __future__ import annotations

import multiprocessing
import multiprocessing.synchronize
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from model_deck.adapters.storage.sqlite_extension_lifecycle import (
    SQLiteExtensionLifecycleRepository,
)
from model_deck.engine.extensions.ports import (
    ClaimDisposition,
    ExecutableArtifact,
    ExtensionLifecycleRepository,
    ExtensionStatus,
    LifecycleAction,
    LifecycleConflictError,
    LifecycleOperation,
    LifecyclePhase,
    LifecycleRequest,
    ReceiptOutcome,
    SelectedInstallation,
    StagedData,
)


EXTENSION_ID = "org.example.processes.lifecycle"
PROCESS_ARTIFACT = ExecutableArtifact(
    "f" * 64,
    EXTENSION_ID,
    "1.0.0",
    ("data.read", "data.write"),
)
PROCESS_OP_A = "10000000-0000-4000-8000-000000000a01"
PROCESS_OP_B = "10000000-0000-4000-8000-000000000a02"
PROCESS_OP_REPLAY = "10000000-0000-4000-8000-000000000a03"
PROCESS_OP_IN_PROGRESS = "10000000-0000-4000-8000-000000000a04"

PROCESS_KEY_A = "install-a"
PROCESS_KEY_B = "install-b"
PROCESS_KEY_REPLAY = "install-replay"
PROCESS_KEY_IN_PROGRESS = "install-pending"

PROCESS_PRINCIPAL_A = "ref:operator-a"
PROCESS_PRINCIPAL_B = "ref:operator-b"

PROCESS_HARD_DEADLINE_SECONDS = 15.0
PROCESS_QUEUE_TIMEOUT_SECONDS = 10.0
PROCESS_STARTUP_TIMEOUT_SECONDS = 5.0


def _request(
    operation_id: str,
    *,
    principal: str,
    idempotency_key: str,
    digest_character: str,
    candidate: ExecutableArtifact = PROCESS_ARTIFACT,
    extension_id: str = EXTENSION_ID,
    expected_revision: int = 0,
) -> LifecycleRequest:
    return LifecycleRequest(
        operation_id=operation_id,
        principal_ref=principal,
        action=LifecycleAction.INSTALL,
        extension_id=extension_id,
        expected_revision=expected_revision,
        request_digest=digest_character * 64,
        idempotency_key=idempotency_key,
        candidate=candidate,
    )


def _settle_install(repo: SQLiteExtensionLifecycleRepository, request: LifecycleRequest) -> LifecycleOperation:
    """Drive a fresh install through CLAIMED -> QUIESCED -> DATA_STAGED -> SWITCHED -> SETTLED.

    Mirrors ``SQLiteExtensionLifecycleTests._install`` so the result is
    comparable to the in-process install path in ``test_sqlite_extension_lifecycle``.
    """
    claimed = repo.claim(request).operation
    assert claimed is not None
    quiesced = repo.advance(
        replace(claimed, phase=LifecyclePhase.QUIESCED, phase_revision=1),
        expected_phase_revision=0,
    )
    candidate = SelectedInstallation(PROCESS_ARTIFACT, "ref:data.v1", (), 0, 0)
    staged = repo.advance(
        replace(
            quiesced,
            phase=LifecyclePhase.DATA_STAGED,
            phase_revision=2,
            candidate=candidate,
            staged_data=StagedData("ref:data.v1", 0, "ref:migration.v1"),
        ),
        expected_phase_revision=1,
    )
    switched = repo.switch(request.operation_id, expected_phase_revision=staged.phase_revision)
    repo.settle(request.operation_id, expected_phase_revision=switched.phase_revision)
    return switched


def _payload(request: LifecycleRequest) -> dict[str, Any]:
    """Return a multiprocessing-safe kwargs payload reconstructed in the worker."""
    return {
        "operation_id": request.operation_id,
        "principal_ref": request.principal_ref,
        "action": request.action,
        "extension_id": request.extension_id,
        "expected_revision": request.expected_revision,
        "request_digest": request.request_digest,
        "idempotency_key": request.idempotency_key,
        "candidate": request.candidate,
        "approved_scopes": request.approved_scopes,
    }


# ---------------------------------------------------------------------------
# Multiprocessing workers
#
# Every worker has the same signature so the parent can drive them with a
# single helper:
#   db_path_text, request_payload, started, release, result
# ``started`` is set by the worker once it is ready; ``release`` is set by
# the parent when the worker should proceed to ``claim``.
# ---------------------------------------------------------------------------


def _claim_worker(
    db_path_text: str,
    request_payload: dict[str, Any],
    started: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue,
) -> None:
    """Issue a single ``claim`` and report disposition or LifecycleConflictError."""
    request = LifecycleRequest(**request_payload)
    repo = SQLiteExtensionLifecycleRepository(Path(db_path_text))
    try:
        started.set()
        if not release.wait(timeout=PROCESS_HARD_DEADLINE_SECONDS):
            result.put(("timeout", None))
            return
        try:
            claim = repo.claim(request)
        except LifecycleConflictError as error:
            result.put(("conflict", str(error)))
            return
        result.put(
            (
                "admitted" if claim.disposition is ClaimDisposition.ADMITTED else "in_progress",
                {
                    "operation_id": claim.operation.request.operation_id
                    if claim.operation is not None
                    else None,
                    "phase": claim.operation.phase.value if claim.operation is not None else None,
                    "disposition": claim.disposition.value,
                },
            )
        )
    finally:
        try:
            result.put(("done", None))
        except (BrokenPipeError, OSError):
            pass


def _hold_claim_worker(
    db_path_text: str,
    request_payload: dict[str, Any],
    started: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue,
) -> None:
    """Claim then park until ``release`` so another process can race the same key."""
    request = LifecycleRequest(**request_payload)
    repo = SQLiteExtensionLifecycleRepository(Path(db_path_text))
    try:
        repo.claim(request)
        started.set()
        if not release.wait(timeout=PROCESS_HARD_DEADLINE_SECONDS):
            result.put(("timeout", None))
            return
        result.put(("claim-held", request.operation_id))
    finally:
        try:
            result.put(("done", None))
        except (BrokenPipeError, OSError):
            pass


def _retry_claim_worker(
    db_path_text: str,
    request_payload: dict[str, Any],
    started: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue,
) -> None:
    """Wait for ``release`` and then retry the exact same request."""
    request = LifecycleRequest(**request_payload)
    repo = SQLiteExtensionLifecycleRepository(Path(db_path_text))
    try:
        started.set()
        if not release.wait(timeout=PROCESS_HARD_DEADLINE_SECONDS):
            result.put(("timeout", None))
            return
        claim = repo.claim(request)
        receipt = claim.receipt
        result.put(
            (
                "claim",
                {
                    "disposition": claim.disposition.value,
                    "operation_id": claim.operation.request.operation_id
                    if claim.operation is not None
                    else None,
                    "phase": claim.operation.phase.value if claim.operation is not None else None,
                    "outcome": receipt.outcome.value if receipt is not None else None,
                    "record_revision": receipt.record.revision if receipt and receipt.record else None,
                    "record_status": receipt.record.status.value if receipt and receipt.record else None,
                    "data_ref": receipt.record.selected.data_ref if receipt and receipt.record else None,
                },
            )
        )
    finally:
        try:
            result.put(("done", None))
        except (BrokenPipeError, OSError):
            pass


def _drive_settle_worker(
    db_path_text: str,
    request_payload: dict[str, Any],
    started: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue,
) -> None:
    """Claim -> advance -> switch -> settle in a fresh process and report outcome."""
    request = LifecycleRequest(**request_payload)
    repo = SQLiteExtensionLifecycleRepository(Path(db_path_text))
    try:
        started.set()
        if not release.wait(timeout=PROCESS_HARD_DEADLINE_SECONDS):
            result.put(("timeout", None))
            return
        switched = _settle_install(repo, request)
        assert switched.intended_receipt is not None
        result.put(
            (
                "settled",
                {
                    "operation_id": request.operation_id,
                    "phase": switched.phase.value,
                    "outcome": switched.intended_receipt.outcome.value,
                    "record_revision": switched.intended_receipt.record.revision
                    if switched.intended_receipt.record
                    else None,
                },
            )
        )
    finally:
        try:
            result.put(("done", None))
        except (BrokenPipeError, OSError):
            pass


def _cleanup_probe_worker(
    exit_code: int,
    started: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue,
) -> None:
    """Emit a valid final marker, then exit with the requested status."""
    started.set()
    if not release.wait(timeout=PROCESS_HARD_DEADLINE_SECONDS):
        raise SystemExit(2)
    result.put(("done", None))
    raise SystemExit(exit_code)


# ---------------------------------------------------------------------------
# Test case
# ---------------------------------------------------------------------------


class SQLiteExtensionLifecycleProcessTests(unittest.TestCase):
    """Two-process claim exclusion and exact replay durability."""

    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self._db_path = Path(self._temp_dir.name) / "extension-lifecycle.sqlite3"
        self._processes: list[multiprocessing.Process] = []
        self._started_events: list[multiprocessing.synchronize.Event] = []
        self._release_events: list[multiprocessing.synchronize.Event] = []
        self._queues: list[multiprocessing.queues.Queue] = []
        self._cleanup_checked_indices: set[int] = set()

    def tearDown(self) -> None:
        cleanup_errors: list[str] = []
        try:
            try:
                cleanup_errors.extend(self._cleanup_processes())
            except Exception as error:
                cleanup_errors.append(f"process cleanup failed unexpectedly: {error!r}")
        finally:
            try:
                try:
                    cleanup_errors.extend(self._close_queues())
                except Exception as error:
                    cleanup_errors.append(
                        f"queue cleanup failed unexpectedly: {error!r}"
                    )
            finally:
                try:
                    self._temp_dir.cleanup()
                except Exception as error:
                    cleanup_errors.append(
                        f"temporary directory cleanup failed: {error!r}"
                    )
        self._raise_cleanup_errors(cleanup_errors)

    # ----- fixture helpers -------------------------------------------------

    def _start_process(
        self,
        target: Any,
        args: tuple[Any, ...],
    ) -> tuple[multiprocessing.Process, multiprocessing.synchronize.Event, multiprocessing.synchronize.Event, multiprocessing.queues.Queue]:
        started: multiprocessing.synchronize.Event = multiprocessing.Event()
        release: multiprocessing.synchronize.Event = multiprocessing.Event()
        result: multiprocessing.queues.Queue = multiprocessing.Queue()
        process = multiprocessing.Process(target=target, args=(*args, started, release, result), daemon=True)
        process.start()
        self._processes.append(process)
        self._started_events.append(started)
        self._release_events.append(release)
        self._queues.append(result)
        return process, started, release, result

    def _cleanup_processes(self) -> list[str]:
        """Release, check once, and reap every worker without stopping early."""
        errors: list[str] = []
        for idx, release in enumerate(self._release_events):
            if idx in self._cleanup_checked_indices:
                continue
            try:
                release.set()
            except Exception as error:
                errors.append(f"worker #{idx} release failed: {error!r}")

        for idx in range(len(self._processes)):
            if idx in self._cleanup_checked_indices:
                continue
            self._cleanup_checked_indices.add(idx)
            process = self._processes[idx]
            queue = self._queues[idx]
            try:
                message = queue.get(timeout=PROCESS_QUEUE_TIMEOUT_SECONDS)
            except Exception as error:
                errors.append(
                    f"worker #{idx} did not emit 'done' before deadline: {error!r}"
                )
            else:
                if message != ("done", None):
                    errors.append(
                        f"worker #{idx} final marker was not 'done': {message!r}"
                    )

            try:
                process.join(timeout=5.0)
            except Exception as error:
                errors.append(f"worker #{idx} initial join failed: {error!r}")
            try:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2.0)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1.0)
                if process.is_alive():
                    errors.append(f"worker #{idx} remained alive after kill")
                elif process.exitcode != 0:
                    errors.append(
                        f"worker #{idx} did not exit cleanly: "
                        f"exitcode={process.exitcode!r}"
                    )
            except Exception as error:
                errors.append(f"worker #{idx} reap failed: {error!r}")

        return errors

    def _close_queues(self) -> list[str]:
        errors: list[str] = []
        for queue in self._queues:
            try:
                queue.close()
            except Exception as error:
                errors.append(f"worker queue close failed: {error!r}")
        self._processes.clear()
        self._started_events.clear()
        self._release_events.clear()
        self._queues.clear()
        return errors

    def _raise_cleanup_errors(self, errors: list[str]) -> None:
        if errors:
            self.fail("process cleanup failed:\n- " + "\n- ".join(errors))

    def _wait_for_started(
        self, events: list[multiprocessing.synchronize.Event]
    ) -> None:
        deadline = min(PROCESS_HARD_DEADLINE_SECONDS, PROCESS_STARTUP_TIMEOUT_SECONDS)
        for index, event in enumerate(events):
            if not event.wait(timeout=deadline):
                self.fail(f"child process #{index} did not announce startup in time")

    def _release_all(self) -> None:
        for release in self._release_events:
            try:
                release.set()
            except (BrokenPipeError, OSError):
                pass

    def _collect_result(
        self,
        queue: multiprocessing.queues.Queue,
        worker_label: str,
    ) -> tuple[str, Any]:
        try:
            return queue.get(timeout=PROCESS_QUEUE_TIMEOUT_SECONDS)
        except Exception as error:
            self.fail(f"{worker_label} did not report before deadline: {error!r}")

    def _drain_done(self) -> None:
        """Check each new final marker and reap all new workers before failing."""
        self._raise_cleanup_errors(self._cleanup_processes())

    # ----- tests -----------------------------------------------------------

    def test_cleanup_reaps_later_worker_after_first_worker_exits_badly(self) -> None:
        first, first_started, _, _ = self._start_process(
            _cleanup_probe_worker, (7,)
        )
        second, second_started, _, _ = self._start_process(
            _cleanup_probe_worker, (0,)
        )
        self._wait_for_started([first_started, second_started])

        with self.assertRaisesRegex(
            AssertionError,
            r"worker #0 did not exit cleanly: exitcode=7",
        ):
            self._drain_done()

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(second.exitcode, 0)
        self.assertEqual(self._cleanup_checked_indices, {0, 1})

    def test_distinct_concurrent_install_requests_admit_exactly_one(self) -> None:
        request_a = _request(
            PROCESS_OP_A,
            principal=PROCESS_PRINCIPAL_A,
            idempotency_key=PROCESS_KEY_A,
            digest_character="a",
        )
        request_b = _request(
            PROCESS_OP_B,
            principal=PROCESS_PRINCIPAL_B,
            idempotency_key=PROCESS_KEY_B,
            digest_character="b",
        )

        _, started_a, _, _ = self._start_process(_claim_worker, (str(self._db_path), _payload(request_a)))
        _, started_b, _, _ = self._start_process(_claim_worker, (str(self._db_path), _payload(request_b)))
        self._wait_for_started([started_a, started_b])

        # Release both processes at the same moment so they race into claim().
        self._release_all()

        outcome_a = self._collect_result(self._queues[0], "worker A")
        outcome_b = self._collect_result(self._queues[1], "worker B")
        self._drain_done()

        outcomes = sorted([outcome_a[0], outcome_b[0]])
        self.assertEqual(outcomes, ["admitted", "conflict"])
        admitted = outcome_a if outcome_a[0] == "admitted" else outcome_b
        conflict = outcome_b if outcome_a[0] == "admitted" else outcome_a
        self.assertEqual(admitted[1]["disposition"], ClaimDisposition.ADMITTED.value)
        self.assertEqual(admitted[1]["phase"], LifecyclePhase.CLAIMED.value)
        self.assertEqual(conflict[0], "conflict")

        # Exactly one pending operation is observable from a fresh connection.
        repo = SQLiteExtensionLifecycleRepository(self._db_path)
        pending = repo.recover()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].request.operation_id, admitted[1]["operation_id"])
        self.assertEqual(pending[0].phase, LifecyclePhase.CLAIMED)

    def test_same_concurrent_request_returns_in_progress_for_competitor(self) -> None:
        # Worker A claims and parks in claim so its operation is the only
        # pending claim; worker B submits the same request and must observe
        # IN_PROGRESS without any new mutation.
        request = _request(
            PROCESS_OP_A,
            principal=PROCESS_PRINCIPAL_A,
            idempotency_key=PROCESS_KEY_A,
            digest_character="a",
        )

        _, started_a, release_a, _ = self._start_process(_hold_claim_worker, (str(self._db_path), _payload(request)))
        _, started_b, release_b, _ = self._start_process(_claim_worker, (str(self._db_path), _payload(request)))
        self._wait_for_started([started_a, started_b])

        # Release worker B first so it races into claim() while A still holds.
        release_b.set()
        b_outcome = self._collect_result(self._queues[1], "worker B (same request)")
        # Then release worker A so it can finish cleanly.
        release_a.set()
        a_outcome = self._collect_result(self._queues[0], "worker A (claim holder)")
        self._drain_done()

        self.assertEqual(a_outcome[0], "claim-held")
        self.assertEqual(b_outcome[0], "in_progress")
        self.assertEqual(b_outcome[1]["disposition"], ClaimDisposition.IN_PROGRESS.value)
        self.assertEqual(b_outcome[1]["phase"], LifecyclePhase.CLAIMED.value)
        self.assertEqual(b_outcome[1]["operation_id"], request.operation_id)

        # The repository still exposes exactly one pending operation because
        # IN_PROGRESS did not insert a duplicate row.
        repo = SQLiteExtensionLifecycleRepository(self._db_path)
        pending = repo.recover()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].request.operation_id, request.operation_id)

    def test_exact_replay_returns_original_receipt_after_settlement(self) -> None:
        # Drive the full install in one process, then a fresh process must
        # observe the exact original APPLIED receipt even after restart.
        request = _request(
            PROCESS_OP_REPLAY,
            principal=PROCESS_PRINCIPAL_A,
            idempotency_key=PROCESS_KEY_REPLAY,
            digest_character="c",
        )

        _, started_drive, release_drive, _ = self._start_process(
            _drive_settle_worker, (str(self._db_path), _payload(request))
        )
        self._wait_for_started([started_drive])
        release_drive.set()
        settled_outcome = self._collect_result(self._queues[0], "settle worker")
        self.assertEqual(settled_outcome[0], "settled")
        self.assertEqual(settled_outcome[1]["outcome"], ReceiptOutcome.APPLIED.value)

        # Brand new process retries the EXACT same request and gets REPLAY.
        _, started_replay, release_replay, _ = self._start_process(
            _retry_claim_worker, (str(self._db_path), _payload(request))
        )
        self._wait_for_started([started_replay])
        release_replay.set()
        replay_outcome = self._collect_result(self._queues[1], "replay worker")
        self._drain_done()

        self.assertEqual(replay_outcome[0], "claim")
        self.assertEqual(replay_outcome[1]["disposition"], ClaimDisposition.REPLAY.value)
        self.assertEqual(replay_outcome[1]["outcome"], ReceiptOutcome.APPLIED.value)
        self.assertEqual(replay_outcome[1]["record_status"], ExtensionStatus.INSTALLED.value)
        self.assertEqual(replay_outcome[1]["record_revision"], 1)
        self.assertEqual(replay_outcome[1]["data_ref"], "ref:data.v1")
        self.assertIsNone(replay_outcome[1]["operation_id"])

        # The in-process repository also sees the durable record directly.
        repo = SQLiteExtensionLifecycleRepository(self._db_path)
        record = repo.get(EXTENSION_ID)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.revision, 1)
        self.assertEqual(record.status, ExtensionStatus.INSTALLED)
        self.assertEqual(record.selected.data_ref, "ref:data.v1")
        self.assertEqual(repo.recover(), ())

    def test_in_progress_same_request_remains_pending_then_replays_after_settle(self) -> None:
        # First process holds the claim open; second process observes the
        # pending claim as IN_PROGRESS. After the parent settles from this
        # process, a brand new retry sees the durable receipt.
        request = _request(
            PROCESS_OP_IN_PROGRESS,
            principal=PROCESS_PRINCIPAL_A,
            idempotency_key=PROCESS_KEY_IN_PROGRESS,
            digest_character="d",
        )

        # Phase 1: holder parks in claim; pending retry observes IN_PROGRESS.
        _, started_holder, release_holder, _ = self._start_process(
            _hold_claim_worker, (str(self._db_path), _payload(request))
        )
        _, started_retry_pending, release_retry_pending, _ = self._start_process(
            _retry_claim_worker, (str(self._db_path), _payload(request))
        )
        self._wait_for_started([started_holder, started_retry_pending])
        release_retry_pending.set()
        pending_outcome = self._collect_result(
            self._queues[1], "pending retry worker"
        )

        self.assertEqual(pending_outcome[0], "claim")
        self.assertEqual(
            pending_outcome[1]["disposition"], ClaimDisposition.IN_PROGRESS.value
        )
        self.assertEqual(
            pending_outcome[1]["phase"], LifecyclePhase.CLAIMED.value
        )

        repo = SQLiteExtensionLifecycleRepository(self._db_path)
        pending = repo.recover()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].request.operation_id, request.operation_id)
        self.assertEqual(pending[0].phase, LifecyclePhase.CLAIMED)

        # Phase 2: release holder so it exits; settle the claim in the parent.
        release_holder.set()
        holder_outcome = self._collect_result(self._queues[0], "holder worker")
        self.assertEqual(holder_outcome[0], "claim-held")
        _settle_install(repo, request)

        # Phase 3: brand-new retry observes REPLAY with the durable receipt.
        _, started_replay, release_replay, _ = self._start_process(
            _retry_claim_worker, (str(self._db_path), _payload(request))
        )
        self._wait_for_started([started_replay])
        release_replay.set()
        replay_outcome = self._collect_result(
            self._queues[2], "post-settle replay worker"
        )

        self.assertEqual(replay_outcome[0], "claim")
        self.assertEqual(replay_outcome[1]["disposition"], ClaimDisposition.REPLAY.value)
        self.assertEqual(replay_outcome[1]["outcome"], ReceiptOutcome.APPLIED.value)
        self.assertEqual(replay_outcome[1]["record_status"], ExtensionStatus.INSTALLED.value)
        self.assertEqual(replay_outcome[1]["record_revision"], 1)

        # Final cleanup: drain every queued worker (asserts clean exit + done markers).
        self._drain_done()


if __name__ == "__main__":
    unittest.main()
