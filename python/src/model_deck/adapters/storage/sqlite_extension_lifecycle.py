"""SQLite persistence for the extension installation lifecycle.

The repository records lifecycle decisions and evidence only. Process, data,
authority, and admission effects are completed by their owning adapters before
the corresponding repository call.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from model_deck.engine.extensions.ports import (
    ClaimDisposition,
    ExecutableArtifact,
    ExtensionRecord,
    ExtensionStatus,
    FrozenData,
    LifecycleAction,
    LifecycleClaim,
    LifecycleConflictError,
    LifecycleContractError,
    LifecycleIdempotencyConflictError,
    LifecycleOperation,
    LifecyclePhase,
    LifecycleReceipt,
    LifecycleRequest,
    LifecycleResolutionRequiredError,
    ReceiptOutcome,
    SelectedInstallation,
    StagedData,
    ValidatedActivation,
    allowed_next_phases,
)
from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS extension_lifecycle_records (
    extension_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    record_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extension_lifecycle_operations (
    operation_id TEXT PRIMARY KEY,
    extension_id TEXT NOT NULL,
    principal_ref TEXT NOT NULL,
    action TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    phase TEXT NOT NULL,
    phase_revision INTEGER NOT NULL,
    operation_json TEXT NOT NULL,
    revocation_ref TEXT,
    resolution_ref TEXT,
    rollback_freeze_ref TEXT,
    rollback_data_ref TEXT,
    rollback_data_revision INTEGER,
    pending INTEGER NOT NULL CHECK (pending IN (0, 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS extension_lifecycle_pending_extension
ON extension_lifecycle_operations(extension_id) WHERE pending = 1;

CREATE UNIQUE INDEX IF NOT EXISTS extension_lifecycle_pending_key
ON extension_lifecycle_operations(principal_ref, action, idempotency_key)
WHERE pending = 1;

CREATE TABLE IF NOT EXISTS extension_lifecycle_receipts (
    principal_ref TEXT NOT NULL,
    action TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    receipt_json TEXT NOT NULL,
    PRIMARY KEY (principal_ref, action, idempotency_key)
);
"""


