"""Serialized extension lifecycle orchestration over frozen B20 ports."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from typing import Protocol, TypeVar, runtime_checkable

from .ports import (
    ClaimDisposition,
    ExtensionActivationLifecycle,
    ExtensionDataLifecycle,
    ExtensionLifecycleRepository,
    ExtensionStatus,
    FrozenData,
    LifecycleAction,
    LifecycleConflictError,
    LifecycleOperation,
    LifecyclePhase,
    LifecycleReceipt,
    LifecycleRequest,
    SelectedInstallation,
    ValidatedActivation,
)


_DEFAULT_QUIESCE_DEADLINE_MS = 10_000
_MAX_QUIESCE_DEADLINE_MS = 120_000
_RECOVERY_PAGE_SIZE = 256
_T = TypeVar("_T")


class LifecycleServiceContractError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid extension lifecycle service contract")


class LifecycleExecutionConflictError(LifecycleConflictError):
    """The held engine lease already has an execution owner for this operation."""


@runtime_checkable
class HeldExclusiveEngineLease(Protocol):
    """Composition-owned proof that this process holds the repository's DB lease.

    Implementations raise when the lease is absent or no longer held. The service
    intentionally does not discover lock files, database paths, or process state.
    """

    def assert_held_for(self, repository: ExtensionLifecycleRepository) -> None: ...

    def execution_owner(
        self,
        repository: ExtensionLifecycleRepository,
        operation_id: str,
    ) -> AbstractContextManager[None]:
        """Return shared ownership across every service wrapper for this DB.

        Entering must reject an operation ID already executing under this lease.
        Implementations must not use a service-instance-local lock.
        """
        ...


class ExtensionLifecycleService:
    """Runs one admitted extension operation through durable lifecycle phases.

    ``execute`` starts only newly admitted work. Replays and pending claims return
    without effects. Restart or operator recovery is explicit through
    ``recover_operation``, which reloads the durable pending operation before
    resuming it.
    """

    def __init__(
        self,
        *,
        repository: ExtensionLifecycleRepository,
        data_lifecycle: ExtensionDataLifecycle,
        activation_lifecycle: ExtensionActivationLifecycle,
        engine_lease: HeldExclusiveEngineLease,
        quiesce_deadline_ms: int = _DEFAULT_QUIESCE_DEADLINE_MS,
    ) -> None:
        if not isinstance(repository, ExtensionLifecycleRepository):
            raise LifecycleServiceContractError()
        if not isinstance(data_lifecycle, ExtensionDataLifecycle):
            raise LifecycleServiceContractError()
        if not isinstance(activation_lifecycle, ExtensionActivationLifecycle):
            raise LifecycleServiceContractError()
        if not isinstance(engine_lease, HeldExclusiveEngineLease):
            raise LifecycleServiceContractError()
        if (
            type(quiesce_deadline_ms) is not int
            or quiesce_deadline_ms <= 0
            or quiesce_deadline_ms > _MAX_QUIESCE_DEADLINE_MS
        ):
            raise LifecycleServiceContractError()
        self._repository = repository
        self._data_lifecycle = data_lifecycle
        self._activation_lifecycle = activation_lifecycle
        self._engine_lease = engine_lease
        self._quiesce_deadline_ms = quiesce_deadline_ms

    def execute(self, request: LifecycleRequest) -> LifecycleReceipt | LifecycleOperation:
        """Claim and run new work; pending claims are never resumed implicitly."""
        if type(request) is not LifecycleRequest:
            raise LifecycleServiceContractError()
        with self._execution_owner(request.operation_id):
            claim = self._leased_call(self._repository.claim, request)
            if claim.disposition is ClaimDisposition.REPLAY:
                assert claim.receipt is not None
                return claim.receipt
            assert claim.operation is not None
            if claim.disposition is ClaimDisposition.IN_PROGRESS:
                return claim.operation
            return self._resume(claim.operation, resolution_ref=None)

    def list_pending(
        self,
        *,
        after_operation_id: str | None = None,
        limit: int = _RECOVERY_PAGE_SIZE,
    ) -> tuple[LifecycleOperation, ...]:
        """List durable pending operations without executing external effects."""
        return self._leased_call(
            self._repository.recover,
            after_operation_id=after_operation_id,
            limit=limit,
        )

    def recover_operation(
        self,
        operation: LifecycleOperation,
        *,
        resolution_ref: str | None = None,
    ) -> LifecycleReceipt | LifecycleOperation:
        """Explicitly resume the current durable phase of one pending operation.

        The supplied object identifies the operation. Its untrusted phase evidence
        is ignored: the service locates the pending record and then uses ``claim``
        to reload and validate its immutable request binding before any effect.
        """
        if type(operation) is not LifecycleOperation:
            raise LifecycleServiceContractError()
        operation_id = operation.request.operation_id
        with self._execution_owner(operation_id):
            durable = self._find_pending(operation_id)
            if durable is None:
                raise LifecycleExecutionConflictError()
            claim = self._leased_call(self._repository.claim, operation.request)
            if claim.disposition is not ClaimDisposition.IN_PROGRESS or claim.operation is None:
                raise LifecycleExecutionConflictError()
            current = claim.operation
            if current.request != durable.request or current.request.operation_id != operation_id:
                raise LifecycleExecutionConflictError()
            if (
                resolution_ref is not None
                and current.phase is not LifecyclePhase.RESOLUTION_REQUIRED
            ):
                raise LifecycleServiceContractError()
            return self._resume(current, resolution_ref=resolution_ref)

    def _find_pending(self, operation_id: str) -> LifecycleOperation | None:
        cursor: str | None = None
        while True:
            pending = self._leased_call(
                self._repository.recover,
                after_operation_id=cursor,
                limit=_RECOVERY_PAGE_SIZE,
            )
            for operation in pending:
                current_id = operation.request.operation_id
                if current_id == operation_id:
                    return operation
                if current_id > operation_id:
                    return None
            if len(pending) < _RECOVERY_PAGE_SIZE:
                return None
            cursor = pending[-1].request.operation_id

    def _resume(
        self,
        operation: LifecycleOperation,
        *,
        resolution_ref: str | None,
    ) -> LifecycleReceipt | LifecycleOperation:
        if operation.phase is LifecyclePhase.RESTORING:
            return self._restore_then_settle(operation)
        if operation.phase is LifecyclePhase.RESOLUTION_REQUIRED:
            if resolution_ref is None:
                return operation
            return self._resolve_then_restore(operation, resolution_ref)
        if operation.phase is LifecyclePhase.SWITCHED:
            return self._synchronize_then_settle(operation, runtime_activation=None)
        if operation.phase not in (
            LifecyclePhase.CLAIMED,
            LifecyclePhase.QUIESCED,
            LifecyclePhase.DATA_STAGED,
            LifecyclePhase.ACTIVATION_VALIDATED,
        ):
            raise LifecycleExecutionConflictError()

        current = operation
        runtime_activation: ValidatedActivation | None = None
        restoration_frozen = current.frozen_data
        try:
            if current.phase is LifecyclePhase.CLAIMED:
                previous = current.previous
                if previous is not None and current.request.action is not LifecycleAction.INSTALL:
                    self._leased_call(
                        self._activation_lifecycle.quiesce,
                        current.request.operation_id,
                        previous,
                        deadline_ms=self._quiesce_deadline_ms,
                    )
                    restoration_frozen = self._leased_call(
                        self._data_lifecycle.freeze,
                        current.request.operation_id,
                        previous.selected,
                    )
                current = self._advance(
                    current,
                    LifecyclePhase.QUIESCED,
                    frozen_data=restoration_frozen,
                )
            if current.phase is LifecyclePhase.QUIESCED:
                action = current.request.action
                if action in (LifecycleAction.INSTALL, LifecycleAction.UPDATE):
                    assert current.request.candidate is not None
                    staged = self._leased_call(
                        self._data_lifecycle.stage,
                        current.request.operation_id,
                        current.request.candidate,
                        current.frozen_data,
                    )
                    candidate = self._staged_selection(current, staged.data_ref)
                    current = self._advance(
                        current,
                        LifecyclePhase.DATA_STAGED,
                        candidate=candidate,
                        staged_data=staged,
                    )
                elif self._needs_activation(current):
                    candidate = self._activation_selection(current)
                    runtime_activation = self._leased_call(
                        self._activation_lifecycle.validate,
                        current.request.operation_id,
                        candidate,
                    )
                    current = self._advance(
                        current,
                        LifecyclePhase.ACTIVATION_VALIDATED,
                        candidate=candidate,
                        activation=runtime_activation,
                    )
                else:
                    current = self._switch(current)
            if current.phase is LifecyclePhase.DATA_STAGED:
                if self._needs_activation(current):
                    runtime_activation = self._leased_call(
                        self._activation_lifecycle.validate,
                        current.request.operation_id,
                        self._require_candidate(current),
                    )
                    current = self._advance(
                        current,
                        LifecyclePhase.ACTIVATION_VALIDATED,
                        activation=runtime_activation,
                    )
                else:
                    current = self._switch(current)
            if current.phase is LifecyclePhase.ACTIVATION_VALIDATED:
                if runtime_activation is None:
                    runtime_activation = self._leased_call(
                        self._activation_lifecycle.validate,
                        current.request.operation_id,
                        self._require_candidate(current),
                    )
                current = self._switch(current)
        except Exception:
            return self._abort_then_restore(
                current,
                runtime_activation,
                restoration_frozen=restoration_frozen,
            )
        return self._synchronize_then_settle(current, runtime_activation)

    def _staged_selection(
        self,
        operation: LifecycleOperation,
        data_ref: str,
    ) -> SelectedInstallation:
        candidate = operation.request.candidate
        assert candidate is not None
        previous = operation.previous
        if operation.request.action is LifecycleAction.INSTALL:
            approved_scopes: tuple[str, ...] = ()
        else:
            assert previous is not None
            requested = set(candidate.requested_scopes)
            approved_scopes = tuple(
                scope for scope in previous.selected.approved_scopes if scope in requested
            )
        if previous is None:
            grant_generation = 0
            activation_generation = 0
        else:
            grant_generation = previous.selected.grant_generation + 1
            activation_generation = previous.selected.activation_generation + 1
        return SelectedInstallation(
            executable=candidate,
            data_ref=data_ref,
            approved_scopes=approved_scopes,
            grant_generation=grant_generation,
            activation_generation=activation_generation,
        )

    def _activation_selection(self, operation: LifecycleOperation) -> SelectedInstallation:
        previous = operation.previous
        if previous is None:
            raise LifecycleExecutionConflictError()
        if operation.request.action is LifecycleAction.ENABLE:
            return replace(
                previous.selected,
                activation_generation=previous.selected.activation_generation + 1,
            )
        if operation.request.action is LifecycleAction.CHANGE_GRANTS:
            return replace(
                previous.selected,
                approved_scopes=operation.request.approved_scopes,
                grant_generation=previous.selected.grant_generation + 1,
                activation_generation=previous.selected.activation_generation + 1,
            )
        raise LifecycleExecutionConflictError()

    def _needs_activation(self, operation: LifecycleOperation) -> bool:
        if operation.request.action is LifecycleAction.ENABLE:
            return True
        return (
            operation.request.action in (LifecycleAction.UPDATE, LifecycleAction.CHANGE_GRANTS)
            and operation.previous is not None
            and operation.previous.status is ExtensionStatus.ENABLED
        )

    def _require_candidate(self, operation: LifecycleOperation) -> SelectedInstallation:
        if operation.candidate is None:
            raise LifecycleExecutionConflictError()
        return operation.candidate

    def _advance(
        self,
        operation: LifecycleOperation,
        phase: LifecyclePhase,
        **evidence: object,
    ) -> LifecycleOperation:
        updated = replace(
            operation,
            phase=phase,
            phase_revision=operation.phase_revision + 1,
            **evidence,
        )
        return self._leased_call(
            self._repository.advance,
            updated,
            expected_phase_revision=operation.phase_revision,
        )

    def _switch(self, operation: LifecycleOperation) -> LifecycleOperation:
        return self._leased_call(
            self._repository.switch,
            operation.request.operation_id,
            expected_phase_revision=operation.phase_revision,
        )

    def _abort_then_restore(
        self,
        operation: LifecycleOperation,
        runtime_activation: ValidatedActivation | None,
        *,
        restoration_frozen: FrozenData | None,
    ) -> LifecycleReceipt:
        revocation_ref = self._leased_call(
            self._activation_lifecycle.revoke,
            operation.request.operation_id,
            runtime_activation,
        )
        restoring = self._leased_call(
            self._repository.abort,
            operation.request.operation_id,
            expected_phase_revision=operation.phase_revision,
            revocation_ref=revocation_ref,
            frozen_data=restoration_frozen,
        )
        return self._restore_then_settle(
            restoring,
            fallback_frozen=restoration_frozen,
        )

    def _synchronize_then_settle(
        self,
        operation: LifecycleOperation,
        runtime_activation: ValidatedActivation | None,
    ) -> LifecycleReceipt:
        if operation.phase is not LifecyclePhase.SWITCHED:
            raise LifecycleExecutionConflictError()
        intended = operation.intended_receipt
        if intended is None or intended.record is None:
            raise LifecycleExecutionConflictError()
        record = intended.record
        try:
            if record.status is ExtensionStatus.ENABLED and runtime_activation is None:
                runtime_activation = self._leased_call(
                    self._activation_lifecycle.validate,
                    operation.request.operation_id,
                    record.selected,
                )
            self._admit_record(
                operation.request.operation_id,
                record,
                runtime_activation,
            )
        except Exception:
            return self._rollback_then_restore(operation, runtime_activation, resolution_ref=None)
        return self._leased_call(
            self._repository.settle,
            operation.request.operation_id,
            expected_phase_revision=operation.phase_revision,
        )

    def _rollback_then_restore(
        self,
        operation: LifecycleOperation,
        runtime_activation: ValidatedActivation | None,
        *,
        resolution_ref: str | None,
    ) -> LifecycleReceipt:
        intended = operation.intended_receipt
        if intended is None or intended.record is None:
            raise LifecycleExecutionConflictError()
        current_data = self._leased_call(
            self._data_lifecycle.freeze,
            operation.request.operation_id,
            intended.record.selected,
        )
        revocation_ref = self._leased_call(
            self._activation_lifecycle.revoke,
            operation.request.operation_id,
            runtime_activation,
        )
        restoring = self._leased_call(
            self._repository.rollback,
            operation.request.operation_id,
            expected_phase_revision=operation.phase_revision,
            revocation_ref=revocation_ref,
            current_data=current_data,
            resolution_ref=resolution_ref,
        )
        return self._restore_then_settle(restoring)

    def _resolve_then_restore(
        self,
        operation: LifecycleOperation,
        resolution_ref: str,
    ) -> LifecycleReceipt:
        return self._rollback_then_restore(
            operation,
            runtime_activation=None,
            resolution_ref=resolution_ref,
        )

    def _restore_then_settle(
        self,
        operation: LifecycleOperation,
        *,
        fallback_frozen: FrozenData | None = None,
    ) -> LifecycleReceipt:
        if operation.phase is not LifecyclePhase.RESTORING:
            raise LifecycleExecutionConflictError()
        intended = operation.intended_receipt
        if intended is None:
            raise LifecycleExecutionConflictError()
        restored = intended.record
        if restored is not None:
            frozen = operation.frozen_data or fallback_frozen
            if (
                restored.status is not ExtensionStatus.REMOVED
                and frozen is not None
                and frozen.data_ref == restored.selected.data_ref
            ):
                self._leased_call(
                    self._data_lifecycle.thaw,
                    operation.request.operation_id,
                    frozen,
                )
            runtime_activation: ValidatedActivation | None = None
            if restored.status is ExtensionStatus.ENABLED:
                runtime_activation = self._leased_call(
                    self._activation_lifecycle.validate,
                    operation.request.operation_id,
                    restored.selected,
                )
            self._admit_record(
                operation.request.operation_id,
                restored,
                runtime_activation,
            )
        return self._leased_call(
            self._repository.settle,
            operation.request.operation_id,
            expected_phase_revision=operation.phase_revision,
        )

    def _admit_record(
        self,
        operation_id: str,
        record: ExtensionRecord,
        activation: ValidatedActivation | None,
    ) -> None:
        if record.status is ExtensionStatus.ENABLED:
            if activation is None or activation.selection != record.selected:
                raise LifecycleExecutionConflictError()
            expected_data_revision = activation.validated_data_revision
        else:
            if activation is not None:
                raise LifecycleExecutionConflictError()
            expected_data_revision = self._leased_call(
                self._data_lifecycle.selected_revision,
                record.selected,
            )
        self._leased_call(
            self._activation_lifecycle.admit,
            operation_id,
            record,
            activation,
            expected_data_revision=expected_data_revision,
        )

    def _leased_call(self, callback: Callable[..., _T], *args: object, **kwargs: object) -> _T:
        self._engine_lease.assert_held_for(self._repository)
        return callback(*args, **kwargs)

    def _execution_owner(self, operation_id: str) -> AbstractContextManager[None]:
        self._engine_lease.assert_held_for(self._repository)
        return self._engine_lease.execution_owner(self._repository, operation_id)
