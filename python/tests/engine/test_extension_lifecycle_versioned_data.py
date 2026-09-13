from __future__ import annotations

import threading
import unittest
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.storage.sqlite_extension_lifecycle import SQLiteExtensionLifecycleRepository
from model_deck.adapters.storage.sqlite_versioned_plugin_data import SQLiteVersionedPluginDataStore
from model_deck.engine.extensions.ports import (
    ExecutableArtifact,
    ExtensionRecord,
    ExtensionStatus,
    FrozenData,
    LifecycleAction,
    LifecycleConflictError,
    LifecyclePhase,
    LifecycleReceipt,
    LifecycleRequest,
    ReceiptOutcome,
    SelectedInstallation,
    ValidatedActivation,
)
from model_deck.engine.extensions.service import ExtensionLifecycleService, LifecycleExecutionConflictError
from model_deck.engine.plugin_data.ports import (
    PluginDataNotFoundError,
    PluginDataRepository,
    PluginDataRevisionConflictError,
)
from model_deck.engine.plugin_data.versioning import PluginDataBinding, PluginDataMigration


EXTENSION_ID = "org.example.versioned-data"


class _AdmissionFailure(RuntimeError):
    pass


def _artifact(version: str, digest_character: str) -> ExecutableArtifact:
    return ExecutableArtifact(
        digest_character * 64,
        EXTENSION_ID,
        version,
        ("data.read", "data.write"),
    )


def _request(
    action: LifecycleAction,
    *,
    suffix: int,
    previous: ExtensionRecord | None = None,
    candidate: ExecutableArtifact | None = None,
) -> LifecycleRequest:
    return LifecycleRequest(
        operation_id=f"30000000-0000-4000-8000-{suffix:012d}",
        principal_ref="ref:operator.integration",
        action=action,
        extension_id=EXTENSION_ID,
        expected_revision=0 if previous is None else previous.revision,
        request_digest=str(suffix % 10) * 64,
        idempotency_key=f"versioned-data-{suffix}",
        candidate=candidate,
    )


def _binding(selected: SelectedInstallation) -> PluginDataBinding:
    return PluginDataBinding(
        selected.executable.extension_id,
        selected.data_ref,
        selected.activation_generation,
    )


class _HeldLease:
    def __init__(self, repository: SQLiteExtensionLifecycleRepository) -> None:
        self._repository = repository
        self._guard = threading.Lock()
        self._owners: set[str] = set()

    def assert_held_for(self, repository: object) -> None:
        if repository is not self._repository:
            raise AssertionError("service used a repository outside the held lease")

    @contextmanager
    def execution_owner(self, repository: object, operation_id: str):
        self.assert_held_for(repository)
        with self._guard:
            if operation_id in self._owners:
                raise LifecycleExecutionConflictError()
            self._owners.add(operation_id)
        try:
            yield
        finally:
            with self._guard:
                self._owners.remove(operation_id)


