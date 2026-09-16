"""B19 job STATE slice: real SQLite behavior."""
from __future__ import annotations

import json
import sqlite3
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
    GetPublicCommand,
    JobNotFoundError,
    JobOriginMismatchError,
    JobOwner,
    JobOwnershipMismatchError,
    JobResumeConflictError,
    JobResumeUnsupportedError,
    JobState,
    JobStateConflictError,
    JobTerminalConflictError,
    ReportProgressCommand,
    RequestCancelCommand,
    ResumeJobCommand,
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


# --- Explicit resume ---------------------------------------------------------
# A job is resumable only because its manifest said so at create time, and only
# an interrupted resumable job can be handed back to a worker. Everything here
# goes through the real SQLite adapter, including across a reopened database.


def interrupt(repo, record):
    """Reach INTERRUPTED the only way production does: the worker is lost."""
    repo.mark_worker_crashed(OWNER)
    return repo.get(GetJobCommand(job_id=record.job_id))


def resume(repo, record, *, key="resume-1", principal="origin", **overrides):
    command = ResumeJobCommand(
        job_id=overrides.get("job_id", record.job_id),
        caller_principal_id=principal,
        idempotency_key=key,
        activation_id=overrides.get("activation_id"),
        invocation_id=overrides.get("invocation_id"),
    )
    return repo.begin_resume(command)


