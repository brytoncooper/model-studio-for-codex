"""Public jobs.get / jobs.cancel use-cases over the narrow repository port.

These use-cases are the only public reader/canceller entry points. They
validate caller-supplied params, authorize the caller against the
origin_principal_id persisted at create-time, and return the public view
shaped exactly like the jobs.get / jobs.cancel result schemas.

Cancellation is an *acknowledgement*: it returns ``{"accepted": true}`` once
the durable cancel-request flag has been recorded. It does not promise the
job is already terminal; plugin workers confirm termination through the
existing broker confirm_cancel path.
"""
from __future__ import annotations

import uuid as _uuid
from collections.abc import Mapping
from typing import Any

from .ports import (
    GetPublicCommand,
    JobNotFoundError,
    JobOriginMismatchError,
    JobPublicView,
    PluginJobRepository,
    RequestCancelPublicCommand,
)

__all__ = ["GetJobUseCase", "CancelJobUseCase", "JobsUseCaseError"]

_ALLOWED_GET_PARAMS = frozenset({"job_id"})
_ALLOWED_CANCEL_PARAMS = frozenset({"job_id", "idempotency_key"})


class JobsUseCaseError(ValueError):
    """Use-case contract violation surfaced as a public-facing error."""


class JobsCallerMismatchError(JobsUseCaseError):
    """Caller does not match the originating principal recorded on the job."""


class JobsUnknownKeyError(JobsUseCaseError):
    """Caller supplied a key the contract does not advertise."""


class JobsInvalidArgumentError(JobsUseCaseError):
    """Caller supplied a malformed job_id or idempotency_key."""


class JobsNotFoundError(JobsUseCaseError):
    """Job is absent from durable storage."""


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
        _validate_idempotency_key(params.get("idempotency_key"))
        if not isinstance(caller_principal_id, str) or not caller_principal_id:
            raise JobsInvalidArgumentError("caller_principal_id required")
        try:
            accepted = self._repository.request_cancel_public(
                RequestCancelPublicCommand(
                    job_id=job_id,
                    caller_principal_id=caller_principal_id,
                )
            )
        except JobNotFoundError:
            raise JobsNotFoundError("job not found")
        except JobOriginMismatchError as exc:
            raise JobsCallerMismatchError(str(exc))
        # False means the job was already terminal: the user's cancel did
        # not need to be recorded. Either way we return exactly the schema.
        return {"accepted": bool(accepted)}
