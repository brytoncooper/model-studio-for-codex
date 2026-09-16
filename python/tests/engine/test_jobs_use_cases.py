"""Public jobs.get / jobs.cancel use-case tests (B22)."""
from __future__ import annotations

from pathlib import Path

import pytest

from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.engine.jobs.first_party import (
    # The bound itself is internal; the test reads it so the assertion cannot
    # drift from the value the use case enforces.
    _MAX_TRACKED_KEYS,
    FIRST_PARTY_ORIGIN_PRINCIPAL,
    FIRST_PARTY_PLUGIN_ID,
    FIRST_PARTY_PLUGIN_PREFIX,
    JOB_KIND_BENCHMARKS_REFRESH,
    JOB_KIND_PRICES_REFRESH,
    CreateFirstPartyJobUseCase,
    FirstPartyJobDirectory,
    FirstPartyJobKindError,
    FirstPartyJobOwnerError,
    first_party_owner,
    guard_external_plugin_id,
    is_first_party_plugin_id,
    recover_first_party_jobs,
)
from model_deck.engine.jobs.ports import (
    ClaimJobCommand,
    CompleteJobCommand,
    ConfirmCancelCommand,
    CreateJobCommand,
    FailJobCommand,
    GetJobCommand,
    JobOwner,
    JobResumeConflictError,
    JobResumeUnsupportedError,
    JobState,
    JobTerminalConflictError,
    RequestCancelPublicCommand,
    ResumeInvocationRequest,
    ResumeTarget,
    SaveCheckpointCommand,
)
from model_deck.engine.jobs.use_cases import (
    CancelJobUseCase,
    GetJobUseCase,
    JobsCallerMismatchError,
    JobsInvalidArgumentError,
    JobsIdempotencyConflictError,
    JobsNotFoundError,
    JobsPluginUnavailableError,
    JobsUnknownKeyError,
    ResumeJobUseCase,
)


OWNER = JobOwner(plugin_id="com.example.jobs", activation_id="act-1")


def strict_validator(schema_id, value):
    return None


def make_repo(tmp_path: Path) -> SQLitePluginJobRepository:
    return SQLitePluginJobRepository(
        tmp_path / "jobs.sqlite",
        checkpoint_validator=strict_validator,
    )


def create_with_origin(repo: SQLitePluginJobRepository, *, origin: str = "alice"):
    return repo.create(
        __import__("model_deck.engine.jobs.ports", fromlist=["CreateJobCommand"]).CreateJobCommand(
            owner=OWNER,
            invocation_id="inv-1",
            operation_id="com.example.run",
            origin_principal_id=origin,
        )
    )


def test_get_returns_public_view_with_output(tmp_path):
    repo = make_repo(tmp_path)
    record = create_with_origin(repo)
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    repo.complete(
        CompleteJobCommand(
            job_id=record.job_id,
            owner=OWNER,
            output={"media_type": "text/markdown", "content": "# hi"},
            output_present=True,
        )
    )
    use_case = GetJobUseCase(repo)
    result = use_case.execute({"job_id": record.job_id}, caller_principal_id="alice")
    assert result["state"] == "completed"
    assert result["progress"] == 0.0  # progress is not advanced by terminalize
    assert result["job_id"] == record.job_id
    assert result["output"] == {"media_type": "text/markdown", "content": "# hi"}


def test_get_omits_output_when_absent(tmp_path):
    repo = make_repo(tmp_path)
    record = create_with_origin(repo)
    # No completion → no output, no `output` key in payload.
    use_case = GetJobUseCase(repo)
    result = use_case.execute({"job_id": record.job_id}, caller_principal_id="alice")
    assert "output" not in result
    assert result["state"] in ("running", "queued")  # claim folded → RUNNING


def test_get_origin_mismatch_raises(tmp_path):
    repo = make_repo(tmp_path)
    record = create_with_origin(repo, origin="alice")
    use_case = GetJobUseCase(repo)
    with pytest.raises(JobsCallerMismatchError):
        use_case.execute({"job_id": record.job_id}, caller_principal_id="bob")


def test_get_unknown_job_raises(tmp_path):
    repo = make_repo(tmp_path)
    use_case = GetJobUseCase(repo)
    with pytest.raises(JobsNotFoundError):
        use_case.execute({"job_id": "00000000-0000-4000-8000-000000000000"}, caller_principal_id="alice")


