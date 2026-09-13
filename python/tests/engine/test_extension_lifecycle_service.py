from __future__ import annotations

import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.storage.sqlite_extension_lifecycle import (
    SQLiteExtensionLifecycleRepository,
)

from model_deck.engine.extensions.ports import (
    ClaimDisposition,
    ExecutableArtifact,
    ExtensionRecord,
    ExtensionStatus,
    FrozenData,
    LifecycleAction,
    LifecycleClaim,
    LifecycleOperation,
    LifecyclePhase,
    LifecycleReceipt,
    LifecycleRequest,
    ReceiptOutcome,
    SelectedInstallation,
    StagedData,
    ValidatedActivation,
)
from model_deck.engine.extensions.service import (
    ExtensionLifecycleService,
    LifecycleExecutionConflictError,
)


EXTENSION_ID = "org.example.lifecycle"


class BoundaryFailure(RuntimeError):
    pass


class FakeLease:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.held = True
        self._available_assertions = 0
        self._guard = threading.Lock()
        self._owned_operations: set[str] = set()

    def assert_held_for(self, repository: object) -> None:
        if not self.held:
            raise BoundaryFailure("lease")
        with self._guard:
            self._available_assertions += 1

    @contextmanager
    def execution_owner(self, repository: object, operation_id: str):
        with self._guard:
            if self._available_assertions == 0:
                raise AssertionError("missing lease assertion before execution ownership")
            self._available_assertions -= 1
            key = operation_id
            if key in self._owned_operations:
                raise LifecycleExecutionConflictError()
            self._owned_operations.add(key)
        try:
            yield
        finally:
            with self._guard:
                self._owned_operations.remove(key)

    def consume_assertion(self, boundary: str) -> None:
        with self._guard:
            if self._available_assertions == 0:
                raise AssertionError(f"missing lease assertion before {boundary}")
            self._available_assertions -= 1
        self.log.append(boundary)


class BoundaryController:
    def __init__(self, lease: FakeLease) -> None:
        self.lease = lease
        self.failures: dict[str, int] = {}

    def fail_once(self, boundary: str) -> None:
        self.fail_on(boundary, occurrence=1)

    def fail_on(self, boundary: str, *, occurrence: int) -> None:
        self.failures[boundary] = occurrence

    def enter(self, boundary: str) -> None:
        self.lease.consume_assertion(boundary)
        remaining = self.failures.get(boundary, 0)
        if remaining:
            remaining -= 1
            self.failures[boundary] = remaining
            if remaining == 0:
                raise BoundaryFailure(boundary)


