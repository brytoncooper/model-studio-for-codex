"""Supervisor-side job broker over settled PluginAuthority and PluginJobRepository."""
from __future__ import annotations

import math
import re
import uuid as uuid_module
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from ..plugin_authority import (
    ActivationIdentity,
    AuthorityDeniedError,
    PluginAuthority,
)
from .ports import (
    FAILURE_CODES,
    CompleteJobCommand,
    CreateJobCommand,
    FailJobCommand,
    GetJobCommand,
    JobNotFoundError,
    JobOwner,
    JobOwnershipMismatchError,
    JobRecord,
    JobStateConflictError,
    JobTerminalConflictError,
    ReportProgressCommand,
)

_OPERATIONS = ("create", "progress", "complete", "fail", "check_cancelled")

_REVERSE_DOMAIN = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9_-]*)+$")
_JSON_STRING_MAX = 1048576
_JSON_ARRAY_MAX = 4096
_JSON_OBJECT_MAX = 1024


class BrokerJobError(ValueError):
    pass


class BrokerJobInvalidRequestError(BrokerJobError):
    def __init__(self) -> None:
        super().__init__("broker invalid request")


class BrokerJobNotFoundError(BrokerJobError):
    def __init__(self) -> None:
        super().__init__("broker job not found")


class BrokerJobConflictError(BrokerJobError):
    def __init__(self) -> None:
        super().__init__("broker job conflict")


class BrokerJobTerminalError(BrokerJobError):
    def __init__(self) -> None:
        super().__init__("broker job terminal")


class BrokerJobGuardError(BrokerJobError):
    def __init__(self) -> None:
        super().__init__("broker guard failed")


class BrokerJobOperationError(BrokerJobError):
    def __init__(self) -> None:
        super().__init__("broker operation failed")


class BrokerJobDeniedError(PermissionError):
    def __init__(self) -> None:
        super().__init__("broker authority denied")


def _check_handle(handle: object) -> str:
    if type(handle) is not str or not handle or len(handle) > 128:
        raise BrokerJobInvalidRequestError()
    return handle


def _check_job_id(job_id: object) -> str:
    if type(job_id) is not str:
        raise BrokerJobInvalidRequestError()
    try:
        uuid_module.UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise BrokerJobInvalidRequestError()
    return job_id


def _check_operation_id(operation_id: object) -> str:
    if type(operation_id) is not str:
        raise BrokerJobInvalidRequestError()
    if not 3 <= len(operation_id) <= 256 or not _REVERSE_DOMAIN.match(operation_id):
        raise BrokerJobInvalidRequestError()
    return operation_id