def test_get_unknown_param_key_raises(tmp_path):
    repo = make_repo(tmp_path)
    record = create_with_origin(repo)
    use_case = GetJobUseCase(repo)
    with pytest.raises(JobsUnknownKeyError):
        use_case.execute({"job_id": record.job_id, "unknown": 1}, caller_principal_id="alice")


def test_get_rejects_bad_uuid(tmp_path):
    repo = make_repo(tmp_path)
    use_case = GetJobUseCase(repo)
    with pytest.raises(JobsInvalidArgumentError):
        use_case.execute({"job_id": "not-a-uuid"}, caller_principal_id="alice")


def test_cancel_active_returns_accepted_true(tmp_path):
    repo = make_repo(tmp_path)
    record = create_with_origin(repo)
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    use_case = CancelJobUseCase(repo)
    result = use_case.execute(
        {"job_id": record.job_id, "idempotency_key": "abc"},
        caller_principal_id="alice",
    )
    assert result == {"accepted": True}


def test_cancel_repeat_active_returns_accepted_true(tmp_path):
    repo = make_repo(tmp_path)
    record = create_with_origin(repo)
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    use_case = CancelJobUseCase(repo)
    use_case.execute(
        {"job_id": record.job_id, "idempotency_key": "k1"}, caller_principal_id="alice"
    )
    again = use_case.execute(
        {"job_id": record.job_id, "idempotency_key": "k2"}, caller_principal_id="alice"
    )
    assert again == {"accepted": True}


def test_cancel_idempotency_key_cannot_be_reused_for_another_job(tmp_path):
    repo = make_repo(tmp_path)
    first = create_with_origin(repo, origin="alice")
    second = create_with_origin(repo, origin="alice")
    repo.claim(ClaimJobCommand(job_id=first.job_id, owner=OWNER))
    repo.claim(ClaimJobCommand(job_id=second.job_id, owner=OWNER))
    use_case = CancelJobUseCase(repo)
    use_case.execute(
        {"job_id": first.job_id, "idempotency_key": "same-key"},
        caller_principal_id="alice",
    )

    with pytest.raises(JobsIdempotencyConflictError):
        use_case.execute(
            {"job_id": second.job_id, "idempotency_key": "same-key"},
            caller_principal_id="alice",
        )


def test_cancel_terminal_returns_accepted_false(tmp_path):
    repo = make_repo(tmp_path)
    record = create_with_origin(repo)
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=OWNER))
    repo.complete(CompleteJobCommand(job_id=record.job_id, owner=OWNER))
    use_case = CancelJobUseCase(repo)
    result = use_case.execute(
        {"job_id": record.job_id, "idempotency_key": "k"},
        caller_principal_id="alice",
    )
    assert result == {"accepted": False}


def test_cancel_origin_mismatch_raises(tmp_path):
    repo = make_repo(tmp_path)
    record = create_with_origin(repo, origin="alice")
    use_case = CancelJobUseCase(repo)
    with pytest.raises(JobsCallerMismatchError):
        use_case.execute(
            {"job_id": record.job_id, "idempotency_key": "k"},
            caller_principal_id="bob",
        )


def test_cancel_unknown_job_raises(tmp_path):
    repo = make_repo(tmp_path)
    use_case = CancelJobUseCase(repo)
    with pytest.raises(JobsNotFoundError):
        use_case.execute(
            {"job_id": "00000000-0000-4000-8000-000000000000", "idempotency_key": "k"},
            caller_principal_id="alice",
        )


def test_cancel_requires_idempotency_key(tmp_path):
    repo = make_repo(tmp_path)
    record = create_with_origin(repo)
    use_case = CancelJobUseCase(repo)
    with pytest.raises(JobsInvalidArgumentError):
        use_case.execute({"job_id": record.job_id}, caller_principal_id="alice")


def test_cancel_unknown_param_key_raises(tmp_path):
    repo = make_repo(tmp_path)
    record = create_with_origin(repo)
    use_case = CancelJobUseCase(repo)
    with pytest.raises(JobsUnknownKeyError):
        use_case.execute(
            {"job_id": record.job_id, "idempotency_key": "k", "extra": True},
            caller_principal_id="alice",
        )