class FakeRepository:
    def __init__(
        self,
        controller: BoundaryController,
        *,
        previous: ExtensionRecord | None = None,
    ) -> None:
        self.controller = controller
        self.record = previous
        self.operation: LifecycleOperation | None = None
        self.receipt: LifecycleReceipt | None = None

    def get(self, extension_id: str) -> ExtensionRecord | None:
        self.controller.enter("repo.get")
        return self.record if self.record and self.record.extension_id == extension_id else None

    def claim(self, request: LifecycleRequest) -> LifecycleClaim:
        self.controller.enter("repo.claim")
        if self.receipt is not None:
            return LifecycleClaim(ClaimDisposition.REPLAY, receipt=self.receipt)
        if self.operation is not None:
            if request != self.operation.request:
                raise BoundaryFailure("request.binding")
            return LifecycleClaim(ClaimDisposition.IN_PROGRESS, operation=self.operation)
        self.operation = LifecycleOperation(
            request=request,
            phase=LifecyclePhase.CLAIMED,
            phase_revision=0,
            previous=self.record,
        )
        return LifecycleClaim(ClaimDisposition.ADMITTED, operation=self.operation)

    def advance(
        self,
        operation: LifecycleOperation,
        *,
        expected_phase_revision: int,
    ) -> LifecycleOperation:
        self.controller.enter(f"repo.advance.{operation.phase.value}")
        if self.operation is None or expected_phase_revision != self.operation.phase_revision:
            raise AssertionError("bad fake phase revision")
        self.operation = operation
        return operation

    def switch(self, operation_id: str, *, expected_phase_revision: int) -> LifecycleOperation:
        self.controller.enter("repo.switch")
        operation = self._current(operation_id, expected_phase_revision)
        previous = operation.previous
        action = operation.request.action
        candidate = operation.candidate
        if action is LifecycleAction.DISABLE:
            assert previous is not None
            selected = replace(
                previous.selected,
                activation_generation=previous.selected.activation_generation + 1,
            )
            status = ExtensionStatus.DISABLED
        elif action is LifecycleAction.REMOVE:
            assert previous is not None
            selected = replace(
                previous.selected,
                grant_generation=previous.selected.grant_generation + 1,
                activation_generation=previous.selected.activation_generation + 1,
            )
            status = ExtensionStatus.REMOVED
        elif action is LifecycleAction.CHANGE_GRANTS and candidate is None:
            assert previous is not None
            selected = replace(
                previous.selected,
                approved_scopes=operation.request.approved_scopes,
                grant_generation=previous.selected.grant_generation + 1,
            )
            status = previous.status
        else:
            assert candidate is not None
            selected = candidate
            if action is LifecycleAction.INSTALL:
                status = ExtensionStatus.INSTALLED
            elif action is LifecycleAction.ENABLE:
                status = ExtensionStatus.ENABLED
            else:
                assert previous is not None
                status = previous.status
        revision = 1 if previous is None else previous.revision + 1
        self.record = ExtensionRecord(EXTENSION_ID, revision, status, selected)
        intended = LifecycleReceipt(operation.request, self.record, ReceiptOutcome.APPLIED)
        self.operation = replace(
            operation,
            phase=LifecyclePhase.SWITCHED,
            phase_revision=operation.phase_revision + 1,
            candidate=selected,
            intended_receipt=intended,
        )
        return self.operation

    def settle(self, operation_id: str, *, expected_phase_revision: int) -> LifecycleReceipt:
        assert self.operation is not None
        self.controller.enter(f"repo.settle.{self.operation.phase.value}")
        operation = self._current(operation_id, expected_phase_revision)
        assert operation.intended_receipt is not None
        self.receipt = operation.intended_receipt
        self.operation = None
        return self.receipt

    def abort(
        self,
        operation_id: str,
        *,
        expected_phase_revision: int,
        revocation_ref: str,
        frozen_data: FrozenData | None = None,
    ) -> LifecycleOperation:
        self.controller.enter("repo.abort")
        operation = self._current(operation_id, expected_phase_revision)
        intended = LifecycleReceipt(operation.request, operation.previous, ReceiptOutcome.ABORTED)
        self.operation = replace(
            operation,
            phase=LifecyclePhase.RESTORING,
            phase_revision=operation.phase_revision + 1,
            frozen_data=operation.frozen_data or frozen_data,
            intended_receipt=intended,
        )
        return self.operation

    def rollback(
        self,
        operation_id: str,
        *,
        expected_phase_revision: int,
        revocation_ref: str,
        current_data: FrozenData,
        resolution_ref: str | None = None,
    ) -> LifecycleOperation:
        self.controller.enter("repo.rollback")
        operation = self._current(operation_id, expected_phase_revision)
        if operation.previous is None:
            assert self.record is not None
            selected = replace(
                self.record.selected,
                grant_generation=self.record.selected.grant_generation + 1,
                activation_generation=self.record.selected.activation_generation + 1,
            )
            restored = ExtensionRecord(
                EXTENSION_ID,
                self.record.revision + 1,
                ExtensionStatus.REMOVED,
                selected,
            )
        else:
            assert self.record is not None
            selected = replace(
                operation.previous.selected,
                grant_generation=self.record.selected.grant_generation + 1,
                activation_generation=self.record.selected.activation_generation + 1,
            )
            restored = replace(
                operation.previous,
                revision=self.record.revision + 1,
                selected=selected,
            )
        self.record = restored
        intended = LifecycleReceipt(operation.request, restored, ReceiptOutcome.ROLLED_BACK)
        self.operation = replace(
            operation,
            phase=LifecyclePhase.RESTORING,
            phase_revision=operation.phase_revision + 1,
            intended_receipt=intended,
        )
        return self.operation

    def recover(
        self,
        *,
        after_operation_id: str | None = None,
        limit: int = 256,
    ) -> tuple[LifecycleOperation, ...]:
        self.controller.enter("repo.recover")
        return () if self.operation is None else (self.operation,)

    def _current(self, operation_id: str, phase_revision: int) -> LifecycleOperation:
        assert self.operation is not None
        if self.operation.request.operation_id != operation_id:
            raise AssertionError("wrong operation")
        if self.operation.phase_revision != phase_revision:
            raise AssertionError("wrong revision")
        return self.operation


class FailQuiescedAdvanceOnceRepository:
    """Real SQLite wrapper that fails only the first QUIESCED persistence."""

    def __init__(self, repository: SQLiteExtensionLifecycleRepository) -> None:
        self.repository = repository
        self.fail_quiesced_once = False

    def get(self, extension_id: str) -> ExtensionRecord | None:
        return self.repository.get(extension_id)

    def claim(self, lifecycle_request: LifecycleRequest) -> LifecycleClaim:
        return self.repository.claim(lifecycle_request)

    def advance(
        self,
        operation: LifecycleOperation,
        *,
        expected_phase_revision: int,
    ) -> LifecycleOperation:
        if self.fail_quiesced_once and operation.phase is LifecyclePhase.QUIESCED:
            self.fail_quiesced_once = False
            raise BoundaryFailure("repo.advance.quiesced")
        return self.repository.advance(
            operation,
            expected_phase_revision=expected_phase_revision,
        )

    def switch(self, operation_id: str, *, expected_phase_revision: int) -> LifecycleOperation:
        return self.repository.switch(
            operation_id,
            expected_phase_revision=expected_phase_revision,
        )

    def settle(self, operation_id: str, *, expected_phase_revision: int) -> LifecycleReceipt:
        return self.repository.settle(
            operation_id,
            expected_phase_revision=expected_phase_revision,
        )

    def abort(
        self,
        operation_id: str,
        *,
        expected_phase_revision: int,
        revocation_ref: str,
        frozen_data: FrozenData | None = None,
    ) -> LifecycleOperation:
        return self.repository.abort(
            operation_id,
            expected_phase_revision=expected_phase_revision,
            revocation_ref=revocation_ref,
            frozen_data=frozen_data,
        )

    def rollback(
        self,
        operation_id: str,
        *,
        expected_phase_revision: int,
        revocation_ref: str,
        current_data: FrozenData,
        resolution_ref: str | None = None,
    ) -> LifecycleOperation:
        return self.repository.rollback(
            operation_id,
            expected_phase_revision=expected_phase_revision,
            revocation_ref=revocation_ref,
            current_data=current_data,
            resolution_ref=resolution_ref,
        )

    def recover(
        self,
        *,
        after_operation_id: str | None = None,
        limit: int = 256,
    ) -> tuple[LifecycleOperation, ...]:
        return self.repository.recover(
            after_operation_id=after_operation_id,
            limit=limit,
        )