def test_create_persists_the_resumable_flag(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    assert create(repo).resumable is False
    resumable = repo.create(
        CreateJobCommand(
            owner=OWNER,
            invocation_id="inv-2",
            operation_id="com.example.run",
            origin_principal_id="origin",
            resumable=True,
        )
    )
    assert resumable.resumable is True
    assert resumable.resume_count == 0
    # It survives a reopen: the flag is durable, not an in-memory decision.
    reopened = make_repo(tmp_path / "j.sqlite")
    assert reopened.get(GetJobCommand(job_id=resumable.job_id)).resumable is True


def test_read_checkpoint_returns_none_until_one_is_saved(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo, checkpoint_schema_id="ckpt.v1")
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    assert repo.read_checkpoint(OWNER, record.job_id) is None
    repo.save_checkpoint(
        SaveCheckpointCommand(
            job_id=record.job_id,
            owner=OWNER,
            checkpoint={"done": 2},
            expected_revision=0,
            schema_id="ckpt.v1",
        )
    )
    revision, schema_id, encoded = repo.read_checkpoint(OWNER, record.job_id)
    assert revision == 1
    assert schema_id == "ckpt.v1"
    # The third position is the stored encoding, not a decoded value.
    assert json.loads(encoded) == {"done": 2}


def test_read_checkpoint_refuses_unknown_job_and_other_owner(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo)
    with pytest.raises(JobNotFoundError):
        repo.read_checkpoint(OWNER, "00000000-0000-4000-8000-000000000000")
    with pytest.raises(JobOwnershipMismatchError):
        repo.read_checkpoint(OTHER, record.job_id)


def make_interrupted_resumable(tmp_path, *, checkpoint=True, name="j.sqlite"):
    repo = make_repo(tmp_path / name)
    record = repo.create(
        CreateJobCommand(
            owner=OWNER,
            invocation_id="inv-1",
            operation_id="com.example.run",
            origin_principal_id="origin",
            checkpoint_schema_id="ckpt.v1",
            resumable=True,
        )
    )
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    repo.report_progress(
        ReportProgressCommand(job_id=record.job_id, owner=OWNER, progress=0.5)
    )
    if checkpoint:
        repo.save_checkpoint(
            SaveCheckpointCommand(
                job_id=record.job_id,
                owner=OWNER,
                checkpoint={"next_index": 7},
                expected_revision=0,
                schema_id="ckpt.v1",
            )
        )
    return repo, interrupt(repo, record)


def test_begin_resume_returns_to_running_and_rebinds_the_activation(tmp_path):
    repo, record = make_interrupted_resumable(tmp_path)
    assert record.state is JobState.INTERRUPTED

    resumed = resume(repo, record, activation_id="act-9", invocation_id="inv-9")

    assert resumed.state is JobState.RUNNING
    assert resumed.resume_count == 1
    # The owning plugin is preserved; only the dead activation is replaced.
    assert resumed.plugin_id == OWNER.plugin_id
    assert resumed.activation_id == "act-9"
    assert resumed.invocation_id == "inv-9"
    # Nothing is replayed: progress and checkpoint survive untouched.
    assert resumed.progress == 0.5
    assert resumed.checkpoint_revision == 1
    assert json.loads(resumed.checkpoint_json) == {"next_index": 7}


def test_begin_resume_keeps_the_recorded_activation_when_none_is_supplied(tmp_path):
    repo, record = make_interrupted_resumable(tmp_path)
    resumed = resume(repo, record)
    assert resumed.activation_id == OWNER.activation_id
    assert resumed.invocation_id == "inv-1"


def test_begin_resume_refuses_a_job_that_never_declared_itself_resumable(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = create(repo)
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    interrupted = interrupt(repo, record)
    with pytest.raises(JobResumeUnsupportedError):
        resume(repo, interrupted)


def test_begin_resume_refuses_a_job_that_is_not_interrupted(tmp_path):
    repo = make_repo(tmp_path / "j.sqlite")
    record = repo.create(
        CreateJobCommand(
            owner=OWNER,
            invocation_id="inv-1",
            operation_id="com.example.run",
            origin_principal_id="origin",
            resumable=True,
        )
    )
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    with pytest.raises(JobResumeUnsupportedError):
        resume(repo, record)


def test_begin_resume_requires_the_originating_principal(tmp_path):
    repo, record = make_interrupted_resumable(tmp_path)
    with pytest.raises(JobOriginMismatchError):
        resume(repo, record, principal="mallory")
    assert repo.get(GetJobCommand(job_id=record.job_id)).state is JobState.INTERRUPTED


def test_begin_resume_replays_the_same_key_and_conflicts_on_another_job(tmp_path):
    repo, record = make_interrupted_resumable(tmp_path)
    first = resume(repo, record, key="resume-1")
    assert first.resume_count == 1

    replay = resume(repo, record, key="resume-1")
    # The replay answers with the stored row; it does not resume twice.
    assert replay.resume_count == 1

    other = repo.create(
        CreateJobCommand(
            owner=OWNER,
            invocation_id="inv-2",
            operation_id="com.example.run",
            origin_principal_id="origin",
            resumable=True,
        )
    )
    with pytest.raises(JobResumeConflictError):
        resume(repo, other, key="resume-1")


def test_begin_resume_conflicts_when_a_fresh_key_targets_a_resumed_job(tmp_path):
    repo, record = make_interrupted_resumable(tmp_path)
    resume(repo, record, key="resume-1")
    with pytest.raises(JobResumeConflictError):
        resume(repo, record, key="resume-2")


def test_resume_receipt_records_the_revision_it_resumed_from(tmp_path):
    repo, record = make_interrupted_resumable(tmp_path)
    assert repo.read_resume_receipt("origin", "resume-1") is None
    resume(repo, record, key="resume-1")
    assert repo.read_resume_receipt("origin", "resume-1") == (record.job_id, 1)
    # A key that belongs to someone else is not visible.
    assert repo.read_resume_receipt("mallory", "resume-1") is None


def test_resume_receipt_revision_is_none_without_a_checkpoint(tmp_path):
    repo, record = make_interrupted_resumable(tmp_path, checkpoint=False)
    resume(repo, record, key="resume-1")
    assert repo.read_resume_receipt("origin", "resume-1") == (record.job_id, None)


def test_resume_state_survives_reopening_the_database(tmp_path):
    repo, record = make_interrupted_resumable(tmp_path)
    resume(repo, record, key="resume-1", activation_id="act-9")
    reopened = make_repo(tmp_path / "j.sqlite")
    stored = reopened.get(GetJobCommand(job_id=record.job_id))
    assert stored.state is JobState.RUNNING
    assert stored.resume_count == 1
    assert stored.activation_id == "act-9"
    assert reopened.read_resume_receipt("origin", "resume-1") == (record.job_id, 1)


def test_public_view_carries_resumable_and_resume_count(tmp_path):
    repo, record = make_interrupted_resumable(tmp_path)
    view = repo.get_public(
        GetPublicCommand(job_id=record.job_id, caller_principal_id="origin")
    )
    assert view.resumable is True
    assert view.resume_count == 0
    resume(repo, record, key="resume-1")
    after = repo.get_public(
        GetPublicCommand(job_id=record.job_id, caller_principal_id="origin")
    )
    assert after.resume_count == 1


def test_existing_database_without_resume_columns_migrates(tmp_path):
    """A database written before explicit resume stays readable, non-resumable."""
    db = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(str(db))
    connection.executescript(
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
            origin_principal_id TEXT NOT NULL DEFAULT '',
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            checkpoint_revision INTEGER NOT NULL DEFAULT 0,
            checkpoint_schema_id TEXT,
            checkpoint_json TEXT,
            output_json TEXT,
            failure_code TEXT
        );
        INSERT INTO plugin_jobs VALUES (
            '11111111-1111-4111-8111-111111111111', 'com.example.jobs', 'act-1',
            'inv-1', 'com.example.run', 'interrupted', 0.25, '2026-01-01T00:00:00Z',
            'origin', 0, 0, NULL, NULL, NULL, NULL
        );
        """
    )
    connection.commit()
    connection.close()

    repo = make_repo(db)
    legacy = repo.get(GetJobCommand(job_id="11111111-1111-4111-8111-111111111111"))
    assert legacy.resumable is False
    assert legacy.resume_count == 0
    with pytest.raises(JobResumeUnsupportedError):
        repo.begin_resume(
            ResumeJobCommand(
                job_id=legacy.job_id,
                caller_principal_id="origin",
                idempotency_key="resume-legacy",
            )
        )


# --- Taking a failed resume back ---------------------------------------------
# begin_resume has to commit before the worker is touched, so an invocation that
# never lands leaves a RUNNING row nobody is working on. No activation died, so
# worker-loss recovery never fires. rollback_resume is the only thing that
# undoes it.


def test_rollback_resume_returns_the_job_to_interrupted_and_frees_the_key(tmp_path):
    repo, record = make_interrupted_resumable(tmp_path)
    resume(repo, record, key="resume-1", activation_id="act-9")

    rolled = repo.rollback_resume(record.job_id, "origin", "resume-1")

    assert rolled.state is JobState.INTERRUPTED
    assert rolled.resume_count == 0
    assert repo.read_resume_receipt("origin", "resume-1") is None
    # Nothing about the work itself was touched.
    assert rolled.progress == 0.5
    assert rolled.checkpoint_revision == 1


def test_a_rolled_back_job_can_be_resumed_again(tmp_path):
    repo, record = make_interrupted_resumable(tmp_path)
    resume(repo, record, key="resume-1")
    repo.rollback_resume(record.job_id, "origin", "resume-1")

    again = resume(repo, record, key="resume-2")
    assert again.state is JobState.RUNNING
    assert again.resume_count == 1


def test_rollback_resume_leaves_a_job_that_already_settled_alone(tmp_path):
    """The worker did get the invocation; its outcome outranks the error."""
    repo, record = make_interrupted_resumable(tmp_path)
    resume(repo, record, key="resume-1", activation_id="act-9")
    repo.complete(
        CompleteJobCommand(
            job_id=record.job_id,
            owner=JobOwner(plugin_id=OWNER.plugin_id, activation_id="act-9"),
            output={"done": True},
            output_present=True,
        )
    )

    unchanged = repo.rollback_resume(record.job_id, "origin", "resume-1")

    assert unchanged.state is JobState.COMPLETED
    assert unchanged.resume_count == 1
    assert repo.read_resume_receipt("origin", "resume-1") == (record.job_id, 1)


def test_rollback_resume_without_a_resume_to_undo_changes_nothing(tmp_path):
    repo, record = make_interrupted_resumable(tmp_path)
    unchanged = repo.rollback_resume(record.job_id, "origin", "resume-1")
    assert unchanged.state is JobState.INTERRUPTED
    assert unchanged.resume_count == 0


def test_rollback_resume_requires_the_originating_principal(tmp_path):
    repo, record = make_interrupted_resumable(tmp_path)
    resume(repo, record, key="resume-1")
    with pytest.raises(JobOriginMismatchError):
        repo.rollback_resume(record.job_id, "mallory", "resume-1")
    assert repo.get(GetJobCommand(job_id=record.job_id)).state is JobState.RUNNING


def test_rollback_resume_of_an_unknown_job_is_not_found(tmp_path):
    repo, _ = make_interrupted_resumable(tmp_path)
    with pytest.raises(JobNotFoundError):
        repo.rollback_resume(
            "00000000-0000-4000-8000-000000000000", "origin", "resume-1"
        )


def test_rollback_resume_keeps_another_key_receipt(tmp_path):
    """Only the key that failed is freed; another job's receipt is untouched."""
    repo, record = make_interrupted_resumable(tmp_path)
    other = repo.create(
        CreateJobCommand(
            owner=OWNER,
            invocation_id="inv-2",
            operation_id="com.example.run",
            origin_principal_id="origin",
            resumable=True,
        )
    )
    repo.claim(ClaimJobCommand(job_id=other.job_id, owner=OWNER))
    repo.mark_worker_crashed(OWNER)
    resume(repo, other, key="resume-other")
    resume(repo, record, key="resume-1")

    repo.rollback_resume(record.job_id, "origin", "resume-1")

    assert repo.read_resume_receipt("origin", "resume-other") == (other.job_id, None)
    assert repo.get(GetJobCommand(job_id=other.job_id)).state is JobState.RUNNING