class _StorageSynchronizingActivation:
    """In-memory authority state with real versioned-data synchronization."""

    def __init__(self, data_store: SQLiteVersionedPluginDataStore) -> None:
        self._data_store = data_store
        self._quiesced: dict[str, ExtensionRecord] = {}
        self._validation_freezes: dict[str, FrozenData] = {}
        self._admit_failures: dict[str, int] = {}
        self._revision_offsets: dict[str, int] = {}
        self.admissions: list[tuple[ExtensionStatus, int, int]] = []

    def fail_admit(self, operation_id: str, *, times: int = 1) -> None:
        self._admit_failures[operation_id] = times

    def offset_revision_once(self, operation_id: str, offset: int) -> None:
        self._revision_offsets[operation_id] = offset

    def quiesce(
        self,
        operation_id: str,
        previous: ExtensionRecord,
        *,
        deadline_ms: int,
    ) -> None:
        self._quiesced[operation_id] = previous

    def validate(
        self,
        operation_id: str,
        candidate: SelectedInstallation,
    ) -> ValidatedActivation:
        previous = self._quiesced.get(operation_id)
        if (
            previous is not None
            and candidate.data_ref == previous.selected.data_ref
            and candidate.activation_generation
            == previous.selected.activation_generation + 1
        ):
            frozen = self._data_store.freeze(operation_id, previous.selected)
            self._validation_freezes[operation_id] = frozen
            revision = frozen.final_revision
        elif previous is None or candidate.data_ref != previous.selected.data_ref:
            frozen = self._data_store.freeze(operation_id, candidate)
            self._validation_freezes[operation_id] = frozen
            revision = frozen.final_revision
        else:
            self._validation_freezes.pop(operation_id, None)
            revision = self._data_store.selected_revision(candidate)
        return ValidatedActivation(
            f"ref:activation.{operation_id}",
            candidate,
            revision,
        )

    def revoke(
        self,
        operation_id: str,
        activation: ValidatedActivation | None,
    ) -> str:
        return f"ref:revocation.{operation_id}"

    def admit(
        self,
        operation_id: str,
        record: ExtensionRecord,
        activation: ValidatedActivation | None,
        *,
        expected_data_revision: int,
    ) -> None:
        if record.status is ExtensionStatus.ENABLED:
            if activation is None or activation.selection != record.selected:
                raise AssertionError("enabled admission requires exact validation")
        elif activation is not None:
            raise AssertionError("non-serving admission cannot receive activation")

        failures = self._admit_failures.get(operation_id, 0)
        if failures:
            self._admit_failures[operation_id] = failures - 1
            raise _AdmissionFailure(operation_id)

        supplied_revision = expected_data_revision + self._revision_offsets.pop(
            operation_id,
            0,
        )
        self.admissions.append((record.status, expected_data_revision, supplied_revision))
        enabled = record.status is ExtensionStatus.ENABLED
        with self._data_store.mutation_barrier():
            if enabled:
                try:
                    self._data_store.activate_selected(
                        operation_id,
                        record.selected,
                        expected_data_revision=supplied_revision,
                        enabled=True,
                    )
                    return
                except LifecycleConflictError:
                    frozen = self._validation_freezes.get(operation_id)
                    if frozen is None:
                        raise
                    self._data_store.thaw(operation_id, frozen)
            else:
                previous = self._quiesced.get(operation_id)
                if self._is_normal_same_data_transition(previous, record):
                    assert previous is not None
                    frozen = self._data_store.freeze(operation_id, previous.selected)
                    self._data_store.thaw(operation_id, frozen)
            self._data_store.activate_selected(
                operation_id,
                record.selected,
                expected_data_revision=supplied_revision,
                enabled=enabled,
            )

    @staticmethod
    def _is_normal_same_data_transition(
        previous: ExtensionRecord | None,
        record: ExtensionRecord,
    ) -> bool:
        if previous is None or record.selected == previous.selected:
            return False
        selected = record.selected
        prior = previous.selected
        return (
            selected.data_ref == prior.data_ref
            and selected.executable == prior.executable
            and 0 <= selected.activation_generation - prior.activation_generation <= 1
            and 0 <= selected.grant_generation - prior.grant_generation <= 1
        )


class ExtensionLifecycleVersionedDataIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        self.v1 = _artifact("1.0.0", "a")
        self.v2 = _artifact("2.0.0", "b")
        self._configure()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _configure(
        self,
        migrations: Mapping[str, PluginDataMigration] | None = None,
    ) -> None:
        database_path = Path(self._temporary_directory.name) / "state.sqlite3"
        self.lifecycle_repository = SQLiteExtensionLifecycleRepository(database_path)
        self.data_store = SQLiteVersionedPluginDataStore(database_path, migrations=migrations)
        self.activation = _StorageSynchronizingActivation(self.data_store)
        self.service = self._service_for(self.activation)

    def _service_for(
        self,
        activation: _StorageSynchronizingActivation,
    ) -> ExtensionLifecycleService:
        return ExtensionLifecycleService(
            repository=self.lifecycle_repository,
            data_lifecycle=self.data_store,
            activation_lifecycle=activation,
            engine_lease=_HeldLease(self.lifecycle_repository),
        )

    def _execute_receipt(self, request: LifecycleRequest) -> LifecycleReceipt:
        result = self.service.execute(request)
        self.assertIsInstance(result, LifecycleReceipt)
        assert isinstance(result, LifecycleReceipt)
        return result

    def _install_and_enable(
        self,
        *,
        suffix: int,
    ) -> tuple[ExtensionRecord, PluginDataRepository]:
        install = self._execute_receipt(
            _request(LifecycleAction.INSTALL, suffix=suffix, candidate=self.v1)
        )
        assert install.record is not None
        enable = self._execute_receipt(
            _request(
                LifecycleAction.ENABLE,
                suffix=suffix + 1,
                previous=install.record,
            )
        )
        assert enable.record is not None
        return enable.record, self.data_store.repository_for(_binding(enable.record.selected))

    def test_install_enable_disable_update_and_tombstone_copy(self) -> None:
        install = self._execute_receipt(
            _request(LifecycleAction.INSTALL, suffix=1, candidate=self.v1)
        )
        assert install.record is not None
        installed_repository = self.data_store.repository_for(_binding(install.record.selected))
        self.assertEqual(installed_repository.list(EXTENSION_ID), [])
        with self.assertRaises(PluginDataRevisionConflictError):
            installed_repository.put(EXTENSION_ID, "closed", True)

        enable = self._execute_receipt(
            _request(LifecycleAction.ENABLE, suffix=2, previous=install.record)
        )
        assert enable.record is not None
        with self.assertRaises(PluginDataRevisionConflictError):
            installed_repository.list(EXTENSION_ID)
        enabled_repository = self.data_store.repository_for(_binding(enable.record.selected))
        enabled_repository.put(EXTENSION_ID, "live", {"version": 1})
        enabled_repository.put(EXTENSION_ID, "deleted", "old")
        enabled_repository.delete(EXTENSION_ID, "deleted", expected_revision=1)

        disable = self._execute_receipt(
            _request(LifecycleAction.DISABLE, suffix=3, previous=enable.record)
        )
        assert disable.record is not None
        with self.assertRaises(PluginDataRevisionConflictError):
            enabled_repository.list(EXTENSION_ID)
        disabled_repository = self.data_store.repository_for(_binding(disable.record.selected))
        self.assertEqual(disabled_repository.get(EXTENSION_ID, "live").value, {"version": 1})
        with self.assertRaises(PluginDataNotFoundError):
            disabled_repository.get(EXTENSION_ID, "deleted")
        with self.assertRaises(PluginDataRevisionConflictError):
            disabled_repository.put(EXTENSION_ID, "closed", True)

        update = self._execute_receipt(
            _request(
                LifecycleAction.UPDATE,
                suffix=4,
                previous=disable.record,
                candidate=self.v2,
            )
        )
        assert update.record is not None
        self.assertEqual(update.record.status, ExtensionStatus.DISABLED)
        with self.assertRaises(PluginDataRevisionConflictError):
            disabled_repository.list(EXTENSION_ID)
        updated_disabled = self.data_store.repository_for(_binding(update.record.selected))
        self.assertEqual(updated_disabled.get(EXTENSION_ID, "live").value, {"version": 1})
        with self.assertRaises(PluginDataNotFoundError):
            updated_disabled.get(EXTENSION_ID, "deleted")

        reenabled = self._execute_receipt(
            _request(LifecycleAction.ENABLE, suffix=5, previous=update.record)
        )
        assert reenabled.record is not None
        with self.assertRaises(PluginDataRevisionConflictError):
            updated_disabled.list(EXTENSION_ID)
        current = self.data_store.repository_for(_binding(reenabled.record.selected))
        restored = current.put(
            EXTENSION_ID,
            "deleted",
            "new",
            expected_revision=2,
        )
        self.assertEqual(restored.revision, 3)

    def test_disable_then_enable_preserves_the_selected_generation(self) -> None:
        enabled, repository = self._install_and_enable(suffix=60)
        repository.put(EXTENSION_ID, "retained", {"value": 1})

        disabled = self._execute_receipt(
            _request(LifecycleAction.DISABLE, suffix=62, previous=enabled)
        )
        assert disabled.record is not None
        reenabled = self._execute_receipt(
            _request(
                LifecycleAction.ENABLE,
                suffix=63,
                previous=disabled.record,
            )
        )

        assert reenabled.record is not None
        current = self.data_store.repository_for(_binding(reenabled.record.selected))
        self.assertEqual(
            current.get(EXTENSION_ID, "retained").value,
            {"value": 1},
        )
        self.assertEqual(current.put(EXTENSION_ID, "after-enable", True).revision, 1)

    def test_failed_migration_aborts_and_restores_old_writable_generation(self) -> None:
        def fail_migration(repository: PluginDataRepository) -> None:
            repository.put(EXTENSION_ID, "partial", True)
            raise RuntimeError("injected migration failure")

        self._configure(migrations={self.v2.artifact_id: fail_migration})
        enabled, old_repository = self._install_and_enable(suffix=10)
        old_repository.put(EXTENSION_ID, "stable", 1)
        operation = _request(
            LifecycleAction.UPDATE,
            suffix=12,
            previous=enabled,
            candidate=self.v2,
        )

        receipt = self._execute_receipt(operation)

        self.assertEqual(receipt.outcome, ReceiptOutcome.ABORTED)
        self.assertEqual(receipt.record, enabled)
        self.assertEqual(old_repository.get(EXTENSION_ID, "stable").value, 1)
        with self.assertRaises(PluginDataNotFoundError):
            old_repository.get(EXTENSION_ID, "partial")
        old_repository.put(EXTENSION_ID, "after-abort", True)
        partial = SelectedInstallation(
            self.v2,
            f"ref:plugin-data.{operation.operation_id}",
            enabled.selected.approved_scopes,
            enabled.selected.grant_generation + 1,
            enabled.selected.activation_generation + 1,
        )
        with self.assertRaises(PluginDataRevisionConflictError):
            self.data_store.repository_for(_binding(partial)).list(EXTENSION_ID)

    def test_failed_candidate_admission_rolls_back_and_revokes_candidate_binding(self) -> None:
        def migrate(repository: PluginDataRepository) -> None:
            repository.put(EXTENSION_ID, "candidate-only", True)

        self._configure(migrations={self.v2.artifact_id: migrate})
        enabled, old_repository = self._install_and_enable(suffix=20)
        old_repository.put(EXTENSION_ID, "stable", 1)
        operation = _request(
            LifecycleAction.UPDATE,
            suffix=22,
            previous=enabled,
            candidate=self.v2,
        )
        self.activation.fail_admit(operation.operation_id)

        receipt = self._execute_receipt(operation)

        self.assertEqual(receipt.outcome, ReceiptOutcome.ROLLED_BACK)
        assert receipt.record is not None
        with self.assertRaises(PluginDataRevisionConflictError):
            old_repository.list(EXTENSION_ID)
        restored = self.data_store.repository_for(_binding(receipt.record.selected))
        self.assertEqual(restored.get(EXTENSION_ID, "stable").value, 1)
        with self.assertRaises(PluginDataNotFoundError):
            restored.get(EXTENSION_ID, "candidate-only")
        candidate = SelectedInstallation(
            self.v2,
            f"ref:plugin-data.{operation.operation_id}",
            enabled.selected.approved_scopes,
            enabled.selected.grant_generation + 1,
            enabled.selected.activation_generation + 1,
        )
        with self.assertRaises(PluginDataRevisionConflictError):
            self.data_store.repository_for(_binding(candidate)).list(EXTENSION_ID)

    def test_recovered_reinstall_rollback_restores_retained_removed_tombstone(self) -> None:
        install = self._execute_receipt(
            _request(LifecycleAction.INSTALL, suffix=30, candidate=self.v1)
        )
        assert install.record is not None
        remove = self._execute_receipt(
            _request(LifecycleAction.REMOVE, suffix=31, previous=install.record)
        )
        assert remove.record is not None
        old_data_ref = remove.record.selected.data_ref
        operation = _request(
            LifecycleAction.INSTALL,
            suffix=32,
            previous=remove.record,
            candidate=self.v2,
        )
        self.activation.fail_admit(operation.operation_id, times=2)

        with self.assertRaises(_AdmissionFailure):
            self.service.execute(operation)
        pending = self.lifecycle_repository.recover()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].phase, LifecyclePhase.RESTORING)
        assert pending[0].intended_receipt is not None
        self.assertEqual(pending[0].intended_receipt.record.selected.data_ref, old_data_ref)

        recovered_service = self._service_for(
            _StorageSynchronizingActivation(self.data_store)
        )
        receipt = recovered_service.recover_operation(pending[0])

        self.assertIsInstance(receipt, LifecycleReceipt)
        assert isinstance(receipt, LifecycleReceipt) and receipt.record is not None
        self.assertEqual(receipt.outcome, ReceiptOutcome.ROLLED_BACK)
        self.assertEqual(receipt.record.status, ExtensionStatus.REMOVED)
        self.assertEqual(receipt.record.selected.data_ref, old_data_ref)
        self.assertEqual(
            self.data_store.repository_for(_binding(receipt.record.selected)).list(EXTENSION_ID),
            [],
        )
        candidate = SelectedInstallation(
            self.v2,
            f"ref:plugin-data.{operation.operation_id}",
            (),
            remove.record.selected.grant_generation + 1,
            remove.record.selected.activation_generation + 1,
        )
        with self.assertRaises(PluginDataRevisionConflictError):
            self.data_store.repository_for(_binding(candidate)).list(EXTENSION_ID)

    def test_recovered_first_install_rollback_binds_removed_tombstone(self) -> None:
        operation = _request(LifecycleAction.INSTALL, suffix=35, candidate=self.v1)
        self.activation.fail_admit(operation.operation_id, times=2)

        with self.assertRaises(_AdmissionFailure):
            self.service.execute(operation)
        pending = self.lifecycle_repository.recover()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].phase, LifecyclePhase.RESTORING)

        recovered_service = self._service_for(
            _StorageSynchronizingActivation(self.data_store)
        )
        receipt = recovered_service.recover_operation(pending[0])

        self.assertIsInstance(receipt, LifecycleReceipt)
        assert isinstance(receipt, LifecycleReceipt) and receipt.record is not None
        self.assertEqual(receipt.outcome, ReceiptOutcome.ROLLED_BACK)
        self.assertEqual(receipt.record.status, ExtensionStatus.REMOVED)
        self.assertEqual(
            self.data_store.repository_for(_binding(receipt.record.selected)).list(
                EXTENSION_ID
            ),
            [],
        )
        self.assertEqual(self.lifecycle_repository.recover(), ())

    def test_mismatched_admission_revision_cannot_publish_install(self) -> None:
        operation = _request(LifecycleAction.INSTALL, suffix=40, candidate=self.v1)
        self.activation.offset_revision_once(operation.operation_id, 1)

        receipt = self._execute_receipt(operation)

        self.assertEqual(receipt.outcome, ReceiptOutcome.ROLLED_BACK)
        assert receipt.record is not None
        self.assertEqual(receipt.record.status, ExtensionStatus.REMOVED)
        self.assertEqual(
            self.activation.admissions[:2],
            [
                (ExtensionStatus.INSTALLED, 0, 1),
                (ExtensionStatus.REMOVED, 0, 0),
            ],
        )
        self.assertEqual(
            self.data_store.repository_for(_binding(receipt.record.selected)).list(EXTENSION_ID),
            [],
        )


if __name__ == "__main__":
    unittest.main()