def test_get_and_cancel_share_origin_authorization(tmp_path):
    """Cancel from non-origin fails even after a get from origin succeeded."""
    repo = make_repo(tmp_path)
    record = create_with_origin(repo, origin="alice")
    get = GetJobUseCase(repo)
    cancel = CancelJobUseCase(repo)
    get.execute({"job_id": record.job_id}, caller_principal_id="alice")
    with pytest.raises(JobsCallerMismatchError):
        cancel.execute(
            {"job_id": record.job_id, "idempotency_key": "k"},
            caller_principal_id="eve",
        )


# --- First-party (engine-owned) jobs -----------------------------------------
# These rows are created by the engine itself, never by a public operation, so
# they are owned by the reserved namespace and originate from the engine
# principal rather than from any client.

ENGINE_INSTANCE_ID = "11111111-1111-4111-8111-111111111111"


def first_party_use_case(repo: SQLitePluginJobRepository) -> CreateFirstPartyJobUseCase:
    return CreateFirstPartyJobUseCase(repo, owner=first_party_owner(ENGINE_INSTANCE_ID))


def test_first_party_create_is_running_and_owned_by_the_engine(tmp_path):
    repo = make_repo(tmp_path)
    start = first_party_use_case(repo).create(
        JOB_KIND_PRICES_REFRESH, idempotency_key="k"
    )
    assert start.started is True
    record = repo.get(GetJobCommand(job_id=start.job_id))
    # Claim is folded into create, exactly as the plugin broker does it.
    assert record.state is JobState.RUNNING
    assert record.plugin_id == FIRST_PARTY_PLUGIN_ID
    assert record.plugin_id.startswith(FIRST_PARTY_PLUGIN_PREFIX)
    assert record.activation_id == ENGINE_INSTANCE_ID
    assert record.operation_id == JOB_KIND_PRICES_REFRESH
    assert record.origin_principal_id == FIRST_PARTY_ORIGIN_PRINCIPAL


def test_first_party_idempotency_key_maps_to_the_same_job_while_it_runs(tmp_path):
    repo = make_repo(tmp_path)
    use_case = first_party_use_case(repo)
    first = use_case.create(JOB_KIND_PRICES_REFRESH, idempotency_key="same")
    second = use_case.create(JOB_KIND_PRICES_REFRESH, idempotency_key="same")
    assert second.job_id == first.job_id
    assert second.started is False


def test_first_party_idempotency_key_is_free_once_its_job_is_terminal(tmp_path):
    repo = make_repo(tmp_path)
    use_case = first_party_use_case(repo)
    owner = first_party_owner(ENGINE_INSTANCE_ID)
    first = use_case.create(JOB_KIND_PRICES_REFRESH, idempotency_key="same")
    repo.complete(CompleteJobCommand(job_id=first.job_id, owner=owner))
    second = use_case.create(JOB_KIND_PRICES_REFRESH, idempotency_key="same")
    assert second.job_id != first.job_id
    assert second.started is True


def test_first_party_key_map_stays_bounded_across_many_unique_keys(tmp_path):
    """A fresh key per call must not grow engine memory for the whole process.

    Callers send a UUID per refresh, so nothing ever asks for these keys again
    once their job is terminal.
    """
    repo = make_repo(tmp_path)
    owner = first_party_owner(ENGINE_INSTANCE_ID)
    use_case = first_party_use_case(repo)
    creates = _MAX_TRACKED_KEYS * 3
    for index in range(creates):
        start = use_case.create(
            JOB_KIND_PRICES_REFRESH, idempotency_key=f"key-{index}"
        )
        repo.complete(CompleteJobCommand(job_id=start.job_id, owner=owner))
        assert use_case.tracked_key_count <= _MAX_TRACKED_KEYS
    assert creates > _MAX_TRACKED_KEYS
    assert use_case.tracked_key_count <= _MAX_TRACKED_KEYS