def _check_schema_id(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > 256:
        raise BrokerJobInvalidRequestError()
    return value


def _check_progress(value: object) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        raise BrokerJobInvalidRequestError()
    if not 0.0 <= value <= 1.0:
        raise BrokerJobInvalidRequestError()
    return float(value)


def _check_json_value(value: Any) -> None:
    seen: set[int] = set()
    def visit(node: Any) -> None:
        if node is None or type(node) is bool:
            return
        if type(node) is int:
            return
        if type(node) is float:
            if not math.isfinite(node):
                raise BrokerJobInvalidRequestError()
            return
        if type(node) is str:
            if len(node) > _JSON_STRING_MAX:
                raise BrokerJobInvalidRequestError()
            return
        if type(node) is list:
            if len(node) > _JSON_ARRAY_MAX:
                raise BrokerJobInvalidRequestError()
            if id(node) in seen:
                raise BrokerJobInvalidRequestError()
            seen.add(id(node))
            for item in node:
                visit(item)
            seen.discard(id(node))
            return
        if type(node) is dict:
            if len(node) > _JSON_OBJECT_MAX:
                raise BrokerJobInvalidRequestError()
            if id(node) in seen:
                raise BrokerJobInvalidRequestError()
            seen.add(id(node))
            for key, item in node.items():
                if type(key) is not str:
                    raise BrokerJobInvalidRequestError()
                visit(item)
            seen.discard(id(node))
            return
        raise BrokerJobInvalidRequestError()
    try:
        visit(value)
    except RecursionError:
        raise BrokerJobInvalidRequestError() from None


def _check_error(error: object) -> str:
    if type(error) is not dict:
        raise BrokerJobInvalidRequestError()
    allowed = {"code", "retryable", "message", "request_id"}
    for key in error:
        if type(key) is not str or key not in allowed:
            raise BrokerJobInvalidRequestError()
    if "code" not in error or "retryable" not in error:
        raise BrokerJobInvalidRequestError()
    code = error["code"]
    retryable = error["retryable"]
    if type(code) is not str or code not in FAILURE_CODES:
        raise BrokerJobInvalidRequestError()
    if type(retryable) is not bool:
        raise BrokerJobInvalidRequestError()
    for field, maximum in (("message", 2048), ("request_id", 64)):
        if field in error:
            value = error[field]
            if type(value) is not str or len(value) > maximum:
                raise BrokerJobInvalidRequestError()
    return code


def _check_activation(activation: object) -> ActivationIdentity:
    if type(activation) is not ActivationIdentity:
        raise BrokerJobInvalidRequestError()
    return activation


class PluginJobBroker:
    """Supervisor-side broker; synchronous, no sockets or callbacks."""

    def __init__(
        self,
        *,
        authority: PluginAuthority,
        repository: Any,
        grants: dict[str, tuple[str, str, str]],
        mutation_guard: Callable[[], AbstractContextManager[None]],
    ) -> None:
        if not isinstance(authority, PluginAuthority):
            raise BrokerJobInvalidRequestError()
        if not hasattr(repository, "create") or not hasattr(repository, "get"):
            raise BrokerJobInvalidRequestError()
        if type(grants) is not dict or set(grants) != set(_OPERATIONS):
            raise BrokerJobInvalidRequestError()
        parsed: dict[str, tuple[str, str, str]] = {}
        for operation in _OPERATIONS:
            triple = grants[operation]
            if type(triple) is not tuple or len(triple) != 3:
                raise BrokerJobInvalidRequestError()
            for part in triple:
                if type(part) is not str or not part:
                    raise BrokerJobInvalidRequestError()
            parsed[operation] = triple
        if not callable(mutation_guard):
            raise BrokerJobInvalidRequestError()
        self._authority = authority
        self._repository = repository
        self._grants = parsed
        self._mutation_guard = mutation_guard

    def revocation_barrier(self) -> AbstractContextManager[None]:
        return self._guarded()

    @contextmanager
    def _guarded(self) -> Iterator[None]:
        """Contain guard failures; a guard cannot suppress an operation denial.

        Exit failure can happen after a repository commit. It reports failure
        without promising rollback or making the operation safe to retry.
        """
        try:
            guard = self._mutation_guard()
            guard.__enter__()
        except Exception:
            raise BrokerJobGuardError() from None
        operation_error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            operation_error = exc
        try:
            guard.__exit__(
                type(operation_error) if operation_error is not None else None,
                operation_error,
                operation_error.__traceback__ if operation_error is not None else None,
            )
        except Exception:
            raise BrokerJobGuardError() from None
        if operation_error is not None:
            if isinstance(operation_error, (BrokerJobError, BrokerJobDeniedError)):
                raise operation_error from None
            if isinstance(operation_error, Exception):
                raise BrokerJobOperationError() from None
            raise operation_error

    def create(
        self,
        handle: str,
        authenticated_activation: ActivationIdentity,
        *,
        operation_id: str,
        checkpoint_schema_id: str | None = None,
    ) -> dict[str, str]:
        handle = _check_handle(handle)
        activation = _check_activation(authenticated_activation)
        requested_operation = _check_operation_id(operation_id)
        schema_id = _check_schema_id(checkpoint_schema_id)
        with self._guarded():
            try:
                captured = self._authority.capture(handle, activation)
            except AuthorityDeniedError:
                raise BrokerJobDeniedError()
            if captured.operation_id != requested_operation:
                raise BrokerJobDeniedError()
            effect, scope, grant = self._grants["create"]
            try:
                live = self._authority.authorize(
                    handle,
                    activation,
                    effect=effect,
                    resource_scope=scope,
                    capability_grant=grant,
                    private_namespace=activation.plugin_id,
                )
            except AuthorityDeniedError:
                raise BrokerJobDeniedError()
            try:
                record = self._repository.create(
                    CreateJobCommand(
                        owner=JobOwner(
                            plugin_id=activation.plugin_id,
                            activation_id=activation.activation_id,
                        ),
                        invocation_id=live.invocation_id,
                        operation_id=live.operation_id,
                        checkpoint_schema_id=schema_id,
                    )
                )
            except (ValueError, TypeError):
                raise BrokerJobInvalidRequestError()
        return {"job_id": record.job_id}

    def _trusted_followup(
        self, job_id: str, activation: ActivationIdentity, operation: str
    ) -> JobRecord:
        record = self._load_trusted(job_id, activation)
        effect, scope, grant = self._grants[operation]
        try:
            self._authority.reauthorize_captured(
                record.invocation_id,
                activation,
                effect=effect,
                resource_scope=scope,
                capability_grant=grant,
                private_namespace=activation.plugin_id,
            )
        except AuthorityDeniedError:
            raise BrokerJobDeniedError()
        return record

    def _load_trusted(self, job_id: str, activation: ActivationIdentity) -> JobRecord:
        try:
            record = self._repository.get(GetJobCommand(job_id=job_id))
        except JobNotFoundError:
            raise BrokerJobNotFoundError()
        except BrokerJobError:
            raise
        except (KeyError, ValueError):
            raise BrokerJobInvalidRequestError()
        if not isinstance(record, JobRecord):
            raise BrokerJobInvalidRequestError()
        if (
            record.plugin_id != activation.plugin_id
            or record.activation_id != activation.activation_id
        ):
            raise BrokerJobDeniedError()
        return record

    @staticmethod
    def _owner_of(record: JobRecord) -> JobOwner:
        return JobOwner(plugin_id=record.plugin_id, activation_id=record.activation_id)

    def report_progress(
        self,
        authenticated_activation: ActivationIdentity,
        *,
        job_id: str,
        progress: float,
    ) -> dict[str, bool]:
        activation = _check_activation(authenticated_activation)
        job_id = _check_job_id(job_id)
        value = _check_progress(progress)
        with self._guarded():
            record = self._trusted_followup(job_id, activation, "progress")
            try:
                self._repository.report_progress(
                    ReportProgressCommand(
                        job_id=record.job_id, owner=self._owner_of(record), progress=value
                    )
                )
            except JobNotFoundError:
                raise BrokerJobNotFoundError()
            except JobOwnershipMismatchError:
                raise BrokerJobDeniedError()
            except JobTerminalConflictError:
                raise BrokerJobTerminalError()
            except JobStateConflictError:
                raise BrokerJobConflictError()
            except BrokerJobError:
                raise
            except (ValueError, TypeError):
                raise BrokerJobInvalidRequestError()
        return {"accepted": True}

    def complete(
        self,
        authenticated_activation: ActivationIdentity,
        *,
        job_id: str,
        output: Any = None,
    ) -> dict[str, bool]:
        activation = _check_activation(authenticated_activation)
        job_id = _check_job_id(job_id)
        if output is not None:
            _check_json_value(output)
        with self._guarded():
            record = self._trusted_followup(job_id, activation, "complete")
            try:
                self._repository.complete(
                    CompleteJobCommand(job_id=record.job_id, owner=self._owner_of(record))
                )
            except JobNotFoundError:
                raise BrokerJobNotFoundError()
            except JobOwnershipMismatchError:
                raise BrokerJobDeniedError()
            except JobTerminalConflictError:
                raise BrokerJobTerminalError()
            except JobStateConflictError:
                raise BrokerJobConflictError()
            except BrokerJobError:
                raise
            except (ValueError, TypeError):
                raise BrokerJobInvalidRequestError()
        return {"completed": True}

    def fail(
        self,
        authenticated_activation: ActivationIdentity,
        *,
        job_id: str,
        error: dict[str, Any],
    ) -> dict[str, bool]:
        activation = _check_activation(authenticated_activation)
        job_id = _check_job_id(job_id)
        failure_code = _check_error(error)
        with self._guarded():
            record = self._trusted_followup(job_id, activation, "fail")
            try:
                self._repository.fail(
                    FailJobCommand(
                        job_id=record.job_id,
                        owner=self._owner_of(record),
                        failure_code=failure_code,
                    )
                )
            except JobNotFoundError:
                raise BrokerJobNotFoundError()
            except JobOwnershipMismatchError:
                raise BrokerJobDeniedError()
            except JobTerminalConflictError:
                raise BrokerJobTerminalError()
            except JobStateConflictError:
                raise BrokerJobConflictError()
            except BrokerJobError:
                raise
            except (ValueError, TypeError):
                raise BrokerJobInvalidRequestError()
        return {"failed": True}

    def check_cancelled(
        self,
        authenticated_activation: ActivationIdentity,
        *,
        job_id: str,
    ) -> dict[str, bool]:
        activation = _check_activation(authenticated_activation)
        job_id = _check_job_id(job_id)
        with self._guarded():
            record = self._trusted_followup(job_id, activation, "check_cancelled")
        return {"cancelled": bool(record.cancel_requested)}
