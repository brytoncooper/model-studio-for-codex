"""Public jobs.get / jobs.cancel / jobs.resume use-cases over the narrow port.

These use-cases are the only public reader / canceller / resumer entry points.
They validate caller-supplied params, authorize the caller against the
origin_principal_id persisted at create-time, and return payloads shaped
exactly like the jobs.get / jobs.cancel / jobs.resume result schemas.

Cancellation is an *acknowledgement*: it returns ``{"accepted": true}`` once
the durable cancel-request flag has been recorded. It does not promise the
job is already terminal; plugin workers confirm termination through the
existing broker confirm_cancel path.

Resume is explicit and never automatic. Only an interrupted job whose
operation declared itself resumable can be resumed, only by the principal
that originated it, and only while the owning plugin is serving.
"""
from __future__ import annotations

import json
import uuid as _uuid
from collections.abc import Mapping
from typing import Any

from .ports import (
    ACTIVE_JOB_STATES,
    BoundedJsonValidationError,
    GetJobCommand,
    GetPublicCommand,
    JobIdempotencyConflictError,
    JobNotFoundError,
    JobOriginMismatchError,
    JobOwner,
    JobPublicView,
    JobResumeConflictError,
    JobResumeUnsupportedError,
    JobState,
    PluginJobRepository,
    RequestCancelPublicCommand,
    ResumeInvocationRequest,
    ResumeJobCommand,
    validate_bounded_json_value,
)

__all__ = [
    "CancelJobUseCase",
    "GetJobUseCase",
    "JobsPluginUnavailableError",
    "JobsUseCaseError",
    "ResumeJobUseCase",
]

_ALLOWED_GET_PARAMS = frozenset({"job_id"})
_ALLOWED_CANCEL_PARAMS = frozenset({"job_id", "idempotency_key"})
_ALLOWED_RESUME_PARAMS = frozenset({"job_id", "idempotency_key"})


class JobsUseCaseError(ValueError):
    """Use-case contract violation surfaced as a public-facing error."""


class JobsCallerMismatchError(JobsUseCaseError):
    """Caller does not match the originating principal recorded on the job."""


class JobsUnknownKeyError(JobsUseCaseError):
    """Caller supplied a key the contract does not advertise."""


class JobsInvalidArgumentError(JobsUseCaseError):
    """Caller supplied a malformed job_id or idempotency_key."""


class JobsIdempotencyConflictError(JobsUseCaseError):
    """The caller reused an idempotency key for a different job."""


class JobsNotFoundError(JobsUseCaseError):
    """Job is absent from durable storage."""


class JobsPluginUnavailableError(JobsUseCaseError):
    """The plugin that owns this job is not serving, so it cannot be resumed.

    Maps to the ``plugin_unavailable`` domain error. Unlike
    ``resume_unavailable`` this is a temporary answer: enabling the plugin
    again makes the same request work.
    """