def test_first_party_key_map_is_capped_even_while_every_job_runs(tmp_path):
    """The bound holds against a caller that never lets its jobs finish."""
    repo = make_repo(tmp_path)
    use_case = first_party_use_case(repo)
    oldest = use_case.create(JOB_KIND_PRICES_REFRESH, idempotency_key="key-0")
    for index in range(1, _MAX_TRACKED_KEYS + 1):
        use_case.create(JOB_KIND_PRICES_REFRESH, idempotency_key=f"key-{index}")
    assert use_case.tracked_key_count <= _MAX_TRACKED_KEYS
    # The oldest key was forgotten, so repeating it starts a second job. That
    # costs de-duplication, never correctness: the first job is untouched and
    # still observable through jobs.get.
    again = use_case.create(JOB_KIND_PRICES_REFRESH, idempotency_key="key-0")
    assert again.started is True
    assert again.job_id != oldest.job_id
    assert repo.get(GetJobCommand(job_id=oldest.job_id)).state is JobState.RUNNING


def test_first_party_keys_are_scoped_per_job_kind(tmp_path):
    repo = make_repo(tmp_path)
    use_case = first_party_use_case(repo)
    prices = use_case.create(JOB_KIND_PRICES_REFRESH, idempotency_key="shared")
    benchmarks = use_case.create(JOB_KIND_BENCHMARKS_REFRESH, idempotency_key="shared")
    assert prices.job_id != benchmarks.job_id


def test_first_party_create_refuses_an_unknown_job_kind(tmp_path):
    repo = make_repo(tmp_path)
    with pytest.raises(FirstPartyJobKindError):
        first_party_use_case(repo).create("com.example.refresh", idempotency_key="k")


def test_first_party_create_refuses_an_owner_outside_the_reserved_namespace(tmp_path):
    repo = make_repo(tmp_path)
    with pytest.raises(FirstPartyJobOwnerError):
        CreateFirstPartyJobUseCase(repo, owner=OWNER)


def test_reserved_namespace_is_refused_to_any_external_plugin():
    assert is_first_party_plugin_id(FIRST_PARTY_PLUGIN_ID)
    assert not is_first_party_plugin_id("com.example.jobs")
    # A near miss must not pass: the reservation is the dotted prefix, not a
    # string that merely starts with the same letters.
    assert not is_first_party_plugin_id("com.modeldeck.engineering.jobs")
    guard_external_plugin_id("com.example.jobs")
    with pytest.raises(FirstPartyJobOwnerError):
        guard_external_plugin_id(FIRST_PARTY_PLUGIN_ID)
    with pytest.raises(FirstPartyJobOwnerError):
        guard_external_plugin_id(FIRST_PARTY_PLUGIN_PREFIX + "anything")


def test_any_authenticated_caller_may_read_and_cancel_an_engine_job(tmp_path):
    repo = make_repo(tmp_path)
    start = first_party_use_case(repo).create(
        JOB_KIND_BENCHMARKS_REFRESH, idempotency_key="k"
    )
    directory = FirstPartyJobDirectory(repo)
    # Two different clients: neither originated the job, both may observe it,
    # because the engine did and every row in this repository is engine-owned.
    first = directory.job_get({"job_id": start.job_id}, principal="model-deck:client:a")
    second = directory.job_get({"job_id": start.job_id}, principal="model-deck:client:b")
    assert first == second
    assert first["state"] == "running"
    accepted = directory.job_cancel(
        {"job_id": start.job_id, "idempotency_key": "c"},
        principal="model-deck:client:b",
    )
    assert accepted == {"accepted": True}
    assert repo.get(GetJobCommand(job_id=start.job_id)).cancel_requested is True


def test_engine_job_directory_reports_an_unknown_job_as_not_found(tmp_path):
    directory = FirstPartyJobDirectory(make_repo(tmp_path))
    with pytest.raises(JobsNotFoundError):
        directory.job_get(
            {"job_id": "00000000-0000-4000-8000-000000000000"},
            principal="model-deck:client:a",
        )


def test_first_party_terminal_outcome_is_exclusive(tmp_path):
    repo = make_repo(tmp_path)
    owner = first_party_owner(ENGINE_INSTANCE_ID)
    start = first_party_use_case(repo).create(
        JOB_KIND_PRICES_REFRESH, idempotency_key="k"
    )
    repo.complete(CompleteJobCommand(job_id=start.job_id, owner=owner))
    for second_write in (
        lambda: repo.complete(CompleteJobCommand(job_id=start.job_id, owner=owner)),
        lambda: repo.fail(
            FailJobCommand(job_id=start.job_id, owner=owner, failure_code="internal")
        ),
        lambda: repo.confirm_cancel(
            ConfirmCancelCommand(job_id=start.job_id, owner=owner)
        ),
    ):
        with pytest.raises(JobTerminalConflictError):
            second_write()
    assert repo.get(GetJobCommand(job_id=start.job_id)).state is JobState.COMPLETED