class SharedHeldLease:
    def __init__(self, *repositories: object) -> None:
        self._guard = threading.Lock()
        self._owners: set[str] = set()
        self._repositories = {id(repository) for repository in repositories}

    def assert_held_for(self, repository: object) -> None:
        if id(repository) not in self._repositories:
            raise BoundaryFailure("lease.repository")

    @contextmanager
    def execution_owner(self, repository: object, operation_id: str):
        key = operation_id
        with self._guard:
            if key in self._owners:
                raise LifecycleExecutionConflictError()
            self._owners.add(key)
        try:
            yield
        finally:
            with self._guard:
                self._owners.remove(key)


class StatefulDataLifecycle:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.next_data = 0
        self.fail_thaw = False

    def freeze(self, operation_id: str, selected: SelectedInstallation) -> FrozenData:
        self.log.append("freeze")
        return FrozenData(f"ref:freeze.{operation_id}", selected.data_ref, 7)

    def stage(
        self,
        operation_id: str,
        candidate: ExecutableArtifact,
        frozen: FrozenData | None,
    ) -> StagedData:
        self.log.append("stage")
        self.next_data += 1
        return StagedData(
            f"ref:data.real.{self.next_data}",
            0 if frozen is None else frozen.final_revision,
            f"ref:migration.real.{self.next_data}",
        )

    def thaw(self, operation_id: str, frozen: FrozenData) -> None:
        self.log.append("thaw")
        if self.fail_thaw:
            raise BoundaryFailure("data.thaw")


class StatefulActivationLifecycle:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.validate_entered: threading.Event | None = None
        self.validate_release: threading.Event | None = None

    def quiesce(
        self,
        operation_id: str,
        previous: ExtensionRecord,
        *,
        deadline_ms: int,
    ) -> None:
        self.log.append("quiesce")

    def validate(
        self,
        operation_id: str,
        candidate: SelectedInstallation,
    ) -> ValidatedActivation:
        self.log.append("validate")
        if self.validate_entered is not None:
            self.validate_entered.set()
        if self.validate_release is not None:
            self.validate_release.wait(timeout=2)
        return ValidatedActivation("ref:activation.real", candidate, 7)

    def revoke(
        self,
        operation_id: str,
        activation: ValidatedActivation | None,
    ) -> str:
        self.log.append("revoke")
        return "ref:revocation.real"

    def admit(
        self,
        operation_id: str,
        record: ExtensionRecord,
        activation: ValidatedActivation | None,
    ) -> None:
        self.log.append("admit")


class FakeDataLifecycle:
    def __init__(self, controller: BoundaryController) -> None:
        self.controller = controller

    def freeze(self, operation_id: str, selected: SelectedInstallation) -> FrozenData:
        self.controller.enter("data.freeze")
        return FrozenData(f"ref:freeze.{operation_id}", selected.data_ref, 7)

    def stage(
        self,
        operation_id: str,
        candidate: ExecutableArtifact,
        frozen: FrozenData | None,
    ) -> StagedData:
        self.controller.enter("data.stage")
        return StagedData(f"ref:data.{candidate.version}", 7 if frozen else 0, "ref:migration")

    def thaw(self, operation_id: str, frozen: FrozenData) -> None:
        self.controller.enter("data.thaw")


class FakeActivationLifecycle:
    def __init__(self, controller: BoundaryController) -> None:
        self.controller = controller
        self.validate_entered: threading.Event | None = None
        self.validate_release: threading.Event | None = None

    def quiesce(
        self,
        operation_id: str,
        previous: ExtensionRecord,
        *,
        deadline_ms: int,
    ) -> None:
        self.controller.enter("activation.quiesce")
        if deadline_ms != 10_000:
            raise AssertionError("wrong deadline")

    def validate(
        self,
        operation_id: str,
        candidate: SelectedInstallation,
    ) -> ValidatedActivation:
        self.controller.enter("activation.validate")
        if self.validate_entered is not None:
            self.validate_entered.set()
        if self.validate_release is not None:
            self.validate_release.wait(timeout=2)
        return ValidatedActivation("ref:activation", candidate, 7)

    def revoke(
        self,
        operation_id: str,
        activation: ValidatedActivation | None,
    ) -> str:
        self.controller.enter("activation.revoke")
        return "ref:revocation"

    def admit(
        self,
        operation_id: str,
        record: ExtensionRecord,
        activation: ValidatedActivation | None,
    ) -> None:
        self.controller.enter("activation.admit")
        if record.status is ExtensionStatus.ENABLED and activation is None:
            raise AssertionError("enabled admission requires fresh activation")
        if record.status is not ExtensionStatus.ENABLED and activation is not None:
            raise AssertionError("non-serving admission cannot receive activation")


