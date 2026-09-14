"""B20 internal installation state and staged lifecycle boundaries.

No transport schemas, process execution, authority mapping or persistence here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Protocol, runtime_checkable

from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

_COMMON = 'contracts/common/types.schema.json#/definitions/'
_SHA256 = re.compile(r'[0-9a-f]{64}')


class LifecycleContractError(ValueError):
    def __init__(self) -> None:
        super().__init__('invalid extension lifecycle contract')


class LifecycleConflictError(ValueError):
    """Revision, phase or operation ownership changed; no mutation applied."""


class ExtensionHostConflictError(RuntimeError):
    """Application-level extension gateway conflict."""


class ExtensionHostUnavailableError(RuntimeError):
    """Application-level extension gateway is not serving."""


@runtime_checkable
class ExtensionGateway(Protocol):
    """Public engine-facing gateway for installed extension operations."""

    def install(
        self,
        archive_path,
        *,
        principal: str,
        idempotency_key: str,
        expected_revision: int,
    ): ...
    def inspect(self, archive_path): ...
    def update(self, extension_id: str, archive_path, *, principal: str,
               idempotency_key: str, expected_revision: int): ...
    def remove(self, extension_id: str, *, principal: str,
               idempotency_key: str, expected_revision: int): ...
    def enable(
        self,
        extension_id: str,
        *,
        principal: str,
        idempotency_key: str,
        expected_revision: int,
    ): ...
    def disable(
        self,
        extension_id: str,
        *,
        principal: str,
        idempotency_key: str,
        expected_revision: int,
    ): ...
    def get_extension(self, extension_id: str): ...
    def list_extensions(self): ...
    def operation_catalog(self): ...
    def invoke_result(
        self,
        operation: str,
        input: dict,
        *,
        principal: str,
        idempotency_key: str,
    ): ...
    def ui_contributions(self): ...
    def panel_get(self, panel_id: str): ...
    def job_get(self, params: dict, *, principal: str): ...
    def job_cancel(self, params: dict, *, principal: str): ...


class LifecycleIdempotencyConflictError(LifecycleConflictError):
    """Same principal/method/key with a different immutable request digest."""


class LifecycleResolutionRequiredError(LifecycleConflictError):
    """Rollback would discard new data without an explicit resolution."""


def _wire(kind: str, value: object) -> None:
    try:
        validate_schema_ref(_COMMON + kind, value)
    except SchemaValidationError:
        raise LifecycleContractError() from None


def _revision(value: object) -> None:
    if type(value) is not int or value < 0:
        raise LifecycleContractError()


def _ref(value: object) -> None:
    if type(value) is not str or len(value) > 128:
        raise LifecycleContractError()
    _wire('opaque_ref', value)


def _digest(value: object) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise LifecycleContractError()


def _scopes(value: object) -> None:
    if type(value) is not tuple or len(value) > 64:
        raise LifecycleContractError()
    if any(type(item) is not str or not item or len(item) > 64 for item in value):
        raise LifecycleContractError()
    if len(set(value)) != len(value):
        raise LifecycleContractError()


class LifecycleAction(str, Enum):
    INSTALL = 'install'
    ENABLE = 'enable'
    DISABLE = 'disable'
    UPDATE = 'update'
    REMOVE = 'remove'
    CHANGE_GRANTS = 'change_grants'


class ExtensionStatus(str, Enum):
    INSTALLED = 'installed'
    ENABLED = 'enabled'
    DISABLED = 'disabled'
    FAILED = 'failed'
    REMOVED = 'removed'  # Internal retained tombstone, omitted from public list/get.


class LifecyclePhase(str, Enum):
    CLAIMED = 'claimed'
    QUIESCED = 'quiesced'
    DATA_STAGED = 'data_staged'
    ACTIVATION_VALIDATED = 'activation_validated'
    SWITCHED = 'switched'
    RESTORING = 'restoring'
    SETTLED = 'settled'
    ABORTED = 'aborted'
    ROLLED_BACK = 'rolled_back'
    RESOLUTION_REQUIRED = 'resolution_required'


def allowed_next_phases(
    action: LifecycleAction, phase: LifecyclePhase, *,
    previous_status: ExtensionStatus | None = None,
) -> tuple[LifecyclePhase, ...]:
    """Phase graph using DURABLY CAPTURED previous status, never caller intent."""
    if type(action) is not LifecycleAction or type(phase) is not LifecyclePhase:
        raise LifecycleContractError()
    if previous_status is not None and type(previous_status) is not ExtensionStatus:
        raise LifecycleContractError()
    if action in (LifecycleAction.UPDATE, LifecycleAction.CHANGE_GRANTS) and previous_status is None:
        raise LifecycleContractError()
    needs_activation = action is LifecycleAction.ENABLE or (
        action in (LifecycleAction.UPDATE, LifecycleAction.CHANGE_GRANTS)
        and previous_status is ExtensionStatus.ENABLED)
    if phase is LifecyclePhase.CLAIMED:
        return (LifecyclePhase.QUIESCED, LifecyclePhase.RESTORING)
    if phase is LifecyclePhase.QUIESCED:
        if action in (LifecycleAction.INSTALL, LifecycleAction.UPDATE):
            next_phase = LifecyclePhase.DATA_STAGED
        elif needs_activation:
            next_phase = LifecyclePhase.ACTIVATION_VALIDATED
        else:
            next_phase = LifecyclePhase.SWITCHED
        return (next_phase, LifecyclePhase.RESTORING)
    if phase is LifecyclePhase.DATA_STAGED:
        if action not in (LifecycleAction.INSTALL, LifecycleAction.UPDATE):
            return ()
        next_phase = LifecyclePhase.ACTIVATION_VALIDATED if needs_activation else LifecyclePhase.SWITCHED
        return (next_phase, LifecyclePhase.RESTORING)
    if phase is LifecyclePhase.ACTIVATION_VALIDATED:
        return (LifecyclePhase.SWITCHED, LifecyclePhase.RESTORING) if needs_activation else ()
    if phase is LifecyclePhase.SWITCHED:
        return (LifecyclePhase.SETTLED, LifecyclePhase.RESTORING, LifecyclePhase.RESOLUTION_REQUIRED)
    if phase is LifecyclePhase.RESOLUTION_REQUIRED:
        return (LifecyclePhase.RESTORING,)
    if phase is LifecyclePhase.RESTORING:
        return (LifecyclePhase.ABORTED, LifecyclePhase.ROLLED_BACK)
    return ()


@dataclass(frozen=True, slots=True)
class ExecutableArtifact:
    """Inspected hash-addressed artifact, not a pathname or trust assertion."""
    artifact_id: str
    extension_id: str
    version: str
    requested_scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        _digest(self.artifact_id)
        _wire('reverse_domain_id', self.extension_id)
        try:
            validate_schema_ref('contracts/plugin.v1/manifest.schema.json#/properties/version', self.version)
        except SchemaValidationError:
            raise LifecycleContractError() from None
        if len(self.version) > 64:
            raise LifecycleContractError()
        _scopes(self.requested_scopes)


@dataclass(frozen=True, slots=True)
class SelectedInstallation:
    executable: ExecutableArtifact
    data_ref: str
    approved_scopes: tuple[str, ...]
    grant_generation: int
    activation_generation: int

    def __post_init__(self) -> None:
        if type(self.executable) is not ExecutableArtifact:
            raise LifecycleContractError()
        _ref(self.data_ref)
        _scopes(self.approved_scopes)
        if not set(self.approved_scopes).issubset(self.executable.requested_scopes):
            raise LifecycleContractError()
        _revision(self.grant_generation)
        _revision(self.activation_generation)


@dataclass(frozen=True, slots=True)
class ExtensionRecord:
    extension_id: str
    revision: int
    status: ExtensionStatus
    selected: SelectedInstallation

    def __post_init__(self) -> None:
        _wire('reverse_domain_id', self.extension_id)
        _revision(self.revision)
        if type(self.status) is not ExtensionStatus or type(self.selected) is not SelectedInstallation:
            raise LifecycleContractError()
        if self.extension_id != self.selected.executable.extension_id:
            raise LifecycleContractError()


@dataclass(frozen=True, slots=True)
class LifecycleRequest:
    """Authenticated operator request; digest binds ALL supplied semantic fields.

    principal_ref comes from trusted enrollment, never worker input. Candidate
    archives are read/inspected once and bound by digest, not later reread by path.
    Every mutation requires an operator idempotency key; absent install revision is 0.
    """
    operation_id: str
    principal_ref: str
    action: LifecycleAction
    extension_id: str
    expected_revision: int
    request_digest: str
    idempotency_key: str
    candidate: ExecutableArtifact | None = None
    approved_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _wire('uuid', self.operation_id)
        _ref(self.principal_ref)
        _wire('reverse_domain_id', self.extension_id)
        _revision(self.expected_revision)
        _digest(self.request_digest)
        if type(self.action) is not LifecycleAction:
            raise LifecycleContractError()
        _wire('idempotency_key', self.idempotency_key)
        _scopes(self.approved_scopes)
        needs_candidate = self.action in (LifecycleAction.INSTALL, LifecycleAction.UPDATE)
        if needs_candidate != (self.candidate is not None):
            raise LifecycleContractError()
        if self.candidate is not None and (type(self.candidate) is not ExecutableArtifact or self.candidate.extension_id != self.extension_id):
            raise LifecycleContractError()
        if self.action is not LifecycleAction.CHANGE_GRANTS and self.approved_scopes:
            raise LifecycleContractError()


@dataclass(frozen=True, slots=True)
class FrozenData:
    """Writes denied under the broker barrier; final revision includes prior writes."""
    freeze_ref: str
    data_ref: str
    final_revision: int

    def __post_init__(self) -> None:
        _ref(self.freeze_ref)
        _ref(self.data_ref)
        _revision(self.final_revision)


@dataclass(frozen=True, slots=True)
class StagedData:
    data_ref: str
    initial_revision: int
    migration_receipt_ref: str

    def __post_init__(self) -> None:
        _ref(self.data_ref)
        _revision(self.initial_revision)
        _ref(self.migration_receipt_ref)


@dataclass(frozen=True, slots=True)
class ValidatedActivation:
    """Supervisor-owned reference to non-serving validation, never bearer token."""
    activation_ref: str
    selection: SelectedInstallation
    validated_data_revision: int

    def __post_init__(self) -> None:
        _ref(self.activation_ref)
        _revision(self.validated_data_revision)
        if type(self.selection) is not SelectedInstallation:
            raise LifecycleContractError()


@dataclass(frozen=True, slots=True)
class LifecycleOperation:
    request: LifecycleRequest
    phase: LifecyclePhase
    phase_revision: int
    previous: ExtensionRecord | None
    candidate: SelectedInstallation | None = None
    frozen_data: FrozenData | None = None
    staged_data: StagedData | None = None
    activation: ValidatedActivation | None = None
    intended_receipt: LifecycleReceipt | None = None

    def __post_init__(self) -> None:
        if type(self.request) is not LifecycleRequest or type(self.phase) is not LifecyclePhase:
            raise LifecycleContractError()
        _revision(self.phase_revision)
        for value, expected in ((self.previous, ExtensionRecord), (self.candidate, SelectedInstallation),
                                (self.frozen_data, FrozenData), (self.staged_data, StagedData),
                                (self.activation, ValidatedActivation)):
            if value is not None and type(value) is not expected:
                raise LifecycleContractError()
        if self.previous is not None and self.previous.extension_id != self.request.extension_id:
            raise LifecycleContractError()
        if self.candidate is not None and self.candidate.executable.extension_id != self.request.extension_id:
            raise LifecycleContractError()
        if self.activation is not None and self.activation.selection != self.candidate:
            raise LifecycleContractError()
        if self.intended_receipt is not None:
            if type(self.intended_receipt) is not LifecycleReceipt or self.intended_receipt.request != self.request:
                raise LifecycleContractError()
        expected_outcomes = {
            LifecyclePhase.SWITCHED: (ReceiptOutcome.APPLIED,),
            LifecyclePhase.RESOLUTION_REQUIRED: (ReceiptOutcome.APPLIED,),
            LifecyclePhase.RESTORING: (ReceiptOutcome.ABORTED, ReceiptOutcome.ROLLED_BACK),
            LifecyclePhase.SETTLED: (ReceiptOutcome.APPLIED,),
            LifecyclePhase.ABORTED: (ReceiptOutcome.ABORTED,),
            LifecyclePhase.ROLLED_BACK: (ReceiptOutcome.ROLLED_BACK,),
        }
        outcomes = expected_outcomes.get(self.phase)
        if outcomes is None:
            if self.intended_receipt is not None:
                raise LifecycleContractError()
        elif self.intended_receipt is None or self.intended_receipt.outcome not in outcomes:
            raise LifecycleContractError()


class ReceiptOutcome(str, Enum):
    APPLIED = 'applied'
    ABORTED = 'aborted'
    ROLLED_BACK = 'rolled_back'


@dataclass(frozen=True, slots=True)
class LifecycleReceipt:
    """Exact original durable outcome; never replace it with current state."""
    request: LifecycleRequest
    record: ExtensionRecord | None
    outcome: ReceiptOutcome

    def __post_init__(self) -> None:
        if type(self.request) is not LifecycleRequest or type(self.outcome) is not ReceiptOutcome:
            raise LifecycleContractError()
        if self.record is None:
            if self.outcome is not ReceiptOutcome.ABORTED or self.request.action is not LifecycleAction.INSTALL:
                raise LifecycleContractError()
        elif type(self.record) is not ExtensionRecord or self.record.extension_id != self.request.extension_id:
            raise LifecycleContractError()


class ClaimDisposition(str, Enum):
    ADMITTED = 'admitted'
    REPLAY = 'replay'
    IN_PROGRESS = 'in_progress'


@dataclass(frozen=True, slots=True)
class LifecycleClaim:
    disposition: ClaimDisposition
    operation: LifecycleOperation | None = None
    receipt: LifecycleReceipt | None = None

    def __post_init__(self) -> None:
        if type(self.disposition) is not ClaimDisposition:
            raise LifecycleContractError()
        if self.disposition is ClaimDisposition.REPLAY:
            if type(self.receipt) is not LifecycleReceipt or self.operation is not None:
                raise LifecycleContractError()
        elif type(self.operation) is not LifecycleOperation or self.receipt is not None:
            raise LifecycleContractError()


@runtime_checkable
class ExtensionLifecycleRepository(Protocol):
    def get(self, extension_id: str) -> ExtensionRecord | None: ...

    def claim(self, request: LifecycleRequest) -> LifecycleClaim:
        """Atomic: exact receipt/pending key lookup BEFORE CAS, then one claim/id.

        Key identity is principal/action/key, digest mismatch conflicts.
        A pending claim blocks all
        other mutations of this extension. Absent revision is 0; tombstones keep
        their revision. Claim captures previous selected state durably.
        """
        ...

    def advance(self, operation: LifecycleOperation, *, expected_phase_revision: int) -> LifecycleOperation:
        """Persist only legal action-specific forward phases with phase CAS.

        Request/previous binding is immutable. Before switching, attach frozen
        final revision, staged data and non-serving validation receipts as needed.
        Never change selected pointers through this method. Reject forged/skipped
        phases; docs define paths. Only abort/rollback enter RESTORING; only
        settle enters terminal phases. External effects occur outside transactions.
        """
        ...

    def switch(self, operation_id: str, *, expected_phase_revision: int) -> LifecycleOperation:
        """One transaction changes selected executable/data/grants/generations,
        status and extension revision, and records SWITCHED. Install and disabled
        update NEVER execute candidate; use staged-data validation only. Requires prior
        freeze/staging/validation for serving upgrades. Admission remains closed
        until authority synchronization and explicit activation admission. Persist
        intended APPLIED receipt with exact selected record in this transaction.
        """
        ...

    def settle(self, operation_id: str, *, expected_phase_revision: int) -> LifecycleReceipt:
        """Only after runtime synchronization, persist the intended exact receipt
        and release claim. SWITCHED becomes SETTLED; RESTORING becomes ABORTED or
        ROLLED_BACK from its persisted intended receipt, never inferred from current
        state. Restoration failure keeps RESTORING claimed/recoverable.
        """
        ...

    def abort(
        self,
        operation_id: str,
        *,
        expected_phase_revision: int,
        revocation_ref: str,
        frozen_data: FrozenData | None = None,
    ) -> LifecycleOperation:
        """Pre-switch only: proof candidate authority revoked, retain old selection.
        Atomically persist RESTORING plus intended ABORTED receipt (previous
        record or None for failed first install). ``frozen_data`` carries a
        completed freeze whose QUIESCED advance failed; it must match the prior
        selected data and any already-persisted freeze evidence. HOLD claim
        through thaw/admit synchronization; settle alone publishes
        receipt/releases. Never reopen candidate authority. Crash after this
        return remains recoverable.
        """
        ...

    def rollback(self, operation_id: str, *, expected_phase_revision: int,
                 revocation_ref: str, current_data: FrozenData,
                 resolution_ref: str | None = None) -> LifecycleOperation:
        """Post-switch: candidate frozen/revoked; guard actual data revision.

        Automatic restore only if data unchanged since validated/staged switch baseline.
        Otherwise explicit supervisor-approved export/resolution is required;
        denial retains claim in RESOLUTION_REQUIRED. Restore previous pointers
        with NEW monotonic revision/generations, never resurrect old tokens.
        First-install rollback retains REMOVED tombstone and data. Atomically
        persist restored selection, RESTORING and intended ROLLED_BACK receipt.
        HOLD claim until thaw/admit synchronization and settle. No final receipt
        or claim release here, including when runtime restoration fails.
        """
        ...

    def recover(self, *, after_operation_id: str | None = None, limit: int = 256) -> tuple[LifecycleOperation, ...]:
        """Bounded UUID-ordered pending records; no implicit execution or retry.

        Composition MUST hold exclusive engine instance lease for this DB before
        coordinator startup. Recover serially per operation before serving; one
        execution owner per operation. Include RESTORING and RESOLUTION_REQUIRED;
        never exclude restoration because selected pointers already changed.
        Phase CAS alone does not fence effects.
        """
        ...


@runtime_checkable
class ExtensionDataLifecycle(Protocol):
    def selected_revision(self, selected: SelectedInstallation) -> int:
        """Read the exact dataset revision for this namespace/data/artifact binding.

        This lookup does not select, thaw or otherwise mutate the generation. It
        may inspect staged, retained or frozen data and a future target activation
        generation so admission can bind its synchronization barrier to fresh data.
        """
        ...

    def freeze(self, operation_id: str, selected: SelectedInstallation) -> FrozenData:
        """Stop old writes under SAME broker mutation barrier, then final revision."""
        ...

    def stage(self, operation_id: str, candidate: ExecutableArtifact,
              frozen: FrozenData | None) -> StagedData:
        """Initialize install data or migrate a copy of frozen data. Idempotent by
        operation; migration format/implementation remains outside this port.
        Never modify frozen source. Return only after durable validation.
        """
        ...

    def thaw(self, operation_id: str, frozen: FrozenData) -> None:
        """Reopen only the selected old data after durable RESTORING intent and revocation, before settle."""
        ...


@runtime_checkable
class ExtensionActivationLifecycle(Protocol):
    def quiesce(self, operation_id: str, previous: ExtensionRecord, *, deadline_ms: int) -> None:
        """Stop admission, drain or explicitly interrupt all owned work by deadline."""
        ...

    def validate(self, operation_id: str, candidate: SelectedInstallation) -> ValidatedActivation:
        """Verify exact artifact/identity/session using isolated non-serving data;
        no operation admission or authority to write old selected data. Freeze
        candidate writes before recording validated_data_revision; admit releases
        candidate freeze only after switch. Reuse after restart must revalidate.
        """
        ...

    def revoke(self, operation_id: str, activation: ValidatedActivation | None) -> str:
        """Idempotently invalidate candidate authority, return opaque proof ref."""
        ...

    def admit(self, operation_id: str, record: ExtensionRecord,
              activation: ValidatedActivation | None, *,
              expected_data_revision: int) -> None:
        """Synchronize selected generations, revoke old authority, then admit only
        ENABLED record. For disabled/removed records keep all admission closed.
        Atomically require the selected dataset revision supplied by the lifecycle
        coordinator. Restart never trusts a persisted session as a live activation.
        """
        ...