def test_restart_interrupts_active_engine_jobs_and_replays_none(tmp_path):
    repo = make_repo(tmp_path)
    owner = first_party_owner(ENGINE_INSTANCE_ID)
    use_case = first_party_use_case(repo)
    running = use_case.create(JOB_KIND_PRICES_REFRESH, idempotency_key="running")
    finished = use_case.create(JOB_KIND_BENCHMARKS_REFRESH, idempotency_key="finished")
    repo.complete(CompleteJobCommand(job_id=finished.job_id, owner=owner))

    outcome = recover_first_party_jobs(repo, owner)

    assert outcome.interrupted_job_ids == (running.job_id,)
    assert repo.get(GetJobCommand(job_id=running.job_id)).state is JobState.INTERRUPTED
    assert repo.get(GetJobCommand(job_id=finished.job_id)).state is JobState.COMPLETED
    # Interrupted is terminal and nothing re-queues it: the same key starts new
    # work rather than pointing at a job that will never finish.
    again = first_party_use_case(repo).create(
        JOB_KIND_PRICES_REFRESH, idempotency_key="running"
    )
    assert again.job_id != running.job_id
    assert repo.get(GetJobCommand(job_id=running.job_id)).state is JobState.INTERRUPTED


def test_recovery_refuses_an_owner_outside_the_reserved_namespace(tmp_path):
    repo = make_repo(tmp_path)
    with pytest.raises(FirstPartyJobOwnerError):
        recover_first_party_jobs(repo, OWNER)


# --- Explicit resume ---------------------------------------------------------
# jobs.resume is the only way an interrupted job ever runs again. These cases
# use the real SQLite repository and a recording invoker that stands in for the
# supervisor transport, so the ordering of the checks is pinned exactly.


RESUMABLE_OWNER = JobOwner(plugin_id="com.example.jobs", activation_id="act-1")


class RecordingInvoker:
    """Stands in for the host: hands out a live activation, records calls."""

    def __init__(self, *, activation_id="act-2", invocation_id="inv-2"):
        self.activation_id = activation_id
        self.invocation_id = invocation_id
        self.serving = True
        self.fail = False
        self.prepared: list[ResumeInvocationRequest] = []
        self.invoked: list[tuple[ResumeInvocationRequest, ResumeTarget]] = []

    def prepare(self, request):
        self.prepared.append(request)
        if not self.serving:
            return None
        return ResumeTarget(
            activation_id=self.activation_id, invocation_id=self.invocation_id
        )

    def invoke(self, request, target):
        self.invoked.append((request, target))
        if self.fail:
            raise RuntimeError("private plugin transport secret")


def create_resumable(repo, *, origin="alice", checkpoint=True):
    record = repo.create(
        CreateJobCommand(
            owner=RESUMABLE_OWNER,
            invocation_id="inv-1",
            operation_id="com.example.run",
            origin_principal_id=origin,
            checkpoint_schema_id="ckpt.v1",
            resumable=True,
        )
    )
    repo.claim(ClaimJobCommand(job_id=record.job_id, owner=RESUMABLE_OWNER))
    if checkpoint:
        repo.save_checkpoint(
            SaveCheckpointCommand(
                job_id=record.job_id,
                owner=RESUMABLE_OWNER,
                checkpoint={"next_index": 7},
                expected_revision=0,
                schema_id="ckpt.v1",
            )
        )
    return record


def interrupted_resumable(repo, **kwargs):
    record = create_resumable(repo, **kwargs)
    repo.mark_worker_crashed(RESUMABLE_OWNER)
    return record