def artifact(
    version: str,
    scopes: tuple[str, ...] = ("data.read", "data.write"),
) -> ExecutableArtifact:
    digest_character = "a" if version == "1.0.0" else "b"
    return ExecutableArtifact(digest_character * 64, EXTENSION_ID, version, scopes)


def record(status: ExtensionStatus = ExtensionStatus.ENABLED) -> ExtensionRecord:
    selected = SelectedInstallation(
        artifact("1.0.0"),
        "ref:data.1",
        ("data.read", "data.write"),
        3,
        4,
    )
    return ExtensionRecord(EXTENSION_ID, 8, status, selected)


def request(
    action: LifecycleAction,
    *,
    previous: ExtensionRecord | None = None,
    candidate: ExecutableArtifact | None = None,
    scopes: tuple[str, ...] = (),
    suffix: int = 1,
) -> LifecycleRequest:
    return LifecycleRequest(
        operation_id=f"10000000-0000-4000-8000-{suffix:012d}",
        principal_ref="ref:operator",
        action=action,
        extension_id=EXTENSION_ID,
        expected_revision=0 if previous is None else previous.revision,
        request_digest=str(suffix % 10) * 64,
        idempotency_key=f"key-{suffix}",
        candidate=candidate,
        approved_scopes=scopes,
    )