class SQLiteExtensionLifecycleRepository:
    """Atomic lifecycle claims, phase CAS, selection switches, and receipts."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)

    def get(self, extension_id: str) -> ExtensionRecord | None:
        _validate_wire("reverse_domain_id", extension_id)
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            record = self._load_record(connection, extension_id)
            if record is None or record.status is ExtensionStatus.REMOVED:
                return None
            return record
        finally:
            connection.close()

    def claim(self, request: LifecycleRequest) -> LifecycleClaim:
        if type(request) is not LifecycleRequest:
            raise LifecycleContractError()
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            receipt_row = connection.execute(
                "SELECT request_digest, receipt_json FROM extension_lifecycle_receipts "
                "WHERE principal_ref = ? AND action = ? AND idempotency_key = ?",
                (request.principal_ref, request.action.value, request.idempotency_key),
            ).fetchone()
            if receipt_row is not None:
                self._require_matching_digest(receipt_row[0], request.request_digest)
                connection.commit()
                return LifecycleClaim(
                    ClaimDisposition.REPLAY,
                    receipt=_receipt_from_json(receipt_row[1]),
                )

            pending_row = connection.execute(
                "SELECT request_digest, operation_json FROM extension_lifecycle_operations "
                "WHERE principal_ref = ? AND action = ? AND idempotency_key = ? AND pending = 1",
                (request.principal_ref, request.action.value, request.idempotency_key),
            ).fetchone()
            if pending_row is not None:
                self._require_matching_digest(pending_row[0], request.request_digest)
                connection.commit()
                return LifecycleClaim(
                    ClaimDisposition.IN_PROGRESS,
                    operation=_operation_from_json(pending_row[1]),
                )

            if connection.execute(
                "SELECT 1 FROM extension_lifecycle_operations WHERE operation_id = ?",
                (request.operation_id,),
            ).fetchone() is not None:
                raise LifecycleConflictError("operation id already exists")
            if connection.execute(
                "SELECT 1 FROM extension_lifecycle_operations "
                "WHERE extension_id = ? AND pending = 1",
                (request.extension_id,),
            ).fetchone() is not None:
                raise LifecycleConflictError("extension already has an active lifecycle claim")

            previous = self._load_record(connection, request.extension_id)
            self._validate_admission(request, previous)
            operation = LifecycleOperation(
                request=request,
                phase=LifecyclePhase.CLAIMED,
                phase_revision=0,
                previous=previous,
            )
            connection.execute(
                "INSERT INTO extension_lifecycle_operations "
                "(operation_id, extension_id, principal_ref, action, idempotency_key, "
                "request_digest, phase, phase_revision, operation_json, pending) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    request.operation_id,
                    request.extension_id,
                    request.principal_ref,
                    request.action.value,
                    request.idempotency_key,
                    request.request_digest,
                    operation.phase.value,
                    operation.phase_revision,
                    _operation_to_json(operation),
                ),
            )
            connection.commit()
            return LifecycleClaim(ClaimDisposition.ADMITTED, operation=operation)
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise LifecycleConflictError("lifecycle claim conflicted") from error
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def advance(
        self,
        operation: LifecycleOperation,
        *,
        expected_phase_revision: int,
    ) -> LifecycleOperation:
        if type(operation) is not LifecycleOperation:
            raise LifecycleContractError()
        _validate_revision(expected_phase_revision)
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            current = self._load_pending_operation(connection, operation.request.operation_id)
            self._require_phase_revision(current, expected_phase_revision)
            self._validate_advance(current, operation)
            self._store_operation(connection, operation)
            connection.commit()
            return operation
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def switch(
        self,
        operation_id: str,
        *,
        expected_phase_revision: int,
    ) -> LifecycleOperation:
        _validate_wire("uuid", operation_id)
        _validate_revision(expected_phase_revision)
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            current = self._load_pending_operation(connection, operation_id)
            self._require_phase_revision(current, expected_phase_revision)
            previous_status = current.previous.status if current.previous is not None else None
            if LifecyclePhase.SWITCHED not in allowed_next_phases(
                current.request.action,
                current.phase,
                previous_status=previous_status,
            ):
                raise LifecycleConflictError("operation is not ready to switch")
            self._require_selected_state(connection, current.previous, current.request.extension_id)
            selected = self._selection_for_switch(current)
            revision = 1 if current.previous is None else current.previous.revision + 1
            record = ExtensionRecord(
                extension_id=current.request.extension_id,
                revision=revision,
                status=self._status_for_switch(current),
                selected=selected,
            )
            receipt = LifecycleReceipt(current.request, record, ReceiptOutcome.APPLIED)
            switched = replace(
                current,
                phase=LifecyclePhase.SWITCHED,
                phase_revision=current.phase_revision + 1,
                candidate=selected,
                intended_receipt=receipt,
            )
            self._store_record(connection, record)
            self._store_operation(connection, switched)
            connection.commit()
            return switched
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def settle(
        self,
        operation_id: str,
        *,
        expected_phase_revision: int,
    ) -> LifecycleReceipt:
        _validate_wire("uuid", operation_id)
        _validate_revision(expected_phase_revision)
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            current = self._load_pending_operation(connection, operation_id)
            self._require_phase_revision(current, expected_phase_revision)
            receipt = current.intended_receipt
            if receipt is None:
                raise LifecycleConflictError("operation has no intended receipt")
            if current.phase is LifecyclePhase.SWITCHED:
                terminal_phase = LifecyclePhase.SETTLED
                if receipt.outcome is not ReceiptOutcome.APPLIED:
                    raise LifecycleConflictError("switched operation has invalid outcome")
            elif current.phase is LifecyclePhase.RESTORING:
                terminal_phase = (
                    LifecyclePhase.ABORTED
                    if receipt.outcome is ReceiptOutcome.ABORTED
                    else LifecyclePhase.ROLLED_BACK
                )
                if receipt.outcome not in (ReceiptOutcome.ABORTED, ReceiptOutcome.ROLLED_BACK):
                    raise LifecycleConflictError("restoring operation has invalid outcome")
            else:
                raise LifecycleConflictError("operation is not ready to settle")
            terminal = replace(
                current,
                phase=terminal_phase,
                phase_revision=current.phase_revision + 1,
            )
            try:
                connection.execute(
                    "INSERT INTO extension_lifecycle_receipts "
                    "(principal_ref, action, idempotency_key, request_digest, operation_id, receipt_json) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        current.request.principal_ref,
                        current.request.action.value,
                        current.request.idempotency_key,
                        current.request.request_digest,
                        current.request.operation_id,
                        _receipt_to_json(receipt),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise LifecycleConflictError("lifecycle receipt already exists") from error
            self._store_operation(connection, terminal, pending=False)
            connection.commit()
            return receipt
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def abort(
        self,
        operation_id: str,
        *,
        expected_phase_revision: int,
        revocation_ref: str,
        frozen_data: FrozenData | None = None,
    ) -> LifecycleOperation:
        _validate_wire("uuid", operation_id)
        _validate_revision(expected_phase_revision)
        _validate_ref(revocation_ref)
        if frozen_data is not None and type(frozen_data) is not FrozenData:
            raise LifecycleContractError()
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            current = self._load_pending_operation(connection, operation_id)
            self._require_phase_revision(current, expected_phase_revision)
            if current.phase not in (
                LifecyclePhase.CLAIMED,
                LifecyclePhase.QUIESCED,
                LifecyclePhase.DATA_STAGED,
                LifecyclePhase.ACTIVATION_VALIDATED,
            ):
                raise LifecycleConflictError("only a pre-switch operation can abort")
            persisted_frozen = current.frozen_data
            if frozen_data is not None:
                previous = current.previous
                if (
                    current.request.action is LifecycleAction.INSTALL
                    or previous is None
                    or frozen_data.data_ref != previous.selected.data_ref
                ):
                    raise LifecycleConflictError(
                        "abort freeze does not match the prior selection"
                    )
                if persisted_frozen is not None and frozen_data != persisted_frozen:
                    raise LifecycleConflictError(
                        "abort cannot change persisted freeze evidence"
                    )
                persisted_frozen = frozen_data
            receipt = LifecycleReceipt(current.request, current.previous, ReceiptOutcome.ABORTED)
            restoring = replace(
                current,
                phase=LifecyclePhase.RESTORING,
                phase_revision=current.phase_revision + 1,
                frozen_data=persisted_frozen,
                intended_receipt=receipt,
            )
            self._store_operation(connection, restoring, revocation_ref=revocation_ref)
            connection.commit()
            return restoring
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def rollback(
        self,
        operation_id: str,
        *,
        expected_phase_revision: int,
        revocation_ref: str,
        current_data: FrozenData,
        resolution_ref: str | None = None,
    ) -> LifecycleOperation:
        _validate_wire("uuid", operation_id)
        _validate_revision(expected_phase_revision)
        _validate_ref(revocation_ref)
        if type(current_data) is not FrozenData:
            raise LifecycleContractError()
        if resolution_ref is not None:
            _validate_ref(resolution_ref)
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            current = self._load_pending_operation(connection, operation_id)
            self._require_phase_revision(current, expected_phase_revision)
            if current.phase not in (LifecyclePhase.SWITCHED, LifecyclePhase.RESOLUTION_REQUIRED):
                raise LifecycleConflictError("only a switched operation can roll back")
            applied = current.intended_receipt
            if applied is None or applied.outcome is not ReceiptOutcome.APPLIED or applied.record is None:
                raise LifecycleConflictError("switched outcome is missing")
            self._require_selected_state(connection, applied.record, current.request.extension_id)
            if current_data.data_ref != applied.record.selected.data_ref:
                raise LifecycleConflictError("current data does not belong to selected installation")

            baseline = self._rollback_baseline(current)
            resolution_is_required = (
                current.phase is LifecyclePhase.RESOLUTION_REQUIRED
                or current_data.final_revision != baseline
            )
            if resolution_is_required and resolution_ref is None:
                if current.phase is LifecyclePhase.SWITCHED:
                    resolution_required = replace(
                        current,
                        phase=LifecyclePhase.RESOLUTION_REQUIRED,
                        phase_revision=current.phase_revision + 1,
                    )
                    self._store_operation(
                        connection,
                        resolution_required,
                        revocation_ref=revocation_ref,
                        rollback_data=current_data,
                    )
                    connection.commit()
                else:
                    connection.rollback()
                raise LifecycleResolutionRequiredError(
                    "rollback requires an explicit data resolution"
                )

            restored = self._restored_record(current, applied.record)
            receipt = LifecycleReceipt(current.request, restored, ReceiptOutcome.ROLLED_BACK)
            restoring = replace(
                current,
                phase=LifecyclePhase.RESTORING,
                phase_revision=current.phase_revision + 1,
                intended_receipt=receipt,
            )
            self._store_record(connection, restored)
            self._store_operation(
                connection,
                restoring,
                revocation_ref=revocation_ref,
                resolution_ref=resolution_ref,
                rollback_data=current_data,
            )
            connection.commit()
            return restoring
        except LifecycleResolutionRequiredError:
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover(
        self,
        *,
        after_operation_id: str | None = None,
        limit: int = 256,
    ) -> tuple[LifecycleOperation, ...]:
        if after_operation_id is not None:
            _validate_wire("uuid", after_operation_id)
        if type(limit) is not int or not 1 <= limit <= 256:
            raise LifecycleContractError()
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            rows = connection.execute(
                "SELECT operation_json FROM extension_lifecycle_operations "
                "WHERE pending = 1 AND operation_id > ? ORDER BY operation_id LIMIT ?",
                (after_operation_id or "", limit),
            ).fetchall()
            return tuple(_operation_from_json(row[0]) for row in rows)
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(_SCHEMA_SQL)

    @staticmethod
    def _require_matching_digest(stored: str, supplied: str) -> None:
        if stored != supplied:
            raise LifecycleIdempotencyConflictError(
                "idempotency key reused with a different request digest"
            )

    @staticmethod
    def _validate_admission(
        request: LifecycleRequest,
        current: ExtensionRecord | None,
    ) -> None:
        if request.action is LifecycleAction.INSTALL:
            if current is not None and current.status is not ExtensionStatus.REMOVED:
                raise LifecycleConflictError("extension is already installed")
        elif current is None or current.status is ExtensionStatus.REMOVED:
            raise LifecycleConflictError("extension is not installed")
        current_revision = 0 if current is None else current.revision
        if current_revision != request.expected_revision:
            raise LifecycleConflictError("stale extension revision")
        if request.action is LifecycleAction.CHANGE_GRANTS:
            assert current is not None
            if not set(request.approved_scopes).issubset(
                current.selected.executable.requested_scopes
            ):
                raise LifecycleContractError()

    @staticmethod
    def _require_phase_revision(
        operation: LifecycleOperation,
        expected_phase_revision: int,
    ) -> None:
        if operation.phase_revision != expected_phase_revision:
            raise LifecycleConflictError("stale lifecycle phase revision")

    def _validate_advance(
        self,
        current: LifecycleOperation,
        proposed: LifecycleOperation,
    ) -> None:
        if proposed.request != current.request or proposed.previous != current.previous:
            raise LifecycleConflictError("immutable lifecycle binding changed")
        if proposed.phase_revision != current.phase_revision + 1:
            raise LifecycleConflictError("next phase revision is not monotonic")
        previous_status = current.previous.status if current.previous is not None else None
        if proposed.phase not in allowed_next_phases(
            current.request.action,
            current.phase,
            previous_status=previous_status,
        ):
            raise LifecycleConflictError("illegal lifecycle phase transition")
        if proposed.phase in (
            LifecyclePhase.SWITCHED,
            LifecyclePhase.RESTORING,
            LifecyclePhase.SETTLED,
            LifecyclePhase.ABORTED,
            LifecyclePhase.ROLLED_BACK,
            LifecyclePhase.RESOLUTION_REQUIRED,
        ):
            raise LifecycleConflictError("phase requires its dedicated repository method")
        for existing, replacement_value in (
            (current.candidate, proposed.candidate),
            (current.frozen_data, proposed.frozen_data),
            (current.staged_data, proposed.staged_data),
            (current.activation, proposed.activation),
        ):
            if existing is not None and replacement_value != existing:
                raise LifecycleConflictError("persisted lifecycle evidence changed")
        if proposed.intended_receipt is not None:
            raise LifecycleConflictError("advance cannot attach an intended receipt")

        if proposed.phase is LifecyclePhase.QUIESCED:
            if current.phase is not LifecyclePhase.CLAIMED:
                raise LifecycleConflictError("quiesce must follow claim")
            needs_frozen = current.request.action is not LifecycleAction.INSTALL
            if needs_frozen:
                if current.previous is None or proposed.frozen_data is None:
                    raise LifecycleConflictError("quiesce is missing frozen data")
                if proposed.frozen_data.data_ref != current.previous.selected.data_ref:
                    raise LifecycleConflictError("frozen data does not match prior selection")
            elif proposed.frozen_data is not None:
                raise LifecycleConflictError("install cannot claim prior frozen data")
            if any(
                value is not None
                for value in (proposed.candidate, proposed.staged_data, proposed.activation)
            ):
                raise LifecycleConflictError("quiesce contains premature evidence")
            return

        if proposed.phase is LifecyclePhase.DATA_STAGED:
            if proposed.staged_data is None:
                raise LifecycleConflictError("staged phase is missing data evidence")
            if proposed.frozen_data != current.frozen_data:
                raise LifecycleConflictError("staging changed frozen-data evidence")
            expected_candidate = self._candidate_for_staged_data(current, proposed.staged_data)
            if proposed.candidate != expected_candidate or proposed.activation is not None:
                raise LifecycleConflictError("staged candidate binding is invalid")
            return

        if proposed.phase is LifecyclePhase.ACTIVATION_VALIDATED:
            candidate = proposed.candidate
            activation = proposed.activation
            if candidate is None or activation is None or activation.selection != candidate:
                raise LifecycleConflictError("activation evidence is incomplete")
            if proposed.frozen_data != current.frozen_data:
                raise LifecycleConflictError("activation changed frozen-data evidence")
            if proposed.staged_data != current.staged_data:
                raise LifecycleConflictError("activation changed staged-data evidence")
            expected_candidate = self._candidate_for_activation(current, proposed)
            if candidate != expected_candidate:
                raise LifecycleConflictError("validated candidate binding is invalid")
            baseline = (
                proposed.staged_data.initial_revision
                if proposed.staged_data is not None
                else proposed.frozen_data.final_revision
                if proposed.frozen_data is not None
                else None
            )
            if baseline is None or activation.validated_data_revision < baseline:
                raise LifecycleConflictError("activation data revision predates its baseline")

    @staticmethod
    def _candidate_for_staged_data(
        operation: LifecycleOperation,
        staged_data: StagedData,
    ) -> SelectedInstallation:
        artifact = operation.request.candidate
        if artifact is None:
            raise LifecycleConflictError("staged action has no candidate artifact")
        previous = operation.previous
        if operation.request.action is LifecycleAction.INSTALL:
            grant_generation = 0 if previous is None else previous.selected.grant_generation + 1
            activation_generation = (
                0 if previous is None else previous.selected.activation_generation + 1
            )
            approved_scopes: tuple[str, ...] = ()
        elif operation.request.action is LifecycleAction.UPDATE and previous is not None:
            grant_generation = previous.selected.grant_generation + 1
            activation_generation = previous.selected.activation_generation + 1
            approved_scopes = tuple(
                scope
                for scope in previous.selected.approved_scopes
                if scope in artifact.requested_scopes
            )
        else:
            raise LifecycleConflictError("action cannot stage data")
        return SelectedInstallation(
            executable=artifact,
            data_ref=staged_data.data_ref,
            approved_scopes=approved_scopes,
            grant_generation=grant_generation,
            activation_generation=activation_generation,
        )

    def _candidate_for_activation(
        self,
        current: LifecycleOperation,
        proposed: LifecycleOperation,
    ) -> SelectedInstallation:
        if current.request.action is LifecycleAction.UPDATE:
            if current.candidate is None:
                raise LifecycleConflictError("update activation has no staged candidate")
            return current.candidate
        previous = current.previous
        if previous is None:
            raise LifecycleConflictError("activation has no prior selection")
        if current.request.action is LifecycleAction.ENABLE:
            return replace(
                previous.selected,
                activation_generation=previous.selected.activation_generation + 1,
            )
        if current.request.action is LifecycleAction.CHANGE_GRANTS:
            return replace(
                previous.selected,
                approved_scopes=current.request.approved_scopes,
                grant_generation=previous.selected.grant_generation + 1,
                activation_generation=previous.selected.activation_generation + 1,
            )
        raise LifecycleConflictError("action cannot validate activation")

    def _selection_for_switch(self, operation: LifecycleOperation) -> SelectedInstallation:
        action = operation.request.action
        previous = operation.previous
        if action in (LifecycleAction.INSTALL, LifecycleAction.UPDATE):
            if operation.candidate is None or operation.staged_data is None:
                raise LifecycleConflictError("switch is missing staged selection")
            expected = self._candidate_for_staged_data(operation, operation.staged_data)
            if operation.candidate != expected:
                raise LifecycleConflictError("staged selection changed before switch")
            if previous is not None and previous.status is ExtensionStatus.ENABLED:
                if operation.activation is None:
                    raise LifecycleConflictError("enabled update is missing activation validation")
            return operation.candidate
        if previous is None:
            raise LifecycleConflictError("switch is missing prior selection")
        if action is LifecycleAction.ENABLE:
            expected = replace(
                previous.selected,
                activation_generation=previous.selected.activation_generation + 1,
            )
            if operation.candidate != expected or operation.activation is None:
                raise LifecycleConflictError("enable is missing validated activation")
            return expected
        if action is LifecycleAction.DISABLE:
            return replace(
                previous.selected,
                activation_generation=previous.selected.activation_generation + 1,
            )
        if action is LifecycleAction.CHANGE_GRANTS:
            activation_increment = 1 if previous.status is ExtensionStatus.ENABLED else 0
            expected = replace(
                previous.selected,
                approved_scopes=operation.request.approved_scopes,
                grant_generation=previous.selected.grant_generation + 1,
                activation_generation=(
                    previous.selected.activation_generation + activation_increment
                ),
            )
            if previous.status is ExtensionStatus.ENABLED:
                if operation.candidate != expected or operation.activation is None:
                    raise LifecycleConflictError("grant change is missing validated activation")
            return expected
        if action is LifecycleAction.REMOVE:
            return replace(
                previous.selected,
                grant_generation=previous.selected.grant_generation + 1,
                activation_generation=previous.selected.activation_generation + 1,
            )
        raise LifecycleConflictError("unsupported lifecycle action")

    @staticmethod
    def _status_for_switch(operation: LifecycleOperation) -> ExtensionStatus:
        action = operation.request.action
        if action is LifecycleAction.INSTALL:
            return ExtensionStatus.INSTALLED
        if action is LifecycleAction.ENABLE:
            return ExtensionStatus.ENABLED
        if action is LifecycleAction.DISABLE:
            return ExtensionStatus.DISABLED
        if action is LifecycleAction.REMOVE:
            return ExtensionStatus.REMOVED
        if operation.previous is None:
            raise LifecycleConflictError("mutation is missing prior status")
        return operation.previous.status

    @staticmethod
    def _rollback_baseline(operation: LifecycleOperation) -> int:
        if operation.activation is not None:
            return operation.activation.validated_data_revision
        if operation.staged_data is not None:
            return operation.staged_data.initial_revision
        if operation.frozen_data is not None:
            return operation.frozen_data.final_revision
        raise LifecycleConflictError("switched operation has no rollback baseline")

    @staticmethod
    def _restored_record(
        operation: LifecycleOperation,
        current: ExtensionRecord,
    ) -> ExtensionRecord:
        if operation.previous is None:
            selected = replace(
                current.selected,
                grant_generation=current.selected.grant_generation + 1,
                activation_generation=current.selected.activation_generation + 1,
            )
            status = ExtensionStatus.REMOVED
        else:
            selected = replace(
                operation.previous.selected,
                grant_generation=current.selected.grant_generation + 1,
                activation_generation=current.selected.activation_generation + 1,
            )
            status = operation.previous.status
        return ExtensionRecord(
            extension_id=current.extension_id,
            revision=current.revision + 1,
            status=status,
            selected=selected,
        )

    @staticmethod
    def _require_selected_state(
        connection: sqlite3.Connection,
        expected: ExtensionRecord | None,
        extension_id: str,
    ) -> None:
        actual = SQLiteExtensionLifecycleRepository._load_record(connection, extension_id)
        if actual != expected:
            raise LifecycleConflictError("selected extension state changed")

    @staticmethod
    def _load_record(
        connection: sqlite3.Connection,
        extension_id: str,
    ) -> ExtensionRecord | None:
        row = connection.execute(
            "SELECT record_json FROM extension_lifecycle_records WHERE extension_id = ?",
            (extension_id,),
        ).fetchone()
        return None if row is None else _record_from_dict(json.loads(row[0]))

    @staticmethod
    def _store_record(connection: sqlite3.Connection, record: ExtensionRecord) -> None:
        connection.execute(
            "INSERT INTO extension_lifecycle_records (extension_id, revision, status, record_json) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(extension_id) DO UPDATE SET "
            "revision = excluded.revision, status = excluded.status, record_json = excluded.record_json",
            (
                record.extension_id,
                record.revision,
                record.status.value,
                _canonical_json(_record_to_dict(record)),
            ),
        )

    @staticmethod
    def _load_pending_operation(
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> LifecycleOperation:
        row = connection.execute(
            "SELECT operation_json FROM extension_lifecycle_operations "
            "WHERE operation_id = ? AND pending = 1",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise LifecycleConflictError("pending lifecycle operation not found")
        return _operation_from_json(row[0])

    @staticmethod
    def _store_operation(
        connection: sqlite3.Connection,
        operation: LifecycleOperation,
        *,
        pending: bool = True,
        revocation_ref: str | None = None,
        resolution_ref: str | None = None,
        rollback_data: FrozenData | None = None,
    ) -> None:
        cursor = connection.execute(
            "UPDATE extension_lifecycle_operations SET phase = ?, phase_revision = ?, "
            "operation_json = ?, pending = ?, "
            "revocation_ref = COALESCE(?, revocation_ref), "
            "resolution_ref = COALESCE(?, resolution_ref), "
            "rollback_freeze_ref = COALESCE(?, rollback_freeze_ref), "
            "rollback_data_ref = COALESCE(?, rollback_data_ref), "
            "rollback_data_revision = COALESCE(?, rollback_data_revision) "
            "WHERE operation_id = ? AND pending = 1",
            (
                operation.phase.value,
                operation.phase_revision,
                _operation_to_json(operation),
                1 if pending else 0,
                revocation_ref,
                resolution_ref,
                None if rollback_data is None else rollback_data.freeze_ref,
                None if rollback_data is None else rollback_data.data_ref,
                None if rollback_data is None else rollback_data.final_revision,
                operation.request.operation_id,
            ),
        )
        if cursor.rowcount != 1:
            raise LifecycleConflictError("pending lifecycle operation changed")


def _validate_revision(value: object) -> None:
    if type(value) is not int or value < 0:
        raise LifecycleContractError()


def _validate_wire(kind: str, value: object) -> None:
    try:
        validate_schema_ref(f"contracts/common/types.schema.json#/definitions/{kind}", value)
    except SchemaValidationError:
        raise LifecycleContractError() from None


def _validate_ref(value: object) -> None:
    if type(value) is not str or len(value) > 128:
        raise LifecycleContractError()
    _validate_wire("opaque_ref", value)


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _artifact_to_dict(artifact: ExecutableArtifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "extension_id": artifact.extension_id,
        "version": artifact.version,
        "requested_scopes": list(artifact.requested_scopes),
    }


def _artifact_from_dict(value: dict[str, Any]) -> ExecutableArtifact:
    return ExecutableArtifact(
        artifact_id=value["artifact_id"],
        extension_id=value["extension_id"],
        version=value["version"],
        requested_scopes=tuple(value["requested_scopes"]),
    )


def _selection_to_dict(selection: SelectedInstallation) -> dict[str, Any]:
    return {
        "executable": _artifact_to_dict(selection.executable),
        "data_ref": selection.data_ref,
        "approved_scopes": list(selection.approved_scopes),
        "grant_generation": selection.grant_generation,
        "activation_generation": selection.activation_generation,
    }


def _selection_from_dict(value: dict[str, Any]) -> SelectedInstallation:
    return SelectedInstallation(
        executable=_artifact_from_dict(value["executable"]),
        data_ref=value["data_ref"],
        approved_scopes=tuple(value["approved_scopes"]),
        grant_generation=value["grant_generation"],
        activation_generation=value["activation_generation"],
    )


def _record_to_dict(record: ExtensionRecord) -> dict[str, Any]:
    return {
        "extension_id": record.extension_id,
        "revision": record.revision,
        "status": record.status.value,
        "selected": _selection_to_dict(record.selected),
    }


def _record_from_dict(value: dict[str, Any]) -> ExtensionRecord:
    return ExtensionRecord(
        extension_id=value["extension_id"],
        revision=value["revision"],
        status=ExtensionStatus(value["status"]),
        selected=_selection_from_dict(value["selected"]),
    )


def _request_to_dict(request: LifecycleRequest) -> dict[str, Any]:
    return {
        "operation_id": request.operation_id,
        "principal_ref": request.principal_ref,
        "action": request.action.value,
        "extension_id": request.extension_id,
        "expected_revision": request.expected_revision,
        "request_digest": request.request_digest,
        "idempotency_key": request.idempotency_key,
        "candidate": (
            None if request.candidate is None else _artifact_to_dict(request.candidate)
        ),
        "approved_scopes": list(request.approved_scopes),
    }


def _request_from_dict(value: dict[str, Any]) -> LifecycleRequest:
    candidate = value["candidate"]
    return LifecycleRequest(
        operation_id=value["operation_id"],
        principal_ref=value["principal_ref"],
        action=LifecycleAction(value["action"]),
        extension_id=value["extension_id"],
        expected_revision=value["expected_revision"],
        request_digest=value["request_digest"],
        idempotency_key=value["idempotency_key"],
        candidate=None if candidate is None else _artifact_from_dict(candidate),
        approved_scopes=tuple(value["approved_scopes"]),
    )


def _frozen_to_dict(value: FrozenData) -> dict[str, Any]:
    return {
        "freeze_ref": value.freeze_ref,
        "data_ref": value.data_ref,
        "final_revision": value.final_revision,
    }


def _frozen_from_dict(value: dict[str, Any]) -> FrozenData:
    return FrozenData(value["freeze_ref"], value["data_ref"], value["final_revision"])


def _staged_to_dict(value: StagedData) -> dict[str, Any]:
    return {
        "data_ref": value.data_ref,
        "initial_revision": value.initial_revision,
        "migration_receipt_ref": value.migration_receipt_ref,
    }


def _staged_from_dict(value: dict[str, Any]) -> StagedData:
    return StagedData(
        value["data_ref"],
        value["initial_revision"],
        value["migration_receipt_ref"],
    )


def _activation_to_dict(value: ValidatedActivation) -> dict[str, Any]:
    return {
        "activation_ref": value.activation_ref,
        "selection": _selection_to_dict(value.selection),
        "validated_data_revision": value.validated_data_revision,
    }


def _activation_from_dict(value: dict[str, Any]) -> ValidatedActivation:
    return ValidatedActivation(
        value["activation_ref"],
        _selection_from_dict(value["selection"]),
        value["validated_data_revision"],
    )


def _receipt_to_dict(receipt: LifecycleReceipt) -> dict[str, Any]:
    return {
        "request": _request_to_dict(receipt.request),
        "record": None if receipt.record is None else _record_to_dict(receipt.record),
        "outcome": receipt.outcome.value,
    }


def _receipt_from_dict(value: dict[str, Any]) -> LifecycleReceipt:
    record = value["record"]
    return LifecycleReceipt(
        request=_request_from_dict(value["request"]),
        record=None if record is None else _record_from_dict(record),
        outcome=ReceiptOutcome(value["outcome"]),
    )


def _receipt_to_json(receipt: LifecycleReceipt) -> str:
    return _canonical_json(_receipt_to_dict(receipt))


def _receipt_from_json(value: str) -> LifecycleReceipt:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise LifecycleContractError()
    return _receipt_from_dict(parsed)


def _operation_to_json(operation: LifecycleOperation) -> str:
    return _canonical_json(
        {
            "request": _request_to_dict(operation.request),
            "phase": operation.phase.value,
            "phase_revision": operation.phase_revision,
            "previous": (
                None if operation.previous is None else _record_to_dict(operation.previous)
            ),
            "candidate": (
                None
                if operation.candidate is None
                else _selection_to_dict(operation.candidate)
            ),
            "frozen_data": (
                None
                if operation.frozen_data is None
                else _frozen_to_dict(operation.frozen_data)
            ),
            "staged_data": (
                None
                if operation.staged_data is None
                else _staged_to_dict(operation.staged_data)
            ),
            "activation": (
                None
                if operation.activation is None
                else _activation_to_dict(operation.activation)
            ),
            "intended_receipt": (
                None
                if operation.intended_receipt is None
                else _receipt_to_dict(operation.intended_receipt)
            ),
        }
    )


def _operation_from_json(value: str) -> LifecycleOperation:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise LifecycleContractError()
    previous = parsed["previous"]
    candidate = parsed["candidate"]
    frozen_data = parsed["frozen_data"]
    staged_data = parsed["staged_data"]
    activation = parsed["activation"]
    intended_receipt = parsed["intended_receipt"]
    return LifecycleOperation(
        request=_request_from_dict(parsed["request"]),
        phase=LifecyclePhase(parsed["phase"]),
        phase_revision=parsed["phase_revision"],
        previous=None if previous is None else _record_from_dict(previous),
        candidate=None if candidate is None else _selection_from_dict(candidate),
        frozen_data=None if frozen_data is None else _frozen_from_dict(frozen_data),
        staged_data=None if staged_data is None else _staged_from_dict(staged_data),
        activation=None if activation is None else _activation_from_dict(activation),
        intended_receipt=(
            None if intended_receipt is None else _receipt_from_dict(intended_receipt)
        ),
    )
