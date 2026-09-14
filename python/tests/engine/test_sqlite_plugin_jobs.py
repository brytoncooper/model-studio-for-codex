"""B19 job STATE slice: real SQLite behavior."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.engine.jobs.ports import (
    ClaimJobCommand,
    CompleteJobCommand,
    ConfirmCancelCommand,
    CreateJobCommand,
    FailJobCommand,
    GetJobCommand,
    JobCheckpointConflictError,
    JobCheckpointValidationError,
    JobNotFoundError,
    JobOwner,
    JobOwnershipMismatchError,
    JobState,
    JobStateConflictError,
    JobTerminalConflictError,
    ReportProgressCommand,
    RequestCancelCommand,
    SaveCheckpointCommand,
)

OWNER = JobOwner(plugin_id="com.example.jobs", activation_id="act-1")
OTHER = JobOwner(plugin_id="com.example.jobs", activation_id="act-2")


def strict_validator(schema_id, value):
    if schema_id is None:
        return
    if schema_id == "ckpt.v1":
        if not isinstance(value, dict):
            raise JobCheckpointValidationError("must be object")
        return
    raise JobCheckpointValidationError(f"unknown checkpoint schema: {schema_id}")


def make_repo(path: Path, **kwargs):
    kwargs.setdefault("checkpoint_validator", strict_validator)
    return SQLitePluginJobRepository(path, **kwargs)


def create(repo, **kwargs):
    cmd = CreateJobCommand(
        owner=kwargs.get("owner", OWNER),
        invocation_id=kwargs.get("invocation_id", "inv-1"),
        operation_id=kwargs.get("operation_id", "com.example.run"),
        origin_principal_id=kwargs.get("origin_principal_id", "origin"),
        checkpoint_schema_id=kwargs.get("checkpoint_schema_id"),
    )
    return repo.create(cmd)


def test_create_persists_across_restart(tmp_path):
    db = tmp_path / "jobs.sqlite"
    repo = make_repo(db)
    record = create(repo)
    assert record.state is JobState.QUEUED
    repo2 = make_repo(db)
    again = repo2.get(GetJobCommand(job_id=record.job_id))
    assert again.job_id == record.job_id
    assert again.invocation_id == "inv-1"
    assert again.created_at == record.created_at


def test_claim_is_atomic_queued_to_running(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo)
    claimed = repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    assert claimed.state is JobState.RUNNING
    with pytest.raises(JobStateConflictError):
        repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))


def test_progress_monotonic_and_bounded(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo)
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    repo.report_progress(ReportProgressCommand(job_id=record.job_id, owner=OWNER, progress=0.5))
    with pytest.raises(JobStateConflictError):
        repo.report_progress(ReportProgressCommand(job_id=record.job_id, owner=OWNER, progress=0.2))
    with pytest.raises(ValueError):
        repo.report_progress(ReportProgressCommand(job_id=record.job_id, owner=OWNER, progress=1.5))


def test_terminal_once_and_immutable(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo)
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    done = repo.complete(CompleteJobCommand(job_id=record.job_id, owner=OWNER))
    assert done.state is JobState.COMPLETED
    with pytest.raises(JobTerminalConflictError):
        repo.fail(FailJobCommand(job_id=record.job_id, owner=OWNER, failure_code="internal"))
    with pytest.raises(JobTerminalConflictError):
        repo.report_progress(ReportProgressCommand(job_id=record.job_id, owner=OWNER, progress=1.0))


def test_concurrent_terminal_race_single_winner(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo)
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    outcomes: list[str] = []

    def complete():
        try:
            make_repo(tmp_path / "j.sqlite").complete(
                CompleteJobCommand(job_id=record.job_id, owner=OWNER)
            )
            outcomes.append("completed")
        except JobTerminalConflictError:
            outcomes.append("conflict")

    def fail():
        try:
            make_repo(tmp_path / "j.sqlite").fail(
                FailJobCommand(job_id=record.job_id, owner=OWNER, failure_code="internal")
            )
            outcomes.append("failed")
        except JobTerminalConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=complete), threading.Thread(target=fail)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) in (["completed", "conflict"], ["conflict", "failed"])
    final = repo.get(GetJobCommand(job_id=record.job_id))
    assert final.state in (JobState.COMPLETED, JobState.FAILED)


def test_cancel_request_separate_from_confirm(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo)
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    requested = repo.request_cancel(RequestCancelCommand(job_id=record.job_id, owner=OWNER))
    assert requested.cancel_requested is True
    assert requested.state is JobState.RUNNING
    confirmed = repo.confirm_cancel(ConfirmCancelCommand(job_id=record.job_id, owner=OWNER))
    assert confirmed.state is JobState.CANCELLED


def test_wrong_owner_rejected_on_mutation(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo)
    with pytest.raises(JobOwnershipMismatchError):
        repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OTHER))
    with pytest.raises(JobOwnershipMismatchError):
        repo.request_cancel(RequestCancelCommand(job_id=record.job_id, owner=OTHER))
    assert repo.get(GetJobCommand(job_id=record.job_id)).state is JobState.QUEUED


def test_unknown_job_not_found(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    with pytest.raises(JobNotFoundError):
        repo.get(GetJobCommand(job_id="missing"))


def test_crash_interrupts_queued_and_running_once(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    queued = create(repo, invocation_id="a")
    running = create(repo, invocation_id="b")
    repo.claim(ClaimJobCommand(job_id=running.job_id, owner=OWNER))
    other = create(repo, owner=OTHER, invocation_id="c")
    result = repo.mark_worker_crashed(OWNER)
    assert set(result.interrupted_job_ids) == {queued.job_id, running.job_id}
    assert repo.get(GetJobCommand(job_id=queued.job_id)).state is JobState.INTERRUPTED
    assert repo.get(GetJobCommand(job_id=other.job_id)).state is JobState.QUEUED
    again = repo.mark_worker_crashed(OWNER)
    assert again.interrupted_job_ids == ()


def test_crash_interrupts_resumable_with_checkpoint_retained(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo, checkpoint_schema_id="ckpt.v1")
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    saved = repo.save_checkpoint(
        SaveCheckpointCommand(
            job_id=record.job_id, owner=OWNER, checkpoint={"step": 3}, expected_revision=0
        )
    )
    assert saved.checkpoint_revision == 1
    repo.mark_worker_crashed(OWNER)
    final = repo.get(GetJobCommand(job_id=record.job_id))
    assert final.state is JobState.INTERRUPTED
    assert final.checkpoint_revision == 1
    assert json.loads(final.checkpoint_json or "{}") == {"step": 3}


def test_checkpoint_cas_and_schema_rollback(tmp_path):
    def validator(schema_id, value):
        if schema_id == "ckpt.v1" and not isinstance(value, dict):
            raise JobCheckpointValidationError("must be object")

    repo = make_repo(tmp_path / "j.sqlite", checkpoint_validator=validator)
    record = create(repo, checkpoint_schema_id="ckpt.v1")
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    repo.save_checkpoint(
        SaveCheckpointCommand(
            job_id=record.job_id, owner=OWNER, checkpoint={"step": 1}, expected_revision=0
        )
    )
    with pytest.raises(JobCheckpointConflictError):
        repo.save_checkpoint(
            SaveCheckpointCommand(
                job_id=record.job_id, owner=OWNER, checkpoint={"step": 2}, expected_revision=0
            )
        )
    with pytest.raises(JobCheckpointValidationError):
        repo.save_checkpoint(
            SaveCheckpointCommand(
                job_id=record.job_id, owner=OWNER, checkpoint=[1, 2], expected_revision=1
            )
        )
    final = repo.get(GetJobCommand(job_id=record.job_id))
    assert final.checkpoint_revision == 1
    assert json.loads(final.checkpoint_json or "{}") == {"step": 1}


def test_checkpoint_rejects_nan_and_oversize(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo)
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    with pytest.raises(JobCheckpointValidationError):
        repo.save_checkpoint(
            SaveCheckpointCommand(
                job_id=record.job_id, owner=OWNER, checkpoint=float("nan"), expected_revision=0
            )
        )
    with pytest.raises(JobCheckpointValidationError):
        repo.save_checkpoint(
            SaveCheckpointCommand(
                job_id=record.job_id,
                owner=OWNER,
                checkpoint="x" * (1_048_576 + 1),
                expected_revision=0,
            )
        )


def test_fail_stores_code_only(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo)
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    failed = repo.fail(
        FailJobCommand(job_id=record.job_id, owner=OWNER, failure_code="deadline_exceeded")
    )
    assert failed.state is JobState.FAILED
    assert failed.failure_code == "deadline_exceeded"
    assert failed.checkpoint_json is None


def test_fail_rejects_unknown_code(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo)
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    with pytest.raises(ValueError):
        repo.fail(
            FailJobCommand(job_id=record.job_id, owner=OWNER, failure_code="weird-code")
        )
    assert repo.get(GetJobCommand(job_id=record.job_id)).state is JobState.RUNNING


def test_checkpoint_rejects_non_strict_types(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo)
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    with pytest.raises(JobCheckpointValidationError):
        repo.save_checkpoint(
            SaveCheckpointCommand(
                job_id=record.job_id, owner=OWNER, checkpoint=("a", "b"),
                expected_revision=0,
            )
        )
    with pytest.raises(JobCheckpointValidationError):
        repo.save_checkpoint(
            SaveCheckpointCommand(
                job_id=record.job_id, owner=OWNER, checkpoint={1: "one"},
                expected_revision=0,
            )
        )
    final = repo.get(GetJobCommand(job_id=record.job_id))
    assert final.checkpoint_revision == 0
    assert final.checkpoint_json is None


def test_checkpoint_rejects_bool_revision(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo)
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    with pytest.raises(ValueError):
        repo.save_checkpoint(
            SaveCheckpointCommand(
                job_id=record.job_id, owner=OWNER, checkpoint={"step": 1},
                expected_revision=True,
            )
        )


def test_checkpoint_schema_override_rejected(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo, checkpoint_schema_id="ckpt.v1")
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    with pytest.raises(JobCheckpointValidationError):
        repo.save_checkpoint(
            SaveCheckpointCommand(
                job_id=record.job_id, owner=OWNER, checkpoint={"step": 1},
                expected_revision=0, schema_id="other.v2",
            )
        )
    final = repo.get(GetJobCommand(job_id=record.job_id))
    assert final.checkpoint_revision == 0
    assert final.checkpoint_schema_id == "ckpt.v1"


def test_checkpoint_unknown_schema_rejected(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo, checkpoint_schema_id="unknown.v9")
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    with pytest.raises(JobCheckpointValidationError):
        repo.save_checkpoint(
            SaveCheckpointCommand(
                job_id=record.job_id, owner=OWNER, checkpoint={"step": 1},
                expected_revision=0,
            )
        )
    final = repo.get(GetJobCommand(job_id=record.job_id))
    assert final.checkpoint_revision == 0

def test_migration_adds_origin_principal_id_and_output_json(tmp_path):
    """A pre-existing schema missing the new columns is migrated in place."""
    db = tmp_path / "legacy.sqlite"
    import sqlite3
    # Hand-rolled legacy schema: missing origin_principal_id and output_json.
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE plugin_jobs (
            job_id TEXT PRIMARY KEY,
            plugin_id TEXT NOT NULL,
            activation_id TEXT NOT NULL,
            invocation_id TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            state TEXT NOT NULL,
            progress REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            checkpoint_revision INTEGER NOT NULL DEFAULT 0,
            checkpoint_schema_id TEXT,
            checkpoint_json TEXT,
            failure_code TEXT
        );
        """
    )
    # Seed a legacy row WITHOUT origin_principal_id (column absent).
    conn.execute(
        "INSERT INTO plugin_jobs (job_id, plugin_id, activation_id, invocation_id, operation_id, state, progress, created_at, cancel_requested, checkpoint_revision)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
        ("legacy-1", "com.example.jobs", "act-1", "inv-legacy", "com.example.run", "completed", 1.0, "2026-09-12T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    repo = make_repo(db)
    # Migration: column added; legacy row reads back with empty origin.
    again = repo.get(GetJobCommand(job_id="legacy-1"))
    assert again.job_id == "legacy-1"
    assert again.origin_principal_id == ""
    # Empty origin means public read rejects.
    from model_deck.engine.jobs.ports import GetPublicCommand, JobOriginMismatchError
    with pytest.raises(JobOriginMismatchError):
        repo.get_public(GetPublicCommand(job_id="legacy-1", caller_principal_id="any"))


def test_complete_persists_output_atomically(tmp_path):
    """complete() persists output only with successful COMPLETED transition."""
    from model_deck.engine.jobs.ports import (
        ClaimJobCommand, CompleteJobCommand, JobOwner,
    )

    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo)
    # claim first.
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    done = repo.complete(
        CompleteJobCommand(
            job_id=record.job_id,
            owner=OWNER,
            output={"media_type": "text/markdown", "content": "# hi"},
            output_present=True,
        )
    )
    assert done.state is JobState.COMPLETED
    assert done.output_present is True
    assert done.output == {"media_type": "text/markdown", "content": "# hi"}

    # Public read surfaces the output for the originating caller.
    from model_deck.engine.jobs.ports import GetPublicCommand
    view = repo.get_public(GetPublicCommand(job_id=record.job_id, caller_principal_id="origin"))
    assert view.state is JobState.COMPLETED
    assert view.output_present is True
    assert view.output == {"media_type": "text/markdown", "content": "# hi"}


def test_no_output_on_failed_cancelled_interrupted(tmp_path):
    """FAILED/CANCELLED/INTERRUPTED never persist output, even if asked."""
    from model_deck.engine.jobs.ports import (
        ClaimJobCommand, CompleteJobCommand, ConfirmCancelCommand,
        FailJobCommand, WorkerCrashResult, JobOwner,
    )

    repo = make_repo(tmp_path / "j.sqlite")

    # FAILED: complete must reject output_present on non-completed outcomes
    # (the wire enforces this; the adapter itself is silent for fail()).
    record_a = create(repo, invocation_id="a")
    repo.claim(ClaimJobCommand(job_id=record_a.job_id, owner=OWNER))
    failed = repo.fail(FailJobCommand(job_id=record_a.job_id, owner=OWNER, failure_code="internal"))
    assert failed.state.value == "failed"
    assert failed.output_present is False
    assert failed.output is None

    # CANCELLED: confirm_cancel never accepts output.
    record_b = create(repo, invocation_id="b")
    repo.claim(ClaimJobCommand(job_id=record_b.job_id, owner=OWNER))
    cancelled = repo.confirm_cancel(ConfirmCancelCommand(job_id=record_b.job_id, owner=OWNER))
    assert cancelled.state.value == "cancelled"
    assert cancelled.output_present is False

    # INTERRUPTED: mark_worker_crashed transitions active rows to INTERRUPTED
    # without ever accepting output.
    record_c = create(repo, invocation_id="c")
    repo.claim(ClaimJobCommand(job_id=record_c.job_id, owner=OWNER))
    repo.mark_worker_crashed(OWNER)
    again = repo.get(GetJobCommand(job_id=record_c.job_id))
    assert again.state.value == "interrupted"
    assert again.output_present is False


def test_public_read_origin_mismatch_raises(tmp_path):
    from model_deck.engine.jobs.ports import ClaimJobCommand, GetPublicCommand, JobOriginMismatchError
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo, origin_principal_id="alice")
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    # Wrong caller
    with pytest.raises(JobOriginMismatchError):
        repo.get_public(GetPublicCommand(job_id=record.job_id, caller_principal_id="bob"))
    # Right caller
    view = repo.get_public(GetPublicCommand(job_id=record.job_id, caller_principal_id="alice"))
    assert view.job_id == record.job_id