def test_resume_returns_the_job_to_running_and_invokes_the_plugin(tmp_path):
    repo = make_repo(tmp_path)
    record = interrupted_resumable(repo)
    invoker = RecordingInvoker()
    use_case = ResumeJobUseCase(repo, invoker=invoker)

    result = use_case.execute(
        {"job_id": record.job_id, "idempotency_key": "resume-1"},
        caller_principal_id="alice",
    )

    assert result == {
        "accepted": True,
        "job_id": record.job_id,
        "resumed_from_revision": 1,
    }
    stored = repo.get(GetJobCommand(job_id=record.job_id))
    assert stored.state is JobState.RUNNING
    assert stored.resume_count == 1
    assert stored.activation_id == "act-2"
    assert stored.invocation_id == "inv-2"


def test_resume_hands_the_plugin_its_own_last_checkpoint(tmp_path):
    repo = make_repo(tmp_path)
    record = interrupted_resumable(repo)
    invoker = RecordingInvoker()
    ResumeJobUseCase(repo, invoker=invoker).execute(
        {"job_id": record.job_id, "idempotency_key": "resume-1"},
        caller_principal_id="alice",
    )

    request, target = invoker.invoked[0]
    # The sibling invocation, not a second contributed operation.
    assert request.resume_operation_id == "com.example.run.resume"
    assert request.invocation_params() == {
        "job_id": record.job_id,
        "checkpoint_revision": 1,
        "checkpoint_schema_id": "ckpt.v1",
        "checkpoint": {"next_index": 7},
    }
    assert target.activation_id == "act-2"
    # The row is already RUNNING before the plugin is touched, so the worker's
    # first progress call authorizes.
    assert repo.get(GetJobCommand(job_id=record.job_id)).state is JobState.RUNNING


def test_resume_without_a_checkpoint_reports_a_null_revision(tmp_path):
    repo = make_repo(tmp_path)
    record = interrupted_resumable(repo, checkpoint=False)
    invoker = RecordingInvoker()
    result = ResumeJobUseCase(repo, invoker=invoker).execute(
        {"job_id": record.job_id, "idempotency_key": "resume-1"},
        caller_principal_id="alice",
    )
    assert result["resumed_from_revision"] is None
    assert invoker.invoked[0][0].checkpoint is None


def test_resume_repeat_with_the_same_key_does_not_resume_twice(tmp_path):
    repo = make_repo(tmp_path)
    record = interrupted_resumable(repo)
    invoker = RecordingInvoker()
    use_case = ResumeJobUseCase(repo, invoker=invoker)
    params = {"job_id": record.job_id, "idempotency_key": "resume-1"}

    first = use_case.execute(params, caller_principal_id="alice")
    again = use_case.execute(params, caller_principal_id="alice")

    assert again == first
    assert len(invoker.invoked) == 1
    assert repo.get(GetJobCommand(job_id=record.job_id)).resume_count == 1


def test_resume_with_a_different_key_while_active_is_a_conflict(tmp_path):
    repo = make_repo(tmp_path)
    record = interrupted_resumable(repo)
    invoker = RecordingInvoker()
    use_case = ResumeJobUseCase(repo, invoker=invoker)
    use_case.execute(
        {"job_id": record.job_id, "idempotency_key": "resume-1"},
        caller_principal_id="alice",
    )
    with pytest.raises(JobResumeConflictError):
        use_case.execute(
            {"job_id": record.job_id, "idempotency_key": "resume-2"},
            caller_principal_id="alice",
        )
    assert len(invoker.invoked) == 1


def test_resume_key_reused_for_another_job_is_a_conflict(tmp_path):
    repo = make_repo(tmp_path)
    first = interrupted_resumable(repo)
    invoker = RecordingInvoker()
    use_case = ResumeJobUseCase(repo, invoker=invoker)
    use_case.execute(
        {"job_id": first.job_id, "idempotency_key": "resume-1"},
        caller_principal_id="alice",
    )
    second = create_resumable(repo)
    with pytest.raises(JobResumeConflictError):
        use_case.execute(
            {"job_id": second.job_id, "idempotency_key": "resume-1"},
            caller_principal_id="alice",
        )


def test_resume_refuses_a_job_that_never_declared_itself_resumable(tmp_path):
    repo = make_repo(tmp_path)
    record = create_with_origin(repo)
    repo.mark_worker_crashed(OWNER)
    invoker = RecordingInvoker()
    with pytest.raises(JobResumeUnsupportedError):
        ResumeJobUseCase(repo, invoker=invoker).execute(
            {"job_id": record.job_id, "idempotency_key": "resume-1"},
            caller_principal_id="alice",
        )
    assert invoker.prepared == []


