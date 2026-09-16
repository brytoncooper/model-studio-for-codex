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
from .first_party import is_first_party_plugin_id
from .ports import (
    ACTIVE_JOB_STATES,
    FAILURE_CODES,
    BoundedJsonValidationError,
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
    JobRecord,
    JobStateConflictError,
    JobTerminalConflictError,
    ReportProgressCommand,
    SaveCheckpointCommand,
    validate_bounded_json_value,
)

_OPERATIONS = ("create", "progress", "complete", "fail", "check_cancelled")

CHECKPOINT_OPERATION = "checkpoint"
"""The optional sixth grant name.

``checkpoint`` arrived after the first five, and every existing composition
passes exactly those five. A grants dict may therefore leave it out, in which
case it inherits the ``progress`` grant: saving a checkpoint is the same
authority as reporting progress on the same job, a write the worker already
holds for that job, so inheriting names it rather than widening it.
"""

_REVERSE_DOMAIN = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9_-]*)+$")
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


def _check_expected_revision(value: object) -> int:
    """null and 0 both mean "no checkpoint has been saved yet"."""
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise BrokerJobInvalidRequestError()
    return value


def _check_progress(value: object) -> float:
    if type(value) is bool or not isinstance(value, (int, float)):
        raise BrokerJobInvalidRequestError()
    if not 0.0 <= value <= 1.0:
        raise BrokerJobInvalidRequestError()
    return float(value)


