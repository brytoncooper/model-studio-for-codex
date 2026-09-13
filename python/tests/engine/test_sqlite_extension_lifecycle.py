import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.storage.sqlite_extension_lifecycle import (
    SQLiteExtensionLifecycleRepository,
)
from model_deck.engine.extensions.ports import (
    ClaimDisposition,
    ExecutableArtifact,
    ExtensionLifecycleRepository,
    ExtensionRecord,
    ExtensionStatus,
    FrozenData,
    LifecycleAction,
    LifecycleConflictError,
    LifecycleIdempotencyConflictError,
    LifecyclePhase,
    LifecycleRequest,
    LifecycleResolutionRequiredError,
    ReceiptOutcome,
    SelectedInstallation,
    StagedData,
    ValidatedActivation,
)


EXTENSION_ID = "org.example.lifecycle"
OP_INSTALL = "10000000-0000-4000-8000-000000000001"
OP_ENABLE = "10000000-0000-4000-8000-000000000002"
OP_UPDATE = "10000000-0000-4000-8000-000000000003"
OP_OTHER = "10000000-0000-4000-8000-000000000004"
OP_GRANTS = "10000000-0000-4000-8000-000000000005"
OP_DISABLE = "10000000-0000-4000-8000-000000000006"
OP_REMOVE = "10000000-0000-4000-8000-000000000007"
OP_REINSTALL = "10000000-0000-4000-8000-000000000008"


def _artifact(version: str, digest_character: str) -> ExecutableArtifact:
    return ExecutableArtifact(
        digest_character * 64,
        EXTENSION_ID,
        version,
        ("data.read", "data.write"),
    )


def _request(
    operation_id: str,
    action: LifecycleAction,
    revision: int,
    key: str,
    *,
    digest_character: str,
    candidate: ExecutableArtifact | None = None,
    approved_scopes: tuple[str, ...] = (),
    principal: str = "ref:operator",
) -> LifecycleRequest:
    return LifecycleRequest(
        operation_id=operation_id,
        principal_ref=principal,
        action=action,
        extension_id=EXTENSION_ID,
        expected_revision=revision,
        request_digest=digest_character * 64,
        idempotency_key=key,
        candidate=candidate,
        approved_scopes=approved_scopes,
    )


class SQLiteExtensionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "extension-lifecycle.sqlite3"
        self.repo = SQLiteExtensionLifecycleRepository(self.db_path)
        self.v1 = _artifact("1.0.0", "a")
        self.v2 = _artifact("2.0.0", "b")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _install(self) -> ExtensionRecord:
        request = _request(
            OP_INSTALL,
            LifecycleAction.INSTALL,
            0,
            "install-1",
            digest_character="1",
            candidate=self.v1,
        )
        claimed = self.repo.claim(request).operation
        assert claimed is not None
        quiesced = self.repo.advance(
            replace(claimed, phase=LifecyclePhase.QUIESCED, phase_revision=1),
            expected_phase_revision=0,
        )
        candidate = SelectedInstallation(self.v1, "ref:data.v1", (), 0, 0)
        staged = self.repo.advance(
            replace(
                quiesced,
                phase=LifecyclePhase.DATA_STAGED,
                phase_revision=2,
                candidate=candidate,
                staged_data=StagedData("ref:data.v1", 0, "ref:migration.v1"),
            ),
            expected_phase_revision=1,
        )
        switched = self.repo.switch(OP_INSTALL, expected_phase_revision=2)
        receipt = self.repo.settle(OP_INSTALL, expected_phase_revision=switched.phase_revision)
        assert receipt.record is not None
        return receipt.record

    def _enable(self, record: ExtensionRecord) -> ExtensionRecord:
        request = _request(
            OP_ENABLE,
            LifecycleAction.ENABLE,
            record.revision,
            "enable-1",
            digest_character="2",
        )
        claimed = self.repo.claim(request).operation
        assert claimed is not None
        frozen = FrozenData("ref:freeze.v1", record.selected.data_ref, 0)
        quiesced = self.repo.advance(
            replace(
                claimed,
                phase=LifecyclePhase.QUIESCED,
                phase_revision=1,
                frozen_data=frozen,
            ),
            expected_phase_revision=0,
        )
        candidate = replace(
            record.selected,
            activation_generation=record.selected.activation_generation + 1,
        )
        validated = self.repo.advance(
            replace(
                quiesced,
                phase=LifecyclePhase.ACTIVATION_VALIDATED,
                phase_revision=2,
                candidate=candidate,
                activation=ValidatedActivation("ref:activation.v1", candidate, 0),
            ),
            expected_phase_revision=1,
        )
        switched = self.repo.switch(OP_ENABLE, expected_phase_revision=validated.phase_revision)
        receipt = self.repo.settle(OP_ENABLE, expected_phase_revision=switched.phase_revision)
        assert receipt.record is not None
        return receipt.record

    def test_implements_protocol_and_persists_exact_replay_first(self) -> None:
        self.assertIsInstance(self.repo, ExtensionLifecycleRepository)
        request = _request(
            OP_INSTALL,
            LifecycleAction.INSTALL,
            0,
            "install-1",
            digest_character="1",
            candidate=self.v1,
        )
        first = self.repo.claim(request)
        self.assertEqual(first.disposition, ClaimDisposition.ADMITTED)
        self.assertEqual(self.repo.claim(request).disposition, ClaimDisposition.IN_PROGRESS)

        record = self._finish_claimed_install(first.operation)
        later = self._enable(record)
        self.assertEqual(later.status, ExtensionStatus.ENABLED)

        reopened = SQLiteExtensionLifecycleRepository(self.db_path)
        replay = reopened.claim(request)
        self.assertEqual(replay.disposition, ClaimDisposition.REPLAY)
        self.assertEqual(replay.receipt.record, record)
        self.assertEqual(replay.receipt.outcome, ReceiptOutcome.APPLIED)
        self.assertNotEqual(replay.receipt.record, reopened.get(EXTENSION_ID))

    def _finish_claimed_install(self, claimed) -> ExtensionRecord:
        assert claimed is not None
        quiesced = self.repo.advance(
            replace(claimed, phase=LifecyclePhase.QUIESCED, phase_revision=1),
            expected_phase_revision=0,
        )
        candidate = SelectedInstallation(self.v1, "ref:data.v1", (), 0, 0)
        staged = self.repo.advance(
            replace(
                quiesced,
                phase=LifecyclePhase.DATA_STAGED,
                phase_revision=2,
                candidate=candidate,
                staged_data=StagedData("ref:data.v1", 0, "ref:migration.v1"),
            ),
            expected_phase_revision=1,
        )
        switched = self.repo.switch(OP_INSTALL, expected_phase_revision=staged.phase_revision)
        receipt = self.repo.settle(OP_INSTALL, expected_phase_revision=switched.phase_revision)
        assert receipt.record is not None
        return receipt.record

    def test_idempotency_digest_and_active_extension_claims_conflict_without_mutation(self) -> None:
        request = _request(
            OP_INSTALL,
            LifecycleAction.INSTALL,
            0,
            "same-key",
            digest_character="1",
            candidate=self.v1,
        )
        admitted = self.repo.claim(request)
        with self.assertRaises(LifecycleIdempotencyConflictError):
            self.repo.claim(replace(request, request_digest="9" * 64))
        competing = _request(
            OP_OTHER,
            LifecycleAction.INSTALL,
            0,
            "other-key",
            digest_character="3",
            candidate=self.v1,
            principal="ref:other-operator",
        )
        with self.assertRaises(LifecycleConflictError):
            self.repo.claim(competing)
        self.assertEqual(self.repo.claim(request), replace(admitted, disposition=ClaimDisposition.IN_PROGRESS))

    def test_phase_cas_rejects_skips_and_mutated_bindings(self) -> None:
        request = _request(
            OP_INSTALL,
            LifecycleAction.INSTALL,
            0,
            "install-1",
            digest_character="1",
            candidate=self.v1,
        )
        claimed = self.repo.claim(request).operation
        assert claimed is not None
        with self.assertRaises(LifecycleConflictError):
            self.repo.advance(
                replace(claimed, phase=LifecyclePhase.DATA_STAGED, phase_revision=1),
                expected_phase_revision=0,
            )
        with self.assertRaises(LifecycleConflictError):
            self.repo.advance(
                replace(claimed, request=replace(request, request_digest="4" * 64),
                        phase=LifecyclePhase.QUIESCED, phase_revision=1),
                expected_phase_revision=0,
            )
        quiesced = self.repo.advance(
            replace(claimed, phase=LifecyclePhase.QUIESCED, phase_revision=1),
            expected_phase_revision=0,
        )
        with self.assertRaises(LifecycleConflictError):
            self.repo.advance(
                replace(quiesced, phase=LifecyclePhase.DATA_STAGED, phase_revision=2,
                        candidate=SelectedInstallation(self.v1, "ref:forged", (), 7, 7),
                        staged_data=StagedData("ref:data.v1", 0, "ref:migration.v1")),
                expected_phase_revision=1,
            )
        self.assertEqual(self.repo.recover()[0], quiesced)

    def test_abort_is_recoverable_until_settle_and_releases_claim_afterward(self) -> None:
        request = _request(
            OP_INSTALL,
            LifecycleAction.INSTALL,
            0,
            "install-abort",
            digest_character="4",
            candidate=self.v1,
        )
        claimed = self.repo.claim(request).operation
        assert claimed is not None
        restoring = self.repo.abort(
            OP_INSTALL,
            expected_phase_revision=0,
            revocation_ref="ref:revoked.install",
        )
        self.assertEqual(restoring.phase, LifecyclePhase.RESTORING)
        self.assertEqual(restoring.intended_receipt.outcome, ReceiptOutcome.ABORTED)
        self.assertEqual(SQLiteExtensionLifecycleRepository(self.db_path).recover(), (restoring,))
        with self.assertRaises(LifecycleConflictError):
            self.repo.claim(
                _request(OP_OTHER, LifecycleAction.INSTALL, 0, "blocked", digest_character="5", candidate=self.v1)
            )
        receipt = self.repo.settle(OP_INSTALL, expected_phase_revision=restoring.phase_revision)
        self.assertEqual(receipt.outcome, ReceiptOutcome.ABORTED)
        self.assertIsNone(receipt.record)
        self.assertEqual(self.repo.recover(), ())

    def test_abort_atomically_persists_completed_unadvanced_freeze(self) -> None:
        installed = self._install()
        enabled = self._enable(installed)
        request = _request(
            OP_UPDATE,
            LifecycleAction.UPDATE,
            enabled.revision,
            "update-abort-freeze",
            digest_character="8",
            candidate=self.v2,
        )
        claimed = self.repo.claim(request).operation
        assert claimed is not None
        frozen = FrozenData("ref:freeze.completed", enabled.selected.data_ref, 11)

        restoring = self.repo.abort(
            OP_UPDATE,
            expected_phase_revision=claimed.phase_revision,
            revocation_ref="ref:revoked.update",
            frozen_data=frozen,
        )

        self.assertEqual(restoring.phase, LifecyclePhase.RESTORING)
        self.assertEqual(restoring.frozen_data, frozen)
        reopened = SQLiteExtensionLifecycleRepository(self.db_path)
        self.assertEqual(reopened.recover(), (restoring,))

    def test_abort_rejects_mismatched_or_changed_freeze_evidence(self) -> None:
        installed = self._install()
        request = _request(
            OP_UPDATE,
            LifecycleAction.UPDATE,
            installed.revision,
            "update-abort-invalid-freeze",
            digest_character="9",
            candidate=self.v2,
        )
        claimed = self.repo.claim(request).operation
        assert claimed is not None
        with self.assertRaises(LifecycleConflictError):
            self.repo.abort(
                OP_UPDATE,
                expected_phase_revision=claimed.phase_revision,
                revocation_ref="ref:revoked.update",
                frozen_data=FrozenData("ref:freeze.wrong", "ref:other.data", 0),
            )
        self.assertEqual(self.repo.recover(), (claimed,))

        frozen = FrozenData("ref:freeze.original", installed.selected.data_ref, 0)
        quiesced = self.repo.advance(
            replace(
                claimed,
                phase=LifecyclePhase.QUIESCED,
                phase_revision=1,
                frozen_data=frozen,
            ),
            expected_phase_revision=0,
        )
        with self.assertRaises(LifecycleConflictError):
            self.repo.abort(
                OP_UPDATE,
                expected_phase_revision=quiesced.phase_revision,
                revocation_ref="ref:revoked.update",
                frozen_data=replace(frozen, freeze_ref="ref:freeze.changed"),
            )
        self.assertEqual(self.repo.recover(), (quiesced,))

    def test_enabled_update_switch_and_rollback_use_monotonic_generations(self) -> None:
        installed = self._install()
        enabled = self._enable(installed)
        request = _request(
            OP_UPDATE,
            LifecycleAction.UPDATE,
            enabled.revision,
            "update-1",
            digest_character="3",
            candidate=self.v2,
        )
        claimed = self.repo.claim(request).operation
        assert claimed is not None
        quiesced = self.repo.advance(
            replace(
                claimed,
                phase=LifecyclePhase.QUIESCED,
                phase_revision=1,
                frozen_data=FrozenData("ref:freeze.old", enabled.selected.data_ref, 7),
            ),
            expected_phase_revision=0,
        )
        candidate = SelectedInstallation(
            self.v2,
            "ref:data.v2",
            (),
            enabled.selected.grant_generation + 1,
            enabled.selected.activation_generation + 1,
        )
        staged = self.repo.advance(
            replace(
                quiesced,
                phase=LifecyclePhase.DATA_STAGED,
                phase_revision=2,
                candidate=candidate,
                staged_data=StagedData("ref:data.v2", 4, "ref:migration.v2"),
            ),
            expected_phase_revision=1,
        )
        validated = self.repo.advance(
            replace(
                staged,
                phase=LifecyclePhase.ACTIVATION_VALIDATED,
                phase_revision=3,
                activation=ValidatedActivation("ref:activation.v2", candidate, 5),
            ),
            expected_phase_revision=2,
        )
        switched = self.repo.switch(OP_UPDATE, expected_phase_revision=validated.phase_revision)
        current = switched.intended_receipt.record
        assert current is not None
        self.assertEqual(current.status, ExtensionStatus.ENABLED)
        self.assertEqual(current.selected.executable, self.v2)

        with self.assertRaises(LifecycleResolutionRequiredError):
            self.repo.rollback(
                OP_UPDATE,
                expected_phase_revision=switched.phase_revision,
                revocation_ref="ref:revoked.v2",
                current_data=FrozenData("ref:freeze.v2", "ref:data.v2", 6),
            )
        resolution = self.repo.recover()[0]
        self.assertEqual(resolution.phase, LifecyclePhase.RESOLUTION_REQUIRED)
        with self.assertRaises(LifecycleResolutionRequiredError):
            self.repo.rollback(
                OP_UPDATE,
                expected_phase_revision=resolution.phase_revision,
                revocation_ref="ref:revoked.v2",
                current_data=FrozenData("ref:freeze.v2.again", "ref:data.v2", 5),
            )
        restoring = self.repo.rollback(
            OP_UPDATE,
            expected_phase_revision=resolution.phase_revision,
            revocation_ref="ref:revoked.v2",
            current_data=FrozenData("ref:freeze.v2", "ref:data.v2", 6),
            resolution_ref="ref:export.v2",
        )
        restored = restoring.intended_receipt.record
        assert restored is not None
        self.assertEqual(restored.selected.executable, self.v1)
        self.assertGreater(restored.revision, current.revision)
        self.assertGreater(restored.selected.grant_generation, current.selected.grant_generation)
        self.assertGreater(restored.selected.activation_generation, current.selected.activation_generation)
        self.assertEqual(self.repo.get(EXTENSION_ID), restored)
        self.assertEqual(SQLiteExtensionLifecycleRepository(self.db_path).recover(), (restoring,))
        receipt = self.repo.settle(OP_UPDATE, expected_phase_revision=restoring.phase_revision)
        self.assertEqual(receipt.outcome, ReceiptOutcome.ROLLED_BACK)
        self.assertEqual(receipt.record, restored)

    def test_grant_disable_and_remove_switches_preserve_explicit_state_rules(self) -> None:
        installed = self._install()
        grants_request = _request(
            OP_GRANTS,
            LifecycleAction.CHANGE_GRANTS,
            installed.revision,
            "grants-1",
            digest_character="8",
            approved_scopes=("data.read",),
        )
        claimed = self.repo.claim(grants_request).operation
        assert claimed is not None
        quiesced = self.repo.advance(
            replace(
                claimed,
                phase=LifecyclePhase.QUIESCED,
                phase_revision=1,
                frozen_data=FrozenData("ref:freeze.grants", installed.selected.data_ref, 0),
            ),
            expected_phase_revision=0,
        )
        switched = self.repo.switch(OP_GRANTS, expected_phase_revision=quiesced.phase_revision)
        granted_receipt = self.repo.settle(OP_GRANTS, expected_phase_revision=switched.phase_revision)
        granted = granted_receipt.record
        assert granted is not None
        self.assertEqual(granted.selected.approved_scopes, ("data.read",))
        self.assertEqual(
            granted.selected.activation_generation,
            installed.selected.activation_generation,
        )

        enabled = self._enable(granted)
        disable_request = _request(
            OP_DISABLE,
            LifecycleAction.DISABLE,
            enabled.revision,
            "disable-1",
            digest_character="9",
        )
        disable_claim = self.repo.claim(disable_request).operation
        assert disable_claim is not None
        disable_quiesced = self.repo.advance(
            replace(
                disable_claim,
                phase=LifecyclePhase.QUIESCED,
                phase_revision=1,
                frozen_data=FrozenData("ref:freeze.disable", enabled.selected.data_ref, 0),
            ),
            expected_phase_revision=0,
        )
        disabled_switch = self.repo.switch(
            OP_DISABLE,
            expected_phase_revision=disable_quiesced.phase_revision,
        )
        disabled_receipt = self.repo.settle(
            OP_DISABLE,
            expected_phase_revision=disabled_switch.phase_revision,
        )
        disabled = disabled_receipt.record
        assert disabled is not None
        self.assertEqual(disabled.status, ExtensionStatus.DISABLED)
        self.assertGreater(
            disabled.selected.activation_generation,
            enabled.selected.activation_generation,
        )

        remove_request = _request(
            OP_REMOVE,
            LifecycleAction.REMOVE,
            disabled.revision,
            "remove-1",
            digest_character="d",
        )
        remove_claim = self.repo.claim(remove_request).operation
        assert remove_claim is not None
        remove_quiesced = self.repo.advance(
            replace(
                remove_claim,
                phase=LifecyclePhase.QUIESCED,
                phase_revision=1,
                frozen_data=FrozenData("ref:freeze.remove", disabled.selected.data_ref, 0),
            ),
            expected_phase_revision=0,
        )
        removed_switch = self.repo.switch(
            OP_REMOVE,
            expected_phase_revision=remove_quiesced.phase_revision,
        )
        removed_receipt = self.repo.settle(
            OP_REMOVE,
            expected_phase_revision=removed_switch.phase_revision,
        )
        removed = removed_receipt.record
        assert removed is not None
        self.assertEqual(removed.status, ExtensionStatus.REMOVED)
        self.assertIsNone(self.repo.get(EXTENSION_ID))
        reinstall = _request(
            OP_REINSTALL,
            LifecycleAction.INSTALL,
            removed.revision,
            "reinstall-1",
            digest_character="e",
            candidate=self.v2,
        )
        self.assertEqual(self.repo.claim(reinstall).disposition, ClaimDisposition.ADMITTED)

    def test_first_install_rollback_retains_a_monotonic_hidden_tombstone(self) -> None:
        request = _request(
            OP_INSTALL,
            LifecycleAction.INSTALL,
            0,
            "install-rollback",
            digest_character="f",
            candidate=self.v1,
        )
        claimed = self.repo.claim(request).operation
        assert claimed is not None
        quiesced = self.repo.advance(
            replace(claimed, phase=LifecyclePhase.QUIESCED, phase_revision=1),
            expected_phase_revision=0,
        )
        candidate = SelectedInstallation(self.v1, "ref:data.rollback", (), 0, 0)
        staged = self.repo.advance(
            replace(
                quiesced,
                phase=LifecyclePhase.DATA_STAGED,
                phase_revision=2,
                candidate=candidate,
                staged_data=StagedData("ref:data.rollback", 0, "ref:migration.rollback"),
            ),
            expected_phase_revision=1,
        )
        switched = self.repo.switch(OP_INSTALL, expected_phase_revision=staged.phase_revision)
        restoring = self.repo.rollback(
            OP_INSTALL,
            expected_phase_revision=switched.phase_revision,
            revocation_ref="ref:revoked.rollback",
            current_data=FrozenData("ref:freeze.rollback", "ref:data.rollback", 0),
        )
        tombstone = restoring.intended_receipt.record
        assert tombstone is not None
        self.assertEqual(tombstone.status, ExtensionStatus.REMOVED)
        self.assertEqual(tombstone.revision, 2)
        self.assertGreater(tombstone.selected.grant_generation, candidate.grant_generation)
        self.assertGreater(
            tombstone.selected.activation_generation,
            candidate.activation_generation,
        )
        self.assertIsNone(self.repo.get(EXTENSION_ID))
        receipt = self.repo.settle(OP_INSTALL, expected_phase_revision=restoring.phase_revision)
        self.assertEqual(receipt.record, tombstone)
        self.assertEqual(receipt.outcome, ReceiptOutcome.ROLLED_BACK)

    def test_recovery_is_bounded_ordered_and_contains_no_authority_tokens(self) -> None:
        first = _request(
            OP_INSTALL,
            LifecycleAction.INSTALL,
            0,
            "first",
            digest_character="6",
            candidate=self.v1,
        )
        other_artifact = ExecutableArtifact("c" * 64, "org.example.other", "1.0.0", ())
        second = LifecycleRequest(
            OP_OTHER,
            "ref:operator",
            LifecycleAction.INSTALL,
            "org.example.other",
            0,
            "7" * 64,
            "second",
            other_artifact,
        )
        self.repo.claim(second)
        self.repo.claim(first)
        recovered = self.repo.recover(limit=1)
        self.assertEqual(recovered[0].request.operation_id, OP_INSTALL)
        after = self.repo.recover(after_operation_id=OP_INSTALL, limit=1)
        self.assertEqual(after[0].request.operation_id, OP_OTHER)

        import sqlite3

        connection = sqlite3.connect(self.db_path)
        try:
            names = {
                column[1]
                for table in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'extension_lifecycle_%'"
                )
                for column in connection.execute(f"PRAGMA table_info({table[0]})")
            }
        finally:
            connection.close()
        self.assertFalse(any("token" in name or "authority" in name for name in names))


if __name__ == "__main__":
    unittest.main()
