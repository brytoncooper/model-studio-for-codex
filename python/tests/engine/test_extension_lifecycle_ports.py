import inspect
import unittest
from typing import get_type_hints
from dataclasses import FrozenInstanceError, replace

from model_deck.engine.extensions.ports import (
    ClaimDisposition, ExecutableArtifact, ExtensionRecord, ExtensionStatus,
    FrozenData, LifecycleAction, LifecycleClaim, LifecycleContractError,
    LifecycleOperation, LifecyclePhase, LifecycleReceipt, LifecycleRequest,
    ReceiptOutcome, SelectedInstallation, StagedData, ValidatedActivation,
    ExtensionLifecycleRepository, allowed_next_phases,
)


class ExtensionLifecyclePortTests(unittest.TestCase):
    def setUp(self):
        self.artifact = ExecutableArtifact('a' * 64, 'org.example.fixture', '1.0.0', ('read', 'write'))
        self.selection = SelectedInstallation(self.artifact, 'ref:data.v1', (), 0, 0)
        self.request = LifecycleRequest(
            '550e8400-e29b-41d4-a716-446655440000', 'ref:operator', LifecycleAction.INSTALL,
            self.artifact.extension_id, 0, 'b' * 64, 'install-1', self.artifact)
        self.record = ExtensionRecord(self.artifact.extension_id, 1, ExtensionStatus.INSTALLED, self.selection)

    def test_deeply_immutable_request_selection_and_receipt(self):
        receipt = LifecycleReceipt(self.request, self.record, ReceiptOutcome.APPLIED)
        for instance, name, value in ((receipt, 'outcome', ReceiptOutcome.ABORTED),
                                      (receipt.record.selected, 'approved_scopes', ('read',)),
                                      (receipt.request.candidate, 'version', '2.0.0')):
            with self.subTest(name=name), self.assertRaises(FrozenInstanceError):
                setattr(instance, name, value)
        with self.assertRaises(LifecycleContractError):
            replace(self.artifact, requested_scopes=['read'])
        with self.assertRaises(LifecycleContractError):
            replace(self.selection, approved_scopes=['read'])

    def test_required_identity_revision_key_and_hash_bounds(self):
        invalid = {'operation_id': ['x'], 'principal_ref': ['secret input'],
                   'extension_id': ['bad'], 'expected_revision': [True, -1, 1.0],
                   'request_digest': ['x', 'A' * 64], 'idempotency_key': [None, '', 'x' * 129]}
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(LifecycleContractError) as caught:
                    replace(self.request, **{field: value})
                self.assertEqual(str(caught.exception), 'invalid extension lifecycle contract')

    def test_artifact_version_and_scope_bounds(self):
        for fields in ({'version': '1'}, {'version': '1.0.0-' + 'a' * 65},
                       {'requested_scopes': ('read', 'read')}, {'requested_scopes': ('x' * 65,)},
                       {'requested_scopes': tuple(str(i) for i in range(65))}, {'requested_scopes': ('',)}):
            with self.subTest(fields=fields), self.assertRaises(LifecycleContractError):
                replace(self.artifact, **fields)
        for fields in ({'approved_scopes': ('unknown',)}, {'grant_generation': True},
                       {'activation_generation': -1}, {'data_ref': '/tmp/data'}):
            with self.subTest(fields=fields), self.assertRaises(LifecycleContractError):
                replace(self.selection, **fields)

    def test_candidate_action_and_identity_binding(self):
        for fields in ({'candidate': None}, {'approved_scopes': ('read',)},
                       {'candidate': replace(self.artifact, extension_id='org.other.fixture')},
                       {'action': LifecycleAction.REMOVE}):
            with self.subTest(fields=fields), self.assertRaises(LifecycleContractError):
                replace(self.request, **fields)
        remove = replace(self.request, action=LifecycleAction.REMOVE, candidate=None)
        self.assertEqual(remove.idempotency_key, 'install-1')
        with self.assertRaises(LifecycleContractError):
            replace(self.record, extension_id='org.other.fixture')

    def test_operation_evidence_binding(self):
        operation = LifecycleOperation(self.request, LifecyclePhase.CLAIMED, 0, None)
        candidate = replace(operation, candidate=self.selection,
                            activation=ValidatedActivation('ref:activation', self.selection, 0))
        self.assertEqual(candidate.activation.validated_data_revision, 0)
        for fields in ({'candidate': replace(self.selection, executable=replace(self.artifact, extension_id='org.other.fixture'))},
                       {'phase_revision': True}, {'phase': 'claimed'},
                       {'activation': ValidatedActivation('ref:activation', self.selection, 0)}):
            with self.subTest(fields=fields), self.assertRaises(LifecycleContractError):
                replace(operation, **fields)

    def test_claim_alternatives_and_original_receipt(self):
        operation = LifecycleOperation(self.request, LifecyclePhase.CLAIMED, 0, None)
        receipt = LifecycleReceipt(self.request, self.record, ReceiptOutcome.APPLIED)
        self.assertIs(LifecycleClaim(ClaimDisposition.REPLAY, receipt=receipt).receipt, receipt)
        for disposition in (ClaimDisposition.ADMITTED, ClaimDisposition.IN_PROGRESS):
            self.assertIs(LifecycleClaim(disposition, operation=operation).operation, operation)
        for args in ((ClaimDisposition.REPLAY,), (ClaimDisposition.REPLAY, operation, receipt),
                     (ClaimDisposition.ADMITTED, None, receipt), ('admitted', operation)):
            with self.subTest(args=args), self.assertRaises(LifecycleContractError):
                LifecycleClaim(*args)
        self.assertIsNone(LifecycleReceipt(self.request, None, ReceiptOutcome.ABORTED).record)
        with self.assertRaises(LifecycleContractError):
            LifecycleReceipt(self.request, None, ReceiptOutcome.APPLIED)

    def test_action_specific_paths_and_no_abort_after_switch(self):
        for action in LifecycleAction:
            self.assertEqual(allowed_next_phases(action, LifecyclePhase.CLAIMED, previous_status=ExtensionStatus.DISABLED),
                             (LifecyclePhase.QUIESCED, LifecyclePhase.RESTORING))
            following = allowed_next_phases(action, LifecyclePhase.QUIESCED, previous_status=ExtensionStatus.DISABLED)
            expected = (LifecyclePhase.DATA_STAGED if action in (LifecycleAction.INSTALL, LifecycleAction.UPDATE)
                        else LifecyclePhase.ACTIVATION_VALIDATED if action is LifecycleAction.ENABLE
                        else LifecyclePhase.SWITCHED)
            self.assertEqual(following, (expected, LifecyclePhase.RESTORING))
            self.assertNotIn(LifecyclePhase.ABORTED, allowed_next_phases(action, LifecyclePhase.SWITCHED, previous_status=ExtensionStatus.DISABLED))
            for terminal in (LifecyclePhase.SETTLED, LifecyclePhase.ABORTED, LifecyclePhase.ROLLED_BACK):
                self.assertEqual(allowed_next_phases(action, terminal, previous_status=ExtensionStatus.DISABLED), ())

    def test_install_and_disabled_updates_never_activate(self):
        for action in (LifecycleAction.INSTALL, LifecycleAction.UPDATE):
            for status in (ExtensionStatus.INSTALLED, ExtensionStatus.DISABLED, ExtensionStatus.REMOVED):
                with self.subTest(action=action, status=status):
                    self.assertEqual(allowed_next_phases(action, LifecyclePhase.DATA_STAGED, previous_status=status),
                                     (LifecyclePhase.SWITCHED, LifecyclePhase.RESTORING))
                    self.assertEqual(allowed_next_phases(action, LifecyclePhase.ACTIVATION_VALIDATED, previous_status=status), ())
        self.assertEqual(allowed_next_phases(LifecycleAction.UPDATE, LifecyclePhase.DATA_STAGED,
                                            previous_status=ExtensionStatus.ENABLED),
                         (LifecyclePhase.ACTIVATION_VALIDATED, LifecyclePhase.RESTORING))
        self.assertEqual(allowed_next_phases(LifecycleAction.INSTALL, LifecyclePhase.DATA_STAGED,
                                            previous_status=ExtensionStatus.ENABLED),
                         (LifecyclePhase.SWITCHED, LifecyclePhase.RESTORING))
        self.assertEqual(allowed_next_phases(LifecycleAction.CHANGE_GRANTS, LifecyclePhase.QUIESCED,
                                            previous_status=ExtensionStatus.DISABLED),
                         (LifecyclePhase.SWITCHED, LifecyclePhase.RESTORING))

    def test_update_and_grants_fail_closed_without_prior_status(self):
        for action in (LifecycleAction.UPDATE, LifecycleAction.CHANGE_GRANTS):
            for phase in (LifecyclePhase.QUIESCED, LifecyclePhase.DATA_STAGED):
                with self.subTest(action=action, phase=phase), self.assertRaises(LifecycleContractError):
                    allowed_next_phases(action, phase)

    def test_restoration_keeps_durable_intended_outcome_until_settle(self):
        # Public return types prohibit treating abort/rollback as final receipts.
        for method in (ExtensionLifecycleRepository.abort, ExtensionLifecycleRepository.rollback):
            self.assertIs(get_type_hints(method)['return'], LifecycleOperation)
        self.assertIs(get_type_hints(ExtensionLifecycleRepository.settle)['return'], LifecycleReceipt)
        for outcome, record in ((ReceiptOutcome.ABORTED, None),
                                (ReceiptOutcome.ROLLED_BACK, replace(self.record, status=ExtensionStatus.REMOVED))):
            intended = LifecycleReceipt(self.request, record, outcome)
            restoring = LifecycleOperation(self.request, LifecyclePhase.RESTORING, 3, None,
                                           intended_receipt=intended)
            # Serialized data reconstruction can retain exact intent while the claim
            # remains IN_PROGRESS; no SQLite/crash-recovery behavior is claimed here.
            reconstructed = replace(restoring)
            self.assertEqual(reconstructed.intended_receipt, intended)
            pending = LifecycleClaim(ClaimDisposition.IN_PROGRESS, operation=reconstructed)
            self.assertIsNone(pending.receipt)
            with self.assertRaises(LifecycleContractError):
                replace(restoring, intended_receipt=None)
            with self.assertRaises(LifecycleContractError):
                replace(restoring, intended_receipt=replace(intended, record=self.record, outcome=ReceiptOutcome.APPLIED))
        self.assertEqual(allowed_next_phases(LifecycleAction.INSTALL, LifecyclePhase.RESTORING),
                         (LifecyclePhase.ABORTED, LifecyclePhase.ROLLED_BACK))
        self.assertNotIn(LifecyclePhase.ABORTED,
                         allowed_next_phases(LifecycleAction.INSTALL, LifecyclePhase.CLAIMED))
        self.assertNotIn(LifecyclePhase.ROLLED_BACK,
                         allowed_next_phases(LifecycleAction.INSTALL, LifecyclePhase.SWITCHED))

    def test_abort_can_atomically_bind_a_completed_unpersisted_freeze(self):
        parameter = inspect.signature(ExtensionLifecycleRepository.abort).parameters[
            'frozen_data'
        ]
        self.assertIsNone(parameter.default)
        self.assertIn('FrozenData', str(parameter.annotation))

    def test_external_evidence_is_bounded_and_has_no_token_field(self):
        self.assertEqual(FrozenData('ref:freeze', 'ref:data', 2).final_revision, 2)
        self.assertEqual(StagedData('ref:data.v2', 0, 'ref:migration').initial_revision, 0)
        for factory, args in ((FrozenData, ('ref:freeze', 'ref:data', False)),
                              (StagedData, ('/tmp/path', 0, 'ref:migration')),
                              (ValidatedActivation, ('token secret', self.selection, 0))):
            with self.subTest(factory=factory), self.assertRaises(LifecycleContractError):
                factory(*args)
        for model in (LifecycleRequest, LifecycleOperation, SelectedInstallation, ValidatedActivation):
            self.assertFalse(any('token' in name for name in model.__dataclass_fields__))


if __name__ == '__main__':
    unittest.main()