def test_resume_refuses_a_running_job(tmp_path):
    repo = make_repo(tmp_path)
    record = create_resumable(repo)
    with pytest.raises(JobResumeUnsupportedError):
        ResumeJobUseCase(repo, invoker=RecordingInvoker()).execute(
            {"job_id": record.job_id, "idempotency_key": "resume-1"},
            caller_principal_id="alice",
        )


def test_resume_refuses_a_completed_job(tmp_path):
    repo = make_repo(tmp_path)
    record = create_resumable(repo)
    repo.complete(CompleteJobCommand(job_id=record.job_id, owner=RESUMABLE_OWNER))
    with pytest.raises(JobResumeUnsupportedError):
        ResumeJobUseCase(repo, invoker=RecordingInvoker()).execute(
            {"job_id": record.job_id, "idempotency_key": "resume-1"},
            caller_principal_id="alice",
        )


def test_resume_of_an_unknown_job_is_not_found(tmp_path):
    repo = make_repo(tmp_path)
    with pytest.raises(JobsNotFoundError):
        ResumeJobUseCase(repo, invoker=RecordingInvoker()).execute(
            {
                "job_id": "00000000-0000-4000-8000-000000000000",
                "idempotency_key": "resume-1",
            },
            caller_principal_id="alice",
        )


def test_resume_requires_the_originating_principal(tmp_path):
    repo = make_repo(tmp_path)
    record = interrupted_resumable(repo, origin="alice")
    invoker = RecordingInvoker()
    with pytest.raises(JobsCallerMismatchError):
        ResumeJobUseCase(repo, invoker=invoker).execute(
            {"job_id": record.job_id, "idempotency_key": "resume-1"},
            caller_principal_id="bob",
        )
    assert invoker.prepared == []
    assert repo.get(GetJobCommand(job_id=record.job_id)).state is JobState.INTERRUPTED


def test_resume_when_the_plugin_is_not_serving_leaves_the_job_interrupted(tmp_path):
    repo = make_repo(tmp_path)
    record = interrupted_resumable(repo)
    invoker = RecordingInvoker()
    invoker.serving = False
    with pytest.raises(JobsPluginUnavailableError):
        ResumeJobUseCase(repo, invoker=invoker).execute(
            {"job_id": record.job_id, "idempotency_key": "resume-1"},
            caller_principal_id="alice",
        )
    assert repo.get(GetJobCommand(job_id=record.job_id)).state is JobState.INTERRUPTED
    # Nothing was consumed, so the same key works once the plugin is back.
    assert repo.read_resume_receipt("alice", "resume-1") is None


def test_resume_reports_plugin_unavailable_without_leaking_transport_detail(tmp_path):
    repo = make_repo(tmp_path)
    record = interrupted_resumable(repo)
    invoker = RecordingInvoker()
    invoker.fail = True
    with pytest.raises(JobsPluginUnavailableError) as captured:
        ResumeJobUseCase(repo, invoker=invoker).execute(
            {"job_id": record.job_id, "idempotency_key": "resume-1"},
            caller_principal_id="alice",
        )
    assert "secret" not in str(captured.value)


def test_a_failed_invocation_is_taken_back_instead_of_wedging_the_job(tmp_path):
    """begin_resume commits first, so a failed invoke has to be undone.

    Left alone, the row would be RUNNING with no worker on it forever: no
    activation died, so worker-loss recovery never fires, the same key would
    replay the receipt as a false success, and any fresh key would see an
    "already resumed" active job and conflict.
    """
    repo = make_repo(tmp_path)
    record = interrupted_resumable(repo)
    invoker = RecordingInvoker()
    invoker.fail = True
    with pytest.raises(JobsPluginUnavailableError):
        ResumeJobUseCase(repo, invoker=invoker).execute(
            {"job_id": record.job_id, "idempotency_key": "resume-1"},
            caller_principal_id="alice",
        )

    rolled = repo.get(GetJobCommand(job_id=record.job_id))
    assert rolled.state is JobState.INTERRUPTED
    assert rolled.resume_count == 0
    assert repo.read_resume_receipt("alice", "resume-1") is None

    # And the job is genuinely resumable again, not just readable.
    invoker.fail = False
    result = ResumeJobUseCase(repo, invoker=invoker).execute(
        {"job_id": record.job_id, "idempotency_key": "resume-2"},
        caller_principal_id="alice",
    )
    assert result["accepted"] is True
    after = repo.get(GetJobCommand(job_id=record.job_id))
    assert after.state is JobState.RUNNING
    assert after.resume_count == 1


