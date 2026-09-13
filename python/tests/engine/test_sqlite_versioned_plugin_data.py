import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.storage.sqlite_versioned_plugin_data import (
    SQLiteVersionedPluginDataStore,
)
from model_deck.engine.extensions.ports import (
    ExecutableArtifact,
    LifecycleConflictError,
    SelectedInstallation,
)
from model_deck.engine.plugin_data.ports import (
    PluginDataNotFoundError,
    PluginDataQuota,
    PluginDataQuotaExceededError,
    PluginDataRepository,
    PluginDataRevisionConflictError,
)
from model_deck.engine.plugin_data.versioning import (
    PluginDataBinding,
    VersionedPluginDataStore,
)


NAMESPACE = "com.example.notes"
OTHER_NAMESPACE = "com.example.other"
INSTALL_OPERATION = "20000000-0000-4000-8000-000000000001"
FREEZE_OPERATION = "20000000-0000-4000-8000-000000000002"
UPDATE_OPERATION = "20000000-0000-4000-8000-000000000003"
OTHER_OPERATION = "20000000-0000-4000-8000-000000000004"
REFREEZE_OPERATION = "20000000-0000-4000-8000-000000000005"


def _artifact(version: str, digest_character: str) -> ExecutableArtifact:
    return ExecutableArtifact(
        digest_character * 64,
        NAMESPACE,
        version,
        ("data.read", "data.write"),
    )