class ExtensionLifecycleServiceTests(unittest.TestCase):
    def make_service(
        self,
        *,
        previous: ExtensionRecord | None = None,
    ) -> tuple[
        ExtensionLifecycleService,
        FakeRepository,
        FakeDataLifecycle,
        FakeActivationLifecycle,
        BoundaryController,
        list[str],
    ]:
        log: list[str] = []
        lease = FakeLease(log)
        controller = BoundaryController(lease)
        repository = FakeRepository(controller, previous=previous)
        data = FakeDataLifecycle(controller)
        activation = FakeActivationLifecycle(controller)
        service = ExtensionLifecycleService(
            repository=repository,
            data_lifecycle=data,
            activation_lifecycle=activation,
            engine_lease=lease,
        )
        return service, repository, data, activation, controller, log

    def test_replay_and_in_progress_claims_have_no_external_effects(self) -> None:
        previous = record(ExtensionStatus.DISABLED)
        for disposition in (ClaimDisposition.REPLAY, ClaimDisposition.IN_PROGRESS):
            with self.subTest(disposition=disposition):
                service, repository, _, _, _, log = self.make_service(previous=previous)
                lifecycle_request = request(LifecycleAction.DISABLE, previous=previous)
                operation = LifecycleOperation(
                    lifecycle_request,
                    LifecyclePhase.CLAIMED,
                    0,
                    previous,
                )
                if disposition is ClaimDisposition.REPLAY:
                    repository.receipt = LifecycleReceipt(
                        lifecycle_request,
                        previous,
                        ReceiptOutcome.APPLIED,
                    )
                else:
                    repository.operation = operation

                result = service.execute(lifecycle_request)

                self.assertEqual(
                    result,
                    repository.receipt if disposition is ClaimDisposition.REPLAY else operation,
                )
                self.assertEqual(log, ["repo.claim"])

    def test_install_stages_without_freezing_or_executing_candidate(self) -> None:
        service, _, _, _, _, log = self.make_service()
        result = service.execute(
            request(LifecycleAction.INSTALL, candidate=artifact("1.0.0"))
        )

        self.assertEqual(result.outcome, ReceiptOutcome.APPLIED)
        self.assertEqual(result.record.status, ExtensionStatus.INSTALLED)
        self.assertEqual(
            log,
            [
                "repo.claim",
                "repo.advance.quiesced",
                "data.stage",
                "repo.advance.data_staged",
                "repo.switch",
                "activation.admit",
                "repo.settle.switched",
            ],
        )

    def test_disabled_update_and_grant_change_never_validate_activation(self) -> None:
        previous = record(ExtensionStatus.DISABLED)
        cases = (
            (
                request(
                    LifecycleAction.UPDATE,
                    previous=previous,
                    candidate=artifact("2.0.0", ("data.read",)),
                ),
                True,
            ),
            (
                request(
                    LifecycleAction.CHANGE_GRANTS,
                    previous=previous,
                    scopes=("data.read",),
                ),
                False,
            ),
        )
        for lifecycle_request, stages in cases:
            with self.subTest(action=lifecycle_request.action):
                service, _, _, _, _, log = self.make_service(previous=previous)
                result = service.execute(lifecycle_request)
                self.assertEqual(result.record.status, ExtensionStatus.DISABLED)
                self.assertNotIn("activation.validate", log)
                self.assertEqual("data.stage" in log, stages)
                if stages:
                    self.assertEqual(result.record.selected.approved_scopes, ("data.read",))
                else:
                    self.assertEqual(
                        result.record.selected.approved_scopes,
                        lifecycle_request.approved_scopes,
                    )

    def test_enabled_update_orders_effects_and_preserves_only_scope_intersection(self) -> None:
        previous = record()
        service, _, _, _, _, log = self.make_service(previous=previous)
        result = service.execute(
            request(
                LifecycleAction.UPDATE,
                previous=previous,
                candidate=artifact("2.0.0", ("data.read", "new.scope")),
            )
        )

        self.assertEqual(result.outcome, ReceiptOutcome.APPLIED)
        self.assertEqual(result.record.selected.approved_scopes, ("data.read",))
        self.assertEqual(
            log,
            [
                "repo.claim",
                "activation.quiesce",
                "data.freeze",
                "repo.advance.quiesced",
                "data.stage",
                "repo.advance.data_staged",
                "activation.validate",
                "repo.advance.activation_validated",
                "repo.switch",
                "activation.admit",
                "repo.settle.switched",
            ],
        )

    def test_enable_disable_and_remove_apply_their_serving_states(self) -> None:
        cases = (
            (LifecycleAction.ENABLE, ExtensionStatus.DISABLED, ExtensionStatus.ENABLED, True),
            (LifecycleAction.DISABLE, ExtensionStatus.ENABLED, ExtensionStatus.DISABLED, False),
            (LifecycleAction.REMOVE, ExtensionStatus.DISABLED, ExtensionStatus.REMOVED, False),
        )
        for action, prior_status, expected_status, validates in cases:
            with self.subTest(action=action):
                previous = record(prior_status)
                service, _, _, _, _, log = self.make_service(previous=previous)
                result = service.execute(request(action, previous=previous))
                self.assertEqual(result.record.status, expected_status)
                self.assertEqual("activation.validate" in log, validates)
                self.assertEqual(log[-2:], ["activation.admit", "repo.settle.switched"])

    def test_pre_switch_effect_failures_abort_then_restore_before_settle(self) -> None:
        previous = record()
        for boundary in (
            "activation.quiesce",
            "data.freeze",
            "repo.advance.quiesced",
            "data.stage",
            "repo.advance.data_staged",
            "activation.validate",
            "repo.advance.activation_validated",
            "repo.switch",
        ):
            with self.subTest(boundary=boundary):
                service, repository, _, _, controller, log = self.make_service(previous=previous)
                controller.fail_once(boundary)
                result = service.execute(
                    request(
                        LifecycleAction.UPDATE,
                        previous=previous,
                        candidate=artifact("2.0.0"),
                    )
                )
                self.assertEqual(result.outcome, ReceiptOutcome.ABORTED)
                self.assertIsNone(repository.operation)
                self.assertIn("activation.revoke", log)
                self.assertIn("repo.abort", log)
                self.assertLess(log.index("repo.abort"), log.index("activation.admit"))
                if boundary == "repo.advance.quiesced":
                    self.assertIn("data.thaw", log)
                self.assertEqual(log[-1], "repo.settle.restoring")

    def test_claim_failure_has_no_external_effects(self) -> None:
        previous = record()
        service, _, _, _, controller, log = self.make_service(previous=previous)
        controller.fail_once("repo.claim")

        with self.assertRaisesRegex(BoundaryFailure, "repo.claim"):
            service.execute(
                request(
                    LifecycleAction.UPDATE,
                    previous=previous,
                    candidate=artifact("2.0.0"),
                )
            )

        self.assertEqual(log, ["repo.claim"])

    def test_abort_failure_retains_the_original_pre_switch_phase(self) -> None:
        previous = record()
        service, repository, _, _, controller, _ = self.make_service(previous=previous)
        controller.fail_once("data.stage")
        controller.fail_once("repo.abort")

        with self.assertRaisesRegex(BoundaryFailure, "repo.abort"):
            service.execute(
                request(
                    LifecycleAction.UPDATE,
                    previous=previous,
                    candidate=artifact("2.0.0"),
                )
            )

        self.assertEqual(repository.operation.phase, LifecyclePhase.QUIESCED)
        self.assertIsNone(repository.receipt)

    def test_failed_revocation_retains_pre_switch_claim(self) -> None:
        previous = record()
        service, repository, _, _, controller, _ = self.make_service(previous=previous)
        controller.fail_once("data.stage")
        controller.fail_once("activation.revoke")

        with self.assertRaisesRegex(BoundaryFailure, "activation.revoke"):
            service.execute(
                request(
                    LifecycleAction.UPDATE,
                    previous=previous,
                    candidate=artifact("2.0.0"),
                )
            )

        self.assertEqual(repository.operation.phase, LifecyclePhase.QUIESCED)
        self.assertIsNone(repository.receipt)

    def test_failed_restoration_retains_restoring_claim(self) -> None:
        previous = record()
        for boundary in (
            "data.thaw",
            "activation.validate",
            "activation.admit",
            "repo.settle.restoring",
        ):
            with self.subTest(boundary=boundary):
                service, repository, _, _, controller, _ = self.make_service(previous=previous)
                controller.fail_once("data.stage")
                controller.fail_once(boundary)
                with self.assertRaisesRegex(BoundaryFailure, boundary):
                    service.execute(
                        request(
                            LifecycleAction.UPDATE,
                            previous=previous,
                            candidate=artifact("2.0.0"),
                        )
                    )
                self.assertEqual(repository.operation.phase, LifecyclePhase.RESTORING)
                self.assertIsNone(repository.receipt)

    def test_abort_persists_local_freeze_before_a_crash_and_recovery_thaws_it(self) -> None:
        previous = record()
        service, repository, data, activation, controller, _ = self.make_service(
            previous=previous
        )
        controller.fail_once("repo.advance.quiesced")
        controller.fail_once("data.thaw")

        with self.assertRaisesRegex(BoundaryFailure, "data.thaw"):
            service.execute(
                request(
                    LifecycleAction.UPDATE,
                    previous=previous,
                    candidate=artifact("2.0.0"),
                )
            )

        restoring = repository.operation
        assert restoring is not None
        self.assertEqual(restoring.phase, LifecyclePhase.RESTORING)
        self.assertIsNotNone(restoring.frozen_data)
        controller.lease._available_assertions = 0
        controller.lease.log.clear()
        reconstructed = ExtensionLifecycleService(
            repository=repository,
            data_lifecycle=data,
            activation_lifecycle=activation,
            engine_lease=controller.lease,
        )

        receipt = reconstructed.recover_operation(restoring)

        self.assertEqual(receipt.outcome, ReceiptOutcome.ABORTED)
        self.assertIn("data.thaw", controller.lease.log)
        self.assertLess(
            controller.lease.log.index("data.thaw"),
            controller.lease.log.index("activation.admit"),
        )

    def test_real_sqlite_reconstruction_thaws_freeze_bound_during_abort(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "lifecycle.sqlite3"
            repository = SQLiteExtensionLifecycleRepository(database_path)
            failing_repository = FailQuiescedAdvanceOnceRepository(repository)
            data = StatefulDataLifecycle()
            activation = StatefulActivationLifecycle()
            lease = SharedHeldLease(failing_repository)
            service = ExtensionLifecycleService(
                repository=failing_repository,
                data_lifecycle=data,
                activation_lifecycle=activation,
                engine_lease=lease,
            )
            installed = service.execute(
                request(
                    LifecycleAction.INSTALL,
                    candidate=artifact("1.0.0"),
                    suffix=31,
                )
            ).record
            assert installed is not None
            enabled = service.execute(
                request(
                    LifecycleAction.ENABLE,
                    previous=installed,
                    suffix=32,
                )
            ).record
            assert enabled is not None
            failing_repository.fail_quiesced_once = True
            data.fail_thaw = True

            with self.assertRaisesRegex(BoundaryFailure, "data.thaw"):
                service.execute(
                    request(
                        LifecycleAction.UPDATE,
                        previous=enabled,
                        candidate=artifact("2.0.0"),
                        suffix=33,
                    )
                )

            reopened = SQLiteExtensionLifecycleRepository(database_path)
            pending = reopened.recover()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].phase, LifecyclePhase.RESTORING)
            self.assertEqual(pending[0].frozen_data.data_ref, enabled.selected.data_ref)

            data.fail_thaw = False
            data.log.clear()
            activation.log.clear()
            reconstructed = ExtensionLifecycleService(
                repository=reopened,
                data_lifecycle=data,
                activation_lifecycle=activation,
                engine_lease=SharedHeldLease(reopened),
            )
            receipt = reconstructed.recover_operation(pending[0])

            self.assertEqual(receipt.outcome, ReceiptOutcome.ABORTED)
            self.assertEqual(receipt.record, enabled)
            self.assertIn("thaw", data.log)
            self.assertEqual(activation.log[-2:], ["validate", "admit"])
            self.assertEqual(reopened.recover(), ())

    def test_failed_admission_rolls_back_then_restores_before_settle(self) -> None:
        previous = record()
        service, _, _, _, controller, log = self.make_service(previous=previous)
        controller.fail_once("activation.admit")

        result = service.execute(
            request(
                LifecycleAction.UPDATE,
                previous=previous,
                candidate=artifact("2.0.0"),
            )
        )

        self.assertEqual(result.outcome, ReceiptOutcome.ROLLED_BACK)
        rollback_index = log.index("repo.rollback")
        self.assertEqual(
            log[rollback_index - 2:rollback_index],
            ["data.freeze", "activation.revoke"],
        )
        self.assertEqual(log[-1], "repo.settle.restoring")

    def test_failed_post_switch_freeze_or_revoke_retains_switched_claim(self) -> None:
        previous = record()
        for boundary in ("data.freeze", "activation.revoke", "repo.rollback"):
            with self.subTest(boundary=boundary):
                service, repository, _, _, controller, _ = self.make_service(previous=previous)
                controller.fail_once("activation.admit")
                if boundary == "data.freeze":
                    controller.fail_on(boundary, occurrence=2)
                else:
                    controller.fail_once(boundary)
                with self.assertRaisesRegex(BoundaryFailure, boundary):
                    service.execute(
                        request(
                            LifecycleAction.UPDATE,
                            previous=previous,
                            candidate=artifact("2.0.0"),
                        )
                    )
                self.assertEqual(repository.operation.phase, LifecyclePhase.SWITCHED)
                self.assertIsNone(repository.receipt)

    def test_failed_settle_does_not_rollback_synchronized_selection(self) -> None:
        previous = record()
        service, repository, _, _, controller, log = self.make_service(previous=previous)
        controller.fail_once("repo.settle.switched")

        with self.assertRaisesRegex(BoundaryFailure, "repo.settle.switched"):
            service.execute(
                request(
                    LifecycleAction.UPDATE,
                    previous=previous,
                    candidate=artifact("2.0.0"),
                )
            )

        self.assertEqual(repository.operation.phase, LifecyclePhase.SWITCHED)
        self.assertNotIn("repo.rollback", log)

    def test_explicit_recovery_rereads_durable_phase_and_revalidates_activation(self) -> None:
        previous = record()
        service, repository, _, _, _, log = self.make_service(previous=previous)
        lifecycle_request = request(
            LifecycleAction.UPDATE,
            previous=previous,
            candidate=artifact("2.0.0"),
        )
        stale = LifecycleOperation(lifecycle_request, LifecyclePhase.CLAIMED, 0, previous)
        candidate = SelectedInstallation(
            lifecycle_request.candidate,
            "ref:data.2",
            previous.selected.approved_scopes,
            previous.selected.grant_generation + 1,
            previous.selected.activation_generation + 1,
        )
        repository.operation = LifecycleOperation(
            lifecycle_request,
            LifecyclePhase.ACTIVATION_VALIDATED,
            3,
            previous,
            candidate=candidate,
            frozen_data=FrozenData("ref:freeze.old", previous.selected.data_ref, 7),
            staged_data=StagedData("ref:data.2", 7, "ref:migration"),
            activation=ValidatedActivation("ref:stale.activation", candidate, 7),
        )

        result = service.recover_operation(stale)

        self.assertEqual(result.outcome, ReceiptOutcome.APPLIED)
        self.assertEqual(
            log,
            [
                "repo.recover",
                "repo.claim",
                "activation.validate",
                "repo.switch",
                "activation.admit",
                "repo.settle.switched",
            ],
        )

    def test_explicit_recovery_validates_the_supplied_request_binding(self) -> None:
        previous = record()
        service, repository, _, _, _, log = self.make_service(previous=previous)
        lifecycle_request = request(
            LifecycleAction.UPDATE,
            previous=previous,
            candidate=artifact("2.0.0"),
        )
        pending = LifecycleOperation(
            lifecycle_request,
            LifecyclePhase.QUIESCED,
            1,
            previous,
            frozen_data=FrozenData("ref:freeze.old", previous.selected.data_ref, 7),
        )
        repository.operation = pending
        mismatched = replace(
            pending,
            request=replace(lifecycle_request, request_digest="9" * 64),
        )

        with self.assertRaisesRegex(BoundaryFailure, "request.binding"):
            service.recover_operation(mismatched)

        self.assertEqual(log, ["repo.recover", "repo.claim"])

    def test_restoring_recovery_thaws_and_revalidates_before_admission(self) -> None:
        previous = record()
        service, repository, _, _, _, log = self.make_service(previous=previous)
        lifecycle_request = request(
            LifecycleAction.UPDATE,
            previous=previous,
            candidate=artifact("2.0.0"),
        )
        intended = LifecycleReceipt(lifecycle_request, previous, ReceiptOutcome.ABORTED)
        restoring = LifecycleOperation(
            lifecycle_request,
            LifecyclePhase.RESTORING,
            4,
            previous,
            frozen_data=FrozenData("ref:freeze.old", previous.selected.data_ref, 7),
            intended_receipt=intended,
        )
        repository.operation = restoring

        result = service.recover_operation(restoring)

        self.assertEqual(result, intended)
        self.assertEqual(
            log,
            [
                "repo.recover",
                "repo.claim",
                "data.thaw",
                "activation.validate",
                "activation.admit",
                "repo.settle.restoring",
            ],
        )

    def test_pending_listing_has_no_effects_and_requires_held_lease(self) -> None:
        service, repository, _, _, _, log = self.make_service()
        lifecycle_request = request(LifecycleAction.INSTALL, candidate=artifact("1.0.0"))
        repository.operation = LifecycleOperation(
            lifecycle_request,
            LifecyclePhase.CLAIMED,
            0,
            None,
        )
        self.assertEqual(service.list_pending(), (repository.operation,))
        self.assertEqual(log, ["repo.recover"])

        service._engine_lease.held = False
        with self.assertRaisesRegex(BoundaryFailure, "lease"):
            service.list_pending()

    def test_pending_read_failure_does_not_claim_or_execute(self) -> None:
        service, _, _, _, controller, log = self.make_service()
        controller.fail_once("repo.recover")

        with self.assertRaisesRegex(BoundaryFailure, "repo.recover"):
            service.list_pending()

        self.assertEqual(log, ["repo.recover"])

    def test_resolution_required_has_no_implicit_effects(
        self,
    ) -> None:
        previous = record()
        service, repository, _, _, _, log = self.make_service(previous=previous)
        lifecycle_request = request(
            LifecycleAction.UPDATE,
            previous=previous,
            candidate=artifact("2.0.0"),
        )
        current_selection = SelectedInstallation(
            lifecycle_request.candidate,
            "ref:data.2",
            previous.selected.approved_scopes,
            previous.selected.grant_generation + 1,
            previous.selected.activation_generation + 1,
        )
        repository.record = ExtensionRecord(
            EXTENSION_ID,
            previous.revision + 1,
            ExtensionStatus.ENABLED,
            current_selection,
        )
        intended = LifecycleReceipt(
            lifecycle_request,
            repository.record,
            ReceiptOutcome.APPLIED,
        )
        pending = LifecycleOperation(
            lifecycle_request,
            LifecyclePhase.RESOLUTION_REQUIRED,
            5,
            previous,
            candidate=current_selection,
            frozen_data=FrozenData("ref:freeze.old", previous.selected.data_ref, 7),
            intended_receipt=intended,
        )
        repository.operation = pending

        self.assertEqual(service.recover_operation(pending), pending)
        self.assertEqual(log, ["repo.recover", "repo.claim"])

        log.clear()
        result = service.recover_operation(pending, resolution_ref="ref:approved.export")
        self.assertEqual(result.outcome, ReceiptOutcome.ROLLED_BACK)
        self.assertEqual(
            log,
            [
                "repo.recover",
                "repo.claim",
                "data.freeze",
                "activation.revoke",
                "repo.rollback",
                "data.thaw",
                "activation.validate",
                "activation.admit",
                "repo.settle.restoring",
            ],
        )

    def test_shared_lease_rejects_same_operation_across_service_instances(self) -> None:
        previous = record(ExtensionStatus.DISABLED)
        service, repository, data, activation, _, log = self.make_service(previous=previous)
        second_repository = FailQuiescedAdvanceOnceRepository(repository)
        second_service = ExtensionLifecycleService(
            repository=second_repository,
            data_lifecycle=data,
            activation_lifecycle=activation,
            engine_lease=service._engine_lease,
        )
        lifecycle_request = request(LifecycleAction.ENABLE, previous=previous)
        candidate = replace(
            previous.selected,
            activation_generation=previous.selected.activation_generation + 1,
        )
        operation = LifecycleOperation(
            lifecycle_request,
            LifecyclePhase.ACTIVATION_VALIDATED,
            2,
            previous,
            candidate=candidate,
            frozen_data=FrozenData("ref:freeze.old", previous.selected.data_ref, 7),
            activation=ValidatedActivation("ref:stale.activation", candidate, 7),
        )
        repository.operation = operation
        activation.validate_entered = threading.Event()
        activation.validate_release = threading.Event()
        first_error: list[BaseException] = []

        def recover_first() -> None:
            try:
                service.recover_operation(operation)
            except BaseException as error:
                first_error.append(error)

        thread = threading.Thread(target=recover_first)
        thread.start()
        self.assertTrue(activation.validate_entered.wait(timeout=1))
        try:
            with self.assertRaises(LifecycleExecutionConflictError):
                second_service.recover_operation(operation)
        finally:
            activation.validate_release.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first_error, [])
        self.assertEqual(log.count("activation.validate"), 1)
        self.assertEqual(log.count("activation.revoke"), 0)

    def test_shared_lease_serializes_two_sqlite_wrappers_for_the_same_database(self) -> None:
        with TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "shared-lifecycle.sqlite3"
            first_repository = SQLiteExtensionLifecycleRepository(database_path)
            second_repository = SQLiteExtensionLifecycleRepository(database_path)
            lease = SharedHeldLease(first_repository, second_repository)
            data = StatefulDataLifecycle()
            activation = StatefulActivationLifecycle()
            first_service = ExtensionLifecycleService(
                repository=first_repository,
                data_lifecycle=data,
                activation_lifecycle=activation,
                engine_lease=lease,
            )
            second_service = ExtensionLifecycleService(
                repository=second_repository,
                data_lifecycle=data,
                activation_lifecycle=activation,
                engine_lease=lease,
            )
            installed_receipt = first_service.execute(
                request(
                    LifecycleAction.INSTALL,
                    candidate=artifact("1.0.0"),
                    suffix=41,
                )
            )
            assert isinstance(installed_receipt, LifecycleReceipt)
            installed = installed_receipt.record
            assert installed is not None
            enable_request = request(
                LifecycleAction.ENABLE,
                previous=installed,
                suffix=42,
            )
            claimed = first_repository.claim(enable_request).operation
            assert claimed is not None
            frozen = FrozenData("ref:freeze.shared", installed.selected.data_ref, 7)
            quiesced = first_repository.advance(
                replace(
                    claimed,
                    phase=LifecyclePhase.QUIESCED,
                    phase_revision=1,
                    frozen_data=frozen,
                ),
                expected_phase_revision=0,
            )
            candidate = replace(
                installed.selected,
                activation_generation=installed.selected.activation_generation + 1,
            )
            pending = first_repository.advance(
                replace(
                    quiesced,
                    phase=LifecyclePhase.ACTIVATION_VALIDATED,
                    phase_revision=2,
                    candidate=candidate,
                    activation=ValidatedActivation(
                        "ref:activation.staged",
                        candidate,
                        7,
                    ),
                ),
                expected_phase_revision=1,
            )
            activation.log.clear()
            activation.validate_entered = threading.Event()
            activation.validate_release = threading.Event()
            first_results: list[LifecycleReceipt | LifecycleOperation] = []
            first_errors: list[BaseException] = []

            def recover_first() -> None:
                try:
                    first_results.append(first_service.recover_operation(pending))
                except BaseException as error:
                    first_errors.append(error)

            thread = threading.Thread(target=recover_first)
            thread.start()
            self.assertTrue(activation.validate_entered.wait(timeout=1))
            try:
                with self.assertRaises(LifecycleExecutionConflictError):
                    second_service.recover_operation(pending)
            finally:
                activation.validate_release.set()
                thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(first_errors, [])
            self.assertEqual(len(first_results), 1)
            self.assertEqual(first_results[0].outcome, ReceiptOutcome.APPLIED)
            self.assertEqual(activation.log.count("validate"), 1)
            self.assertEqual(activation.log.count("admit"), 1)
            self.assertEqual(activation.log.count("revoke"), 0)


if __name__ == "__main__":
    unittest.main()