def test_request_cancel_public_idempotent_active_false_terminal(tmp_path):
    from model_deck.engine.jobs.ports import (
        ClaimJobCommand, CompleteJobCommand, RequestCancelPublicCommand,
    )
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo, origin_principal_id="alice")
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    # First request against active job
    assert (
        repo.request_cancel_public(
            RequestCancelPublicCommand(
                job_id=record.job_id,
                caller_principal_id="alice",
                idempotency_key="cancel-1",
            )
        )
        is True
    )
    # Repeat is idempotent
    assert (
        repo.request_cancel_public(
            RequestCancelPublicCommand(
                job_id=record.job_id,
                caller_principal_id="alice",
                idempotency_key="cancel-1",
            )
        )
        is True
    )
    # An exact replay returns its original result even after terminalization.
    repo.complete(CompleteJobCommand(job_id=record.job_id, owner=OWNER))
    assert (
        repo.request_cancel_public(
            RequestCancelPublicCommand(
                job_id=record.job_id,
                caller_principal_id="alice",
                idempotency_key="cancel-1",
            )
        )
        is True
    )
    # A new cancellation request observes the already-terminal state.
    assert (
        repo.request_cancel_public(
            RequestCancelPublicCommand(
                job_id=record.job_id,
                caller_principal_id="alice",
                idempotency_key="cancel-2",
            )
        )
        is False
    )