def _validate_uuid(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise JobsInvalidArgumentError(f"{name} must be a non-empty string")
    try:
        parsed = _uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise JobsInvalidArgumentError(f"{name} must be a UUID")
    if str(parsed).casefold() != value.casefold():
        raise JobsInvalidArgumentError(f"{name} must be canonical UUID spelling")
    return value


def _validate_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise JobsInvalidArgumentError("idempotency_key must be a non-empty string")
    if len(value) > 128:
        raise JobsInvalidArgumentError("idempotency_key must be at most 128 characters")
    return value


def _reject_unknown_keys(
    mapping: Mapping[str, Any],
    allowed: frozenset[str],
) -> None:
    unknown = set(mapping.keys()) - allowed
    if unknown:
        names = ", ".join(sorted(unknown))
        raise JobsUnknownKeyError(f"unknown params field(s): {names}")


def _public_view_to_dict(view: JobPublicView) -> dict[str, Any]:
    """Map a JobPublicView to the jobs.get.result schema.

    output is included only when output_present is True; absent output is
    omitted entirely so JSON null and absent remain distinguishable in the
    public payload.
    """

    payload: dict[str, Any] = {
        "job_id": view.job_id,
        "state": view.state.value,
        "progress": view.progress,
        "resumable": view.resumable,
        "resume_count": view.resume_count,
    }
    if view.output_present:
        payload["output"] = view.output
    return payload


class GetJobUseCase:
    """Read a job's bounded public state.

    Requires a caller_principal_id (taken from the trusted AuthorityContext,
    never from request params) so durable cross-restart authorization is
    possible without relying on ephemeral state.
    """

    def __init__(self, repository: PluginJobRepository) -> None:
        if not hasattr(repository, "get_public"):
            raise TypeError("repository must implement get_public")
        self._repository = repository

    def execute(
        self,
        params: Mapping[str, Any] | None,
        *,
        caller_principal_id: str,
    ) -> dict[str, Any]:
        params = dict(params or {})
        _reject_unknown_keys(params, _ALLOWED_GET_PARAMS)
        job_id = _validate_uuid("job_id", params.get("job_id"))
        if not isinstance(caller_principal_id, str) or not caller_principal_id:
            raise JobsInvalidArgumentError("caller_principal_id required")
        try:
            view = self._repository.get_public(
                GetPublicCommand(
                    job_id=job_id,
                    caller_principal_id=caller_principal_id,
                )
            )
        except JobNotFoundError:
            raise JobsNotFoundError("job not found")
        except JobOriginMismatchError as exc:
            raise JobsCallerMismatchError(str(exc))
        return _public_view_to_dict(view)


class CancelJobUseCase:
    """Request cancellation; returns ``{"accepted": true}`` on acknowledgement.

    The caller MUST be the originating principal. Cancellation is durable
    but non-terminal: the public get can still observe an active state until
    the worker confirms via broker.confirm_cancel.
    """

    def __init__(self, repository: PluginJobRepository) -> None:
        if not hasattr(repository, "request_cancel_public"):
            raise TypeError("repository must implement request_cancel_public")
        self._repository = repository

    def execute(
        self,
        params: Mapping[str, Any] | None,
        *,
        caller_principal_id: str,
    ) -> dict[str, Any]:
        params = dict(params or {})
        _reject_unknown_keys(params, _ALLOWED_CANCEL_PARAMS)
        job_id = _validate_uuid("job_id", params.get("job_id"))
        idempotency_key = _validate_idempotency_key(params.get("idempotency_key"))
        if not isinstance(caller_principal_id, str) or not caller_principal_id:
            raise JobsInvalidArgumentError("caller_principal_id required")
        try:
            accepted = self._repository.request_cancel_public(
                RequestCancelPublicCommand(
                    job_id=job_id,
                    caller_principal_id=caller_principal_id,
                    idempotency_key=idempotency_key,
                )
            )
        except JobNotFoundError:
            raise JobsNotFoundError("job not found")
        except JobOriginMismatchError as exc:
            raise JobsCallerMismatchError(str(exc))
        except JobIdempotencyConflictError as exc:
            raise JobsIdempotencyConflictError(str(exc))
        # False means the job was already terminal: the user's cancel did
        # not need to be recorded. Either way we return exactly the schema.
        return {"accepted": bool(accepted)}


class ResumeJobUseCase:
    """Restart one interrupted, resumable job from its last checkpoint.

    Never automatic: nothing in startup, worker recovery, or the runner calls
    this. A resume happens only because a caller asked for this job by id.

    The order of the checks is the contract:

    1. the job must exist (``not_found``),
    2. the caller must be the principal that originated it
       (``capability_denied``),
    3. an exact replay of a settled ``(principal, idempotency_key)`` returns
       that original outcome, and the same key against a different job is a
       ``conflict``,
    4. the job's operation must have declared itself resumable and the job
       must be interrupted (``resume_unavailable``), except that an
       already-resumed job still working is a ``conflict`` rather than a
       second run of the same work,
    5. the owning plugin must be serving right now (``plugin_unavailable``).

    Only then is the durable row rebound to the live activation and returned
    to RUNNING, and only then is the plugin invoked. Nothing is replayed: the
    plugin receives its own last checkpoint and decides what remains.

    The transition has to commit before the worker is touched, so an
    invocation that fails is undone by ``rollback_resume`` before the caller
    is told ``plugin_unavailable``. Nothing else would: no activation died, so
    worker-loss recovery never fires on a job whose resume never landed.
    """

    def __init__(
        self,
        repository: PluginJobRepository,
        *,
        invoker: Any | None = None,
    ) -> None:
        for name in (
            "get",
            "begin_resume",
            "rollback_resume",
            "read_checkpoint",
            "read_resume_receipt",
        ):
            if not hasattr(repository, name):
                raise TypeError(f"repository must implement {name}")
        if invoker is not None and not (
            callable(getattr(invoker, "prepare", None))
            and callable(getattr(invoker, "invoke", None))
        ):
            raise TypeError("invoker must implement prepare and invoke")
        self._repository = repository
        self._invoker = invoker

    def execute(
        self,
        params: Mapping[str, Any] | None,
        *,
        caller_principal_id: str,
    ) -> dict[str, Any]:
        params = dict(params or {})
        _reject_unknown_keys(params, _ALLOWED_RESUME_PARAMS)
        job_id = _validate_uuid("job_id", params.get("job_id"))
        idempotency_key = _validate_idempotency_key(params.get("idempotency_key"))
        if not isinstance(caller_principal_id, str) or not caller_principal_id:
            raise JobsInvalidArgumentError("caller_principal_id required")

        record = self._load_owned(job_id, caller_principal_id)

        settled = self._repository.read_resume_receipt(
            caller_principal_id, idempotency_key
        )
        if settled is not None:
            settled_job_id, settled_revision = settled
            if settled_job_id != job_id:
                raise JobResumeConflictError(
                    "idempotency key already used for another job"
                )
            return _resume_result(job_id, settled_revision)

        if not record.resumable:
            raise JobResumeUnsupportedError("job is not resumable")
        if record.state is not JobState.INTERRUPTED:
            if record.state in ACTIVE_JOB_STATES and record.resume_count > 0:
                raise JobResumeConflictError("job is already resumed")
            raise JobResumeUnsupportedError("job is not interrupted")

        request = self._resume_request(record)
        if self._invoker is None:
            # Nothing can carry the job back to a worker, so saying "resume"
            # would be a promise the engine cannot keep.
            raise JobResumeUnsupportedError("no resume transport is composed")
        target = self._invoker.prepare(request)
        if target is None:
            raise JobsPluginUnavailableError("owning plugin is not serving")

        self._repository.begin_resume(
            ResumeJobCommand(
                job_id=job_id,
                caller_principal_id=caller_principal_id,
                idempotency_key=idempotency_key,
                activation_id=target.activation_id,
                invocation_id=target.invocation_id,
            )
        )
        # The row is RUNNING and bound to the live activation before the
        # worker is touched, so its first progress call already authorizes.
        # An invocation that then fails has to be taken back by hand: nothing
        # else would. No activation died, so worker-loss recovery never fires,
        # and a RUNNING row that nobody is working on is permanent -- the same
        # key would replay the receipt as a false success and a fresh key
        # would conflict with an "already resumed" job forever.
        try:
            self._invoker.invoke(request, target)
        except (JobsUseCaseError, JobResumeUnsupportedError, JobResumeConflictError):
            raise
        except Exception:
            # A rollback that itself fails is not swallowed. The job really
            # is wedged then, and an opaque failure the operator can see beats
            # a tidy "plugin unavailable" that implies a simple retry.
            self._repository.rollback_resume(
                job_id, caller_principal_id, idempotency_key
            )
            raise JobsPluginUnavailableError(
                "owning plugin did not accept the resume"
            ) from None
        return _resume_result(job_id, request.checkpoint_revision)

    def _load_owned(self, job_id: str, caller_principal_id: str):
        try:
            record = self._repository.get(GetJobCommand(job_id=job_id))
        except JobNotFoundError:
            raise JobsNotFoundError("job not found")
        origin = record.origin_principal_id
        if not origin:
            raise JobsCallerMismatchError("job has no recorded origin")
        if origin != caller_principal_id:
            raise JobsCallerMismatchError("caller is not the originating principal")
        return record

    def _resume_request(self, record) -> ResumeInvocationRequest:
        owner = JobOwner(
            plugin_id=record.plugin_id, activation_id=record.activation_id
        )
        stored = self._repository.read_checkpoint(owner, record.job_id)
        if stored is None:
            revision: int | None = None
            schema_id: str | None = None
            checkpoint: Any = None
        else:
            revision, schema_id, encoded = stored
            checkpoint = _decoded_checkpoint(encoded)
        return ResumeInvocationRequest(
            job_id=record.job_id,
            plugin_id=record.plugin_id,
            operation_id=record.operation_id,
            origin_principal_id=record.origin_principal_id,
            checkpoint_revision=revision,
            checkpoint_schema_id=schema_id,
            checkpoint=checkpoint,
        )


def _decoded_checkpoint(encoded: str | None) -> Any:
    """Decode and revalidate a stored checkpoint before a worker sees it.

    The bytes came back from storage, so they are re-checked against the same
    bounded policy that admitted them; a checkpoint that no longer passes is
    not something to hand a worker, and the job simply cannot be resumed.
    """
    if encoded is None:
        return None
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError):
        raise JobResumeUnsupportedError("stored checkpoint is unreadable") from None
    try:
        validate_bounded_json_value(value)
    except BoundedJsonValidationError:
        raise JobResumeUnsupportedError("stored checkpoint is unreadable") from None
    return value


def _resume_result(job_id: str, revision: int | None) -> dict[str, Any]:
    """The jobs.resume payload; the revision is required and nullable."""
    return {
        "accepted": True,
        "job_id": job_id,
        "resumed_from_revision": revision,
    }