class SQLiteVersionedPluginDataContractTests(unittest.TestCase):
    def test_implements_versioned_store_protocol(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteVersionedPluginDataStore(Path(directory) / "data.sqlite3")
            self.assertIsInstance(store, VersionedPluginDataStore)
            self.assertIs(store.mutation_barrier(), store.mutation_barrier())


class SQLiteVersionedPluginDataBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.db_path = Path(self.temporary_directory.name) / "data.sqlite3"
        self.v1 = _artifact("1.0.0", "a")
        self.v2 = _artifact("2.0.0", "b")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _enabled_install(
        self,
        store: SQLiteVersionedPluginDataStore,
    ) -> tuple[SelectedInstallation, PluginDataRepository]:
        staged = store.stage(INSTALL_OPERATION, self.v1, None)
        selected = SelectedInstallation(
            self.v1,
            staged.data_ref,
            (),
            grant_generation=0,
            activation_generation=0,
        )
        store.activate_selected(
            INSTALL_OPERATION,
            selected,
            expected_data_revision=staged.initial_revision,
            enabled=True,
        )
        repository = store.repository_for(
            PluginDataBinding(NAMESPACE, staged.data_ref, 0)
        )
        return selected, repository

    def test_freeze_and_write_race_is_ordered_by_the_shared_barrier(self) -> None:
        store = SQLiteVersionedPluginDataStore(self.db_path)
        selected, repository = self._enabled_install(store)
        competing_store = SQLiteVersionedPluginDataStore(self.db_path)
        competing_repository = competing_store.repository_for(
            PluginDataBinding(NAMESPACE, selected.data_ref, 0)
        )
        repository.put(NAMESPACE, "before", {"value": 1})
        with self.assertRaises(LifecycleConflictError):
            store.freeze(
                OTHER_OPERATION,
                replace(selected, executable=self.v2),
            )
        start = threading.Barrier(3)
        results: dict[str, object] = {}

        def write() -> None:
            start.wait()
            try:
                results["write"] = competing_repository.put(NAMESPACE, "racing", 2)
            except PluginDataRevisionConflictError as error:
                results["write"] = error

        def freeze() -> None:
            start.wait()
            results["freeze"] = store.freeze(FREEZE_OPERATION, selected)

        with ThreadPoolExecutor(max_workers=2) as executor:
            writer = executor.submit(write)
            freezer = executor.submit(freeze)
            start.wait()
            writer.result()
            freezer.result()

        frozen = results["freeze"]
        self.assertEqual(frozen.data_ref, selected.data_ref)
        if isinstance(results["write"], PluginDataRevisionConflictError):
            self.assertEqual(frozen.final_revision, 1)
            with self.assertRaises(PluginDataNotFoundError):
                repository.get(NAMESPACE, "racing")
        else:
            self.assertEqual(frozen.final_revision, 2)
            self.assertEqual(repository.get(NAMESPACE, "racing").value, 2)
        with self.assertRaises(PluginDataRevisionConflictError):
            repository.put(NAMESPACE, "after", 3)
        self.assertEqual(store.freeze(FREEZE_OPERATION, selected), frozen)
        with self.assertRaises(LifecycleConflictError):
            store.freeze(
                FREEZE_OPERATION,
                replace(
                    selected,
                    approved_scopes=("data.read",),
                    grant_generation=1,
                ),
            )
        with self.assertRaises(LifecycleConflictError):
            store.freeze(OTHER_OPERATION, selected)

    def test_stale_activation_and_disabled_binding_cannot_mutate(self) -> None:
        store = SQLiteVersionedPluginDataStore(self.db_path)
        selected, old_repository = self._enabled_install(store)
        old_repository.put(NAMESPACE, "kept", 1)
        disabled = replace(selected, activation_generation=1)
        with self.assertRaises(LifecycleConflictError):
            store.activate_selected(
                OTHER_OPERATION,
                disabled,
                expected_data_revision=0,
                enabled=False,
            )
        store.activate_selected(
            OTHER_OPERATION,
            disabled,
            expected_data_revision=1,
            enabled=False,
        )
        with self.assertRaises(PluginDataRevisionConflictError):
            old_repository.put(NAMESPACE, "stale-write", 2)
        current_repository = store.repository_for(
            PluginDataBinding(NAMESPACE, selected.data_ref, 1)
        )
        self.assertEqual(current_repository.get(NAMESPACE, "kept").value, 1)
        with self.assertRaises(PluginDataRevisionConflictError):
            current_repository.put(NAMESPACE, "disabled-write", 3)
        with self.assertRaises(PluginDataRevisionConflictError):
            current_repository.delete(NAMESPACE, "kept")
        frozen = store.freeze(FREEZE_OPERATION, disabled)
        self.assertEqual(frozen.final_revision, 1)

    def test_activation_receipts_reject_equal_generation_state_revival(self) -> None:
        store = SQLiteVersionedPluginDataStore(self.db_path)
        selected, repository = self._enabled_install(store)
        store.activate_selected(
            INSTALL_OPERATION,
            selected,
            expected_data_revision=0,
            enabled=True,
        )
        with self.assertRaises(LifecycleConflictError):
            store.activate_selected(
                INSTALL_OPERATION,
                replace(selected, grant_generation=1),
                expected_data_revision=0,
                enabled=True,
            )
        with self.assertRaises(LifecycleConflictError):
            store.activate_selected(
                INSTALL_OPERATION,
                selected,
                expected_data_revision=0,
                enabled=False,
            )
        with self.assertRaises(LifecycleConflictError):
            store.activate_selected(
                OTHER_OPERATION,
                selected,
                expected_data_revision=0,
                enabled=False,
            )
        self.assertEqual(repository.put(NAMESPACE, "still-writable", 1).revision, 1)

        disabled = replace(selected, activation_generation=1)
        store.activate_selected(
            FREEZE_OPERATION,
            disabled,
            expected_data_revision=1,
            enabled=False,
        )
        with self.assertRaises(LifecycleConflictError):
            store.activate_selected(
                UPDATE_OPERATION,
                disabled,
                expected_data_revision=1,
                enabled=True,
            )
        disabled_repository = store.repository_for(
            PluginDataBinding(NAMESPACE, selected.data_ref, 1)
        )
        with self.assertRaises(PluginDataRevisionConflictError):
            disabled_repository.put(NAMESPACE, "revived", 1)

    def test_same_operation_thaw_restores_prior_generation_but_foreign_operation_cannot(self) -> None:
        store = SQLiteVersionedPluginDataStore(self.db_path)
        selected, repository = self._enabled_install(store)
        frozen = store.freeze(FREEZE_OPERATION, selected)
        store.thaw(FREEZE_OPERATION, frozen)
        store.thaw(FREEZE_OPERATION, frozen)
        with self.assertRaises(LifecycleConflictError):
            store.activate_selected(
                OTHER_OPERATION,
                selected,
                expected_data_revision=0,
                enabled=True,
            )
        store.activate_selected(
            FREEZE_OPERATION,
            selected,
            expected_data_revision=0,
            enabled=True,
        )
        with self.assertRaises(LifecycleConflictError):
            store.freeze(FREEZE_OPERATION, selected)

        refrozen = store.freeze(REFREEZE_OPERATION, selected)
        self.assertEqual(refrozen.final_revision, 0)
        with self.assertRaises(LifecycleConflictError):
            store.freeze(FREEZE_OPERATION, selected)
        with self.assertRaises(LifecycleConflictError):
            store.thaw(FREEZE_OPERATION, frozen)
        with self.assertRaises(LifecycleConflictError):
            store.stage(UPDATE_OPERATION, self.v2, frozen)
        self.assertEqual(store.freeze(REFREEZE_OPERATION, selected), refrozen)
        store.thaw(REFREEZE_OPERATION, refrozen)
        store.activate_selected(
            REFREEZE_OPERATION,
            selected,
            expected_data_revision=0,
            enabled=True,
        )

        repository.put(NAMESPACE, "restored", True)
        with self.assertRaises(LifecycleConflictError):
            store.thaw(FREEZE_OPERATION, frozen)

        newer = replace(selected, activation_generation=1)
        store.activate_selected(
            UPDATE_OPERATION,
            newer,
            expected_data_revision=1,
            enabled=True,
        )
        with self.assertRaises(LifecycleConflictError):
            store.freeze(FREEZE_OPERATION, selected)

    def test_quota_and_namespace_data_reference_scoping_use_bound_crud(self) -> None:
        quota = PluginDataQuota(max_bytes=1_000_000, max_keys=1, max_value_bytes=100)
        store = SQLiteVersionedPluginDataStore(self.db_path, quota=quota)
        selected, repository = self._enabled_install(store)
        repository.put(NAMESPACE, "one", {"ok": True})
        with self.assertRaises(PluginDataQuotaExceededError):
            repository.put(NAMESPACE, "two", {"no": True})
        with self.assertRaises(PluginDataRevisionConflictError):
            repository.get(OTHER_NAMESPACE, "one")
        wrong_namespace = store.repository_for(
            PluginDataBinding(OTHER_NAMESPACE, selected.data_ref, 0)
        )
        with self.assertRaises(PluginDataRevisionConflictError):
            wrong_namespace.list(OTHER_NAMESPACE)
        wrong_data = store.repository_for(
            PluginDataBinding(NAMESPACE, "ref:plugin-data.unknown", 0)
        )
        with self.assertRaises(PluginDataRevisionConflictError):
            wrong_data.list(NAMESPACE)

    def test_update_copy_preserves_tombstones_and_separate_dataset_revision(self) -> None:
        store = SQLiteVersionedPluginDataStore(self.db_path)
        selected, repository = self._enabled_install(store)
        repository.put(NAMESPACE, "deleted", {"old": True})
        repository.delete(NAMESPACE, "deleted", expected_revision=1)
        repository.put(NAMESPACE, "live", {"value": 1})
        frozen = store.freeze(FREEZE_OPERATION, selected)
        self.assertEqual(frozen.final_revision, 3)

        staged = store.stage(UPDATE_OPERATION, self.v2, frozen)
        self.assertEqual(staged.initial_revision, 3)
        self.assertEqual(repository.get(NAMESPACE, "live").value, {"value": 1})
        updated = SelectedInstallation(
            self.v2,
            staged.data_ref,
            (),
            grant_generation=1,
            activation_generation=1,
        )
        with self.assertRaises(LifecycleConflictError):
            store.freeze(OTHER_OPERATION, updated)
        store.activate_selected(
            UPDATE_OPERATION,
            updated,
            expected_data_revision=3,
            enabled=True,
        )
        updated_repository = store.repository_for(
            PluginDataBinding(NAMESPACE, staged.data_ref, 1)
        )
        with self.assertRaises(PluginDataNotFoundError):
            updated_repository.get(NAMESPACE, "deleted")
        restored = updated_repository.put(
            NAMESPACE,
            "deleted",
            {"new": True},
            expected_revision=2,
        )
        self.assertEqual(restored.revision, 3)
        self.assertEqual(updated_repository.get(NAMESPACE, "live").revision, 1)
        updated_frozen = store.freeze(OTHER_OPERATION, updated)
        self.assertEqual(updated_frozen.final_revision, 4)
        self.assertEqual(store.stage(UPDATE_OPERATION, self.v2, frozen), staged)
        with self.assertRaises(LifecycleConflictError):
            store.activate_selected(
                OTHER_OPERATION,
                selected,
                expected_data_revision=3,
                enabled=False,
            )
        with self.assertRaises(PluginDataRevisionConflictError):
            repository.get(NAMESPACE, "live")

    def test_failed_migration_is_nonselectable_and_retry_recreates_stage(self) -> None:
        attempts = 0

        def migrate(repository: PluginDataRepository) -> None:
            nonlocal attempts
            attempts += 1
            self.assertIsInstance(repository, PluginDataRepository)
            repository.put(NAMESPACE, "partial", {"attempt": attempts})
            if attempts == 1:
                raise RuntimeError("injected migration failure")
            repository.put(NAMESPACE, "complete", True)

        store = SQLiteVersionedPluginDataStore(
            self.db_path,
            migrations={self.v2.artifact_id: migrate},
        )
        selected, source = self._enabled_install(store)
        source.put(NAMESPACE, "source", 1)
        frozen = store.freeze(FREEZE_OPERATION, selected)
        with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
            store.stage(UPDATE_OPERATION, self.v2, frozen)

        partial_binding = PluginDataBinding(
            NAMESPACE,
            f"ref:plugin-data.{UPDATE_OPERATION}",
            1,
        )
        with self.assertRaises(PluginDataRevisionConflictError):
            store.repository_for(partial_binding).list(NAMESPACE)
        self.assertEqual(source.get(NAMESPACE, "source").value, 1)
        with self.assertRaises(PluginDataNotFoundError):
            source.get(NAMESPACE, "partial")

        staged = store.stage(UPDATE_OPERATION, self.v2, frozen)
        self.assertEqual(attempts, 2)
        self.assertEqual(staged.initial_revision, frozen.final_revision + 2)
        updated = SelectedInstallation(self.v2, staged.data_ref, (), 1, 1)
        store.activate_selected(
            UPDATE_OPERATION,
            updated,
            expected_data_revision=staged.initial_revision,
            enabled=True,
        )
        migrated = store.repository_for(
            PluginDataBinding(NAMESPACE, staged.data_ref, 1)
        )
        self.assertEqual(migrated.get(NAMESPACE, "partial").value, {"attempt": 2})
        self.assertTrue(migrated.get(NAMESPACE, "complete").value)
        replay = store.stage(UPDATE_OPERATION, self.v2, frozen)
        self.assertEqual(replay, staged)
        self.assertEqual(attempts, 2)

    def test_concurrent_stage_retry_fences_the_old_callback_incarnation(self) -> None:
        source_store = SQLiteVersionedPluginDataStore(self.db_path)
        selected, source = self._enabled_install(source_store)
        source.put(NAMESPACE, "source", 1)
        frozen = source_store.freeze(FREEZE_OPERATION, selected)
        old_callback_started = threading.Event()
        release_old_callback = threading.Event()

        def old_migration(repository: PluginDataRepository) -> None:
            repository.put(NAMESPACE, "old-before", True)
            old_callback_started.set()
            self.assertTrue(release_old_callback.wait(timeout=5))
            repository.put(NAMESPACE, "old-after", True)

        def new_migration(repository: PluginDataRepository) -> None:
            repository.put(NAMESPACE, "new-only", True)

        old_store = SQLiteVersionedPluginDataStore(
            self.db_path,
            migrations={self.v2.artifact_id: old_migration},
        )
        new_store = SQLiteVersionedPluginDataStore(
            self.db_path,
            migrations={self.v2.artifact_id: new_migration},
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            old_stage = executor.submit(
                old_store.stage,
                UPDATE_OPERATION,
                self.v2,
                frozen,
            )
            self.assertTrue(old_callback_started.wait(timeout=5))
            staged = new_store.stage(UPDATE_OPERATION, self.v2, frozen)
            release_old_callback.set()
            with self.assertRaises(PluginDataRevisionConflictError):
                old_stage.result()

        updated = SelectedInstallation(self.v2, staged.data_ref, (), 1, 1)
        new_store.activate_selected(
            UPDATE_OPERATION,
            updated,
            expected_data_revision=staged.initial_revision,
            enabled=True,
        )
        repository = new_store.repository_for(
            PluginDataBinding(NAMESPACE, staged.data_ref, 1)
        )
        self.assertTrue(repository.get(NAMESPACE, "new-only").value)
        for rejected_key in ("old-before", "old-after"):
            with self.assertRaises(PluginDataNotFoundError):
                repository.get(NAMESPACE, rejected_key)

    def test_thaw_does_not_write_until_exact_generation_is_activated(self) -> None:
        store = SQLiteVersionedPluginDataStore(self.db_path)
        selected, repository = self._enabled_install(store)
        frozen = store.freeze(FREEZE_OPERATION, selected)
        store.thaw(FREEZE_OPERATION, frozen)
        with self.assertRaises(PluginDataRevisionConflictError):
            repository.put(NAMESPACE, "too-early", 1)
        restored = replace(selected, activation_generation=1)
        store.activate_selected(
            OTHER_OPERATION,
            restored,
            expected_data_revision=0,
            enabled=True,
        )
        with self.assertRaises(PluginDataRevisionConflictError):
            repository.put(NAMESPACE, "stale", 1)
        restored_repository = store.repository_for(
            PluginDataBinding(NAMESPACE, restored.data_ref, 1)
        )
        self.assertEqual(restored_repository.put(NAMESPACE, "ready", 1).revision, 1)


if __name__ == "__main__":
    unittest.main()
