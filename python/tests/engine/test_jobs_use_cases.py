"""Public jobs.get / jobs.cancel use-case tests (B22)."""
from __future__ import annotations

from pathlib import Path

import pytest

from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.engine.jobs.ports import (
    ClaimJobCommand,
    CompleteJobCommand,
    GetJobCommand,
    JobOwner,
    RequestCancelPublicCommand,
)
from model_deck.engine.jobs.use_cases import (
    CancelJobUseCase,
    GetJobUseCase,
    JobsCallerMismatchError,
    JobsInvalidArgumentError,
    JobsNotFoundError,
    JobsUnknownKeyError,
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