def _check_json_value(value: Any) -> None:
    """Application-owned bounded JSON validation.

    Delegates to ``validate_bounded_json_value``; surfaces violations as
    the fixed ``BrokerJobInvalidRequestError`` message without echoing the
    offending payload.
    """
    try:
        validate_bounded_json_value(value)
    except BoundedJsonValidationError:
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
    # Confused-deputy guard. Every broker entry point passes through here, so a
    # plugin whose manifest declares an identifier inside the engine's reserved
    # namespace can never create, observe or terminate a first-party job.
    if is_first_party_plugin_id(activation.plugin_id):
        raise BrokerJobDeniedError()
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
        resumable_operations: Callable[[str], bool] | None = None,
    ) -> None:
        if not isinstance(authority, PluginAuthority):
            raise BrokerJobInvalidRequestError()
        if not hasattr(repository, "create") or not hasattr(repository, "get"):
            raise BrokerJobInvalidRequestError()
        if type(grants) is not dict or set(grants) - {CHECKPOINT_OPERATION} != set(
            _OPERATIONS
        ):
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
        checkpoint_grant = grants.get(CHECKPOINT_OPERATION, parsed["progress"])
        if type(checkpoint_grant) is not tuple or len(checkpoint_grant) != 3:
            raise BrokerJobInvalidRequestError()
        for part in checkpoint_grant:
            if type(part) is not str or not part:
                raise BrokerJobInvalidRequestError()
        parsed[CHECKPOINT_OPERATION] = checkpoint_grant
        if not callable(mutation_guard):
            raise BrokerJobInvalidRequestError()
        if resumable_operations is not None and not callable(resumable_operations):
            raise BrokerJobInvalidRequestError()
        self._authority = authority
        self._repository = repository
        self._grants = parsed
        self._mutation_guard = mutation_guard
        self._resumable_operations = resumable_operations

    def _declared_resumable(self, operation_id: str) -> bool:
        """Ask composition whether this operation's manifest said resumable.

        The worker never gets a say. With no lookup composed, nothing is
        resumable, which is the safe answer: an unresumable job simply cannot
        be restarted, while a wrongly resumable one would hand a worker a
        checkpoint it never agreed to honor.

        A lookup that is composed but *fails* is a different thing from one
        that is absent, and it is not safe to answer False for it. False gets
        written into the durable row as "this operation never declared itself
        resumable", which outlives the transient failure, is indistinguishable
        from the real thing, and makes the job answer ``resume_unavailable``
        for the rest of its life. Failing the create is recoverable; a
        silently unresumable job is not.
        """
        if self._resumable_operations is None:
            return False
        try:
            return bool(self._resumable_operations(operation_id))
        except Exception:
            raise BrokerJobOperationError() from None

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
            # Resolved before the repository call, not inside it: a failed
            # lookup is an operation failure, not a malformed request, and
            # the surrounding except would have mislabelled it.
            resumable = self._declared_resumable(live.operation_id)
            try:
                created = self._repository.create(
                    CreateJobCommand(
                        owner=JobOwner(
                            plugin_id=activation.plugin_id,
                            activation_id=activation.activation_id,
                        ),
                        invocation_id=live.invocation_id,
                        operation_id=live.operation_id,
                        origin_principal_id=live.origin_principal_id,
                        checkpoint_schema_id=schema_id,
                        resumable=resumable,
                    )
                )
            except (ValueError, TypeError):
                raise BrokerJobInvalidRequestError()
            # Folded claim: transition QUEUED -> RUNNING inside the same
            # guarded block under the create authority, so callers see a
            # RUNNING job by the time create() returns. A crash between
            # create and claim leaves the row QUEUED; worker-loss recovery
            # then transitions it to INTERRUPTED (never auto-replayed).
            try:
                record = self._repository.claim(
                    ClaimJobCommand(
                        job_id=created.job_id,
                        owner=JobOwner(
                            plugin_id=activation.plugin_id,
                            activation_id=activation.activation_id,
                        ),
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

    def checkpoint(
        self,
        authenticated_activation: ActivationIdentity,
        *,
        job_id: str,
        checkpoint: Any,
        expected_revision: int | None,
        schema_id: str | None = None,
    ) -> dict[str, int]:
        """Save one resume checkpoint under compare-and-swap.

        ``expected_revision`` is what the worker believes is stored; null and
        0 both mean "nothing saved yet". A mismatch stores nothing and is a
        conflict, so a worker that lost a race never silently overwrites the
        newer state. The job must still be active and owned by this
        activation.

        ``schema_id`` is optional, exactly as the published params schema
        says: omitting it keeps the schema the job declared at create, and
        storage still validates the checkpoint against that declaration, so
        an unlabelled save is never an unvalidated one. Supplying a schema_id
        that is not the declared schema is what gets rejected. Refusing an
        omitted schema_id here would break every worker that took the
        contract at its word.
        """
        activation = _check_activation(authenticated_activation)
        job_id = _check_job_id(job_id)
        revision = _check_expected_revision(expected_revision)
        requested_schema_id = _check_schema_id(schema_id)
        _check_json_value(checkpoint)
        with self._guarded():
            record = self._trusted_followup(job_id, activation, CHECKPOINT_OPERATION)
            try:
                saved = self._repository.save_checkpoint(
                    SaveCheckpointCommand(
                        job_id=record.job_id,
                        owner=self._owner_of(record),
                        checkpoint=checkpoint,
                        expected_revision=revision,
                        schema_id=requested_schema_id,
                    )
                )
            except JobNotFoundError:
                raise BrokerJobNotFoundError()
            except JobOwnershipMismatchError:
                raise BrokerJobDeniedError()
            except JobCheckpointConflictError:
                raise BrokerJobConflictError()
            except JobCheckpointValidationError:
                raise BrokerJobInvalidRequestError()
            except JobTerminalConflictError:
                raise BrokerJobTerminalError()
            except JobStateConflictError:
                raise BrokerJobConflictError()
            except BrokerJobError:
                raise
            except (ValueError, TypeError):
                raise BrokerJobInvalidRequestError()
        return {"revision": int(saved.checkpoint_revision)}

    def complete(
        self,
        authenticated_activation: ActivationIdentity,
        *,
        job_id: str,
        output: Any = None,
        output_present: bool = False,
    ) -> dict[str, bool]:
        """Worker-driven completion.

        ``output_present`` is True iff the worker supplied an ``output`` value
        (including explicit JSON null). When True, ``output`` is validated and
        persisted atomically with the COMPLETED transition; when False, no
        output is written and the public reader will not surface one for this
        job.
        """
        activation = _check_activation(authenticated_activation)
        job_id = _check_job_id(job_id)
        if output_present:
            _check_json_value(output)
        with self._guarded():
            record = self._trusted_followup(job_id, activation, "complete")
            try:
                self._repository.complete(
                    CompleteJobCommand(
                        job_id=record.job_id,
                        owner=self._owner_of(record),
                        output=output,
                        output_present=output_present,
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
        """Worker poll point: also the worker-driven cancellation acknowledgement.

        When ``cancel_requested`` is true and the job is still active, this
        call atomically transitions the job to CANCELLED via
        ``repository.confirm_cancel`` before returning. If a concurrent
        ``complete`` or ``fail`` already won the terminal transition, the
        confirm_cancel attempt raises ``JobTerminalConflictError`` and we
        treat it as the user's intent having been satisfied anyway: the
        response still surfaces ``{cancelled: True}``.
        """
        activation = _check_activation(authenticated_activation)
        job_id = _check_job_id(job_id)
        with self._guarded():
            record = self._trusted_followup(job_id, activation, "check_cancelled")
            if (
                record.cancel_requested
                and record.state in ACTIVE_JOB_STATES
            ):
                try:
                    self._repository.confirm_cancel(
                        ConfirmCancelCommand(
                            job_id=record.job_id,
                            owner=self._owner_of(record),
                        )
                    )
                except JobTerminalConflictError:
                    # Another terminal commit (complete/fail/confirm_cancel)
                    # already won. The user-visible intent is satisfied; the
                    # response still reports cancel was observed.
                    pass
                except JobStateConflictError:
                    # Same reasoning: a terminal race left the job in some
                    # other terminal state; cancel was effectively applied.
                    pass
                except BrokerJobError:
                    raise
        return {"cancelled": bool(record.cancel_requested)}