def test_a_repeat_of_a_failed_resume_key_does_not_report_false_success(tmp_path):
    repo = make_repo(tmp_path)
    record = interrupted_resumable(repo)
    invoker = RecordingInvoker()
    invoker.fail = True
    for _ in range(2):
        with pytest.raises(JobsPluginUnavailableError):
            ResumeJobUseCase(repo, invoker=invoker).execute(
                {"job_id": record.job_id, "idempotency_key": "resume-1"},
                caller_principal_id="alice",
            )
    # Two attempts, two honest failures, and the job is still resumable.
    assert repo.get(GetJobCommand(job_id=record.job_id)).state is JobState.INTERRUPTED


def test_resume_without_a_composed_invoker_is_unavailable(tmp_path):
    repo = make_repo(tmp_path)
    record = interrupted_resumable(repo)
    with pytest.raises(JobResumeUnsupportedError):
        ResumeJobUseCase(repo).execute(
            {"job_id": record.job_id, "idempotency_key": "resume-1"},
            caller_principal_id="alice",
        )


def test_resume_validates_its_params(tmp_path):
    repo = make_repo(tmp_path)
    record = interrupted_resumable(repo)
    use_case = ResumeJobUseCase(repo, invoker=RecordingInvoker())
    with pytest.raises(JobsUnknownKeyError):
        use_case.execute(
            {"job_id": record.job_id, "idempotency_key": "k", "extra": 1},
            caller_principal_id="alice",
        )
    with pytest.raises(JobsInvalidArgumentError):
        use_case.execute(
            {"job_id": "not-a-uuid", "idempotency_key": "k"},
            caller_principal_id="alice",
        )
    with pytest.raises(JobsInvalidArgumentError):
        use_case.execute({"job_id": record.job_id}, caller_principal_id="alice")


def test_get_reports_resumable_and_resume_count(tmp_path):
    repo = make_repo(tmp_path)
    record = interrupted_resumable(repo)
    get = GetJobUseCase(repo)
    before = get.execute({"job_id": record.job_id}, caller_principal_id="alice")
    assert before["resumable"] is True
    assert before["resume_count"] == 0
    ResumeJobUseCase(repo, invoker=RecordingInvoker()).execute(
        {"job_id": record.job_id, "idempotency_key": "resume-1"},
        caller_principal_id="alice",
    )
    after = get.execute({"job_id": record.job_id}, caller_principal_id="alice")
    assert after["resume_count"] == 1
    assert after["state"] == "running"


def test_first_party_jobs_are_never_resumable(tmp_path):
    """The engine's own jobs die with the process; resume answers unavailable."""
    repo = make_repo(tmp_path)
    owner = first_party_owner("engine-instance-1")
    use_case = CreateFirstPartyJobUseCase(repo, owner=owner)
    start = use_case.create(JOB_KIND_PRICES_REFRESH, idempotency_key="refresh-1")
    recover_first_party_jobs(repo, owner)
    directory = FirstPartyJobDirectory(repo)

    assert (
        repo.get(GetJobCommand(job_id=start.job_id)).state is JobState.INTERRUPTED
    )
    with pytest.raises(JobResumeUnsupportedError):
        directory.job_resume(
            {"job_id": start.job_id, "idempotency_key": "resume-1"},
            principal="anyone",
        )


def test_first_party_directory_reports_an_unknown_job_as_not_found(tmp_path):
    """So dispatch keeps probing the next directory for that id."""
    directory = FirstPartyJobDirectory(make_repo(tmp_path))
    with pytest.raises(JobsNotFoundError):
        directory.job_resume(
            {
                "job_id": "00000000-0000-4000-8000-000000000000",
                "idempotency_key": "resume-1",
            },
            principal="anyone",
        )
