"""Real-process/SQLite proof for the B20 activation composition adapter."""
from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.adapters.storage.sqlite_versioned_plugin_data import SQLiteVersionedPluginDataStore
from model_deck.engine.extensions.ports import (
    ExecutableArtifact, ExtensionRecord, ExtensionStatus, SelectedInstallation,
)
from model_deck.engine.jobs.ports import CreateJobCommand, GetJobCommand, JobOwner, JobState
from model_deck.engine.plugin_authority import ActivationIdentity
from model_deck.engine.plugin_data import PluginDataRevisionConflictError
from model_deck.engine.plugin_data.versioning import PluginDataBinding
from model_deck.plugins.activation_lifecycle import (
    ActivationLifecycleConflictError, ProcessExtensionActivationLifecycle,
    ResolvedArtifactLaunch,
)
from model_deck.plugins.process_runtime import ProcessRuntimeConfig, ProcessRuntimeError


PLUGIN_ID = "org.example.lifecycle"
INSTALL = "30000000-0000-4000-8000-000000000001"
ENABLE = "30000000-0000-4000-8000-000000000002"
DISABLE = "30000000-0000-4000-8000-000000000003"
UPDATE = "30000000-0000-4000-8000-000000000004"


def _artifact(version: str, digest: str) -> ExecutableArtifact:
    return ExecutableArtifact(digest * 64, PLUGIN_ID, version, ("data.read",))


def _child(version: str, activation_id: str, *, drained: bool) -> str:
    return f"""
import importlib.util
import json
import sys
if not sys.flags.isolated or importlib.util.find_spec("model_deck") is not None:
    raise RuntimeError("fixture must not import engine internals")
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method", "")
    request_id = request.get("id")
    if method.endswith("hello"):
        result = {{"plugin_id": {PLUGIN_ID!r}, "plugin_version": {version!r}, "capabilities": []}}
    elif method.endswith("activate"):
        result = {{"activation_id": {activation_id!r}, "invocation_handle_prefix": "fixture:"}}
    elif method.endswith("drain"):
        result = {{"drained": {drained!r}}}
    elif method == "plugin.v1.invoke":
        result = {{"output": request["params"]["input"]}}
    else:
        raise RuntimeError("unexpected method")
    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": request_id, "result": result}}) + "\\n")
    sys.stdout.flush()
"""


class _Resolver:
    def __init__(self, directory: Path, *, drained: bool = False) -> None:
        self.directory = directory
        self.drained = drained

    def resolve(self, executable):
        activation_id = str(uuid.uuid4())
        return ResolvedArtifactLaunch(
            executable,
            ProcessRuntimeConfig(
                argv=(sys.executable, "-I", "-c", _child(
                    executable.version, activation_id, drained=self.drained,
                )),
                package_dir=str(self.directory),
                timeout_s=1,
            ),
        )


class _AuthorityController:
    def __init__(self) -> None:
        self.identities: dict[SelectedInstallation, ActivationIdentity] = {}
        self.states: dict[str, str] = {}

    def register_non_serving(self, identity, selected):
        self.identities[selected] = identity
        self.states[identity.activation_id] = "non_serving"

    def revoke(self, identity):
        self.states[identity.activation_id] = "revoked"

    def admit(self, identity, selected):
        if self.states.get(identity.activation_id) != "non_serving":
            raise AssertionError("only a non-serving activation may be admitted")
        self.identities[selected] = identity
        self.states[identity.activation_id] = "serving"

    def identity_for(self, selected):
        return self.identities.get(selected)


class _BrokerFactory:
    def __init__(self, authority: _AuthorityController) -> None:
        self.authority = authority
        self.bindings: list[tuple[ActivationIdentity, PluginDataBinding]] = []

    def create(self, identity, binding, allowed_methods):
        self.bindings.append((identity, binding))

        def handle(authenticated_activation_id, method, params):
            if authenticated_activation_id != identity.activation_id:
                raise PermissionError("foreign activation")
            if self.authority.states.get(identity.activation_id) != "serving":
                raise PermissionError("activation is not serving")
            raise AssertionError("fixture declares no broker methods")

        return handle


class ProcessExtensionActivationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="model-deck-activation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = SQLiteVersionedPluginDataStore(self.root / "data.sqlite3")
        self.jobs = SQLitePluginJobRepository(
            self.root / "jobs.sqlite3", checkpoint_validator=lambda schema_id, value: None,
        )
        self.v1 = _artifact("1.0.0", "a")
        self.v2 = _artifact("2.0.0", "b")
        staged = self.store.stage(INSTALL, self.v1, None)
        self.installed = SelectedInstallation(self.v1, staged.data_ref, (), 0, 0)
        self.store.activate_selected(
            INSTALL, self.installed,
            expected_data_revision=staged.initial_revision, enabled=False,
        )
        self.installed_record = ExtensionRecord(
            PLUGIN_ID, 1, ExtensionStatus.INSTALLED, self.installed,
        )
        self.authority = _AuthorityController()
        self.resolver = _Resolver(self.root)
        self.brokers = _BrokerFactory(self.authority)
        self.adapter = self._adapter()

    def _adapter(self):
        return ProcessExtensionActivationLifecycle(
            engine_instance_id="engine-test", audience="plugin-test",
            api_major=1, api_minor=0, data_store=self.store,
            job_repository=self.jobs, launch_resolver=self.resolver,
            authority_controller=self.authority, broker_factory=self.brokers,
            token_factory=lambda: "private-activation-token",
            nonce_factory=lambda: "fixture-nonce",
        )

    def _enable(self):
        self.adapter.quiesce(ENABLE, self.installed_record, deadline_ms=1000)
        self.store.freeze(ENABLE, self.installed)
        selected = replace(self.installed, activation_generation=1)
        validated = self.adapter.validate(ENABLE, selected)
        self.addCleanup(self.adapter.revoke, ENABLE, validated)
        record = ExtensionRecord(PLUGIN_ID, 2, ExtensionStatus.ENABLED, selected)
        return selected, validated, record

    def test_validate_is_non_serving_then_admit_publishes_exact_binding(self) -> None:
        selected, validated, record = self._enable()
        identity, binding = self.brokers.bindings[-1]
        self.assertEqual(self.authority.states[identity.activation_id], "non_serving")
        self.assertIsNone(self.adapter.serving(PLUGIN_ID))
        self.assertEqual(binding, PluginDataBinding(PLUGIN_ID, selected.data_ref, 1))

        self.adapter.admit(ENABLE, record, validated, expected_data_revision=0)
        serving = self.adapter.serving(PLUGIN_ID)
        assert serving is not None
        self.assertEqual(serving.identity, identity)
        self.assertEqual(serving.selected, selected)
        self.assertEqual(serving.invocation_channel.activation_id, identity.activation_id)
        self.assertNotIn("private-activation-token", repr(serving))
        repository = self.store.repository_for(binding)
        self.assertEqual(repository.put(PLUGIN_ID, "ready", {"ok": True}).revision, 1)
        self.adapter.admit(ENABLE, record, validated, expected_data_revision=0)

    def test_non_serving_admission_selects_exact_staged_data(self) -> None:
        self.store = SQLiteVersionedPluginDataStore(self.root / "install-data.sqlite3")
        self.jobs = SQLitePluginJobRepository(
            self.root / "install-jobs.sqlite3",
            checkpoint_validator=lambda schema_id, value: None,
        )
        adapter = self._adapter()
        staged = self.store.stage(INSTALL, self.v1, None)
        selected = SelectedInstallation(self.v1, staged.data_ref, (), 0, 0)
        record = ExtensionRecord(
            PLUGIN_ID, 1, ExtensionStatus.INSTALLED, selected,
        )

        adapter.admit(
            INSTALL,
            record,
            None,
            expected_data_revision=staged.initial_revision,
        )

        frozen = self.store.freeze(INSTALL, selected)
        self.assertEqual(frozen.data_ref, staged.data_ref)
        self.assertEqual(frozen.final_revision, staged.initial_revision)
        self.assertIsNone(adapter.serving(PLUGIN_ID))

    def test_revision_failure_publishes_nothing_and_retry_uses_same_validation(self) -> None:
        _, validated, record = self._enable()
        with self.assertRaises(ActivationLifecycleConflictError):
            self.adapter.admit(ENABLE, record, validated, expected_data_revision=1)
        self.assertIsNone(self.adapter.serving(PLUGIN_ID))
        identity = self.brokers.bindings[-1][0]
        self.assertEqual(self.authority.states[identity.activation_id], "non_serving")
        self.adapter.admit(ENABLE, record, validated, expected_data_revision=0)
        self.assertIsNotNone(self.adapter.serving(PLUGIN_ID))

    def test_revalidation_before_switch_reuses_the_prior_generation_freeze(self) -> None:
        selected, first_validation, record = self._enable()
        first_identity = self.brokers.bindings[-1][0]

        replacement = self.adapter.validate(ENABLE, selected)
        self.addCleanup(self.adapter.revoke, ENABLE, replacement)
        self.assertEqual(
            self.authority.states[first_identity.activation_id],
            "revoked",
        )
        with self.assertRaises(ActivationLifecycleConflictError):
            self.adapter.admit(
                ENABLE,
                record,
                first_validation,
                expected_data_revision=0,
            )
        self.adapter.admit(ENABLE, record, replacement, expected_data_revision=0)
        self.assertIsNotNone(self.adapter.serving(PLUGIN_ID))

    def test_revalidation_of_selected_generation_reopens_writes(self) -> None:
        selected, validated, record = self._enable()
        self.adapter.admit(ENABLE, record, validated, expected_data_revision=0)
        first = self.adapter.serving(PLUGIN_ID)
        assert first is not None
        binding = PluginDataBinding(PLUGIN_ID, selected.data_ref, 1)
        repository = self.store.repository_for(binding)
        repository.put(PLUGIN_ID, "before-recovery", True)

        recovered = self.adapter.validate(ENABLE, selected)
        self.addCleanup(self.adapter.revoke, ENABLE, recovered)
        self.assertIsNone(self.adapter.serving(PLUGIN_ID))
        self.assertEqual(
            self.authority.states[first.identity.activation_id],
            "revoked",
        )
        self.adapter.admit(
            ENABLE,
            record,
            recovered,
            expected_data_revision=recovered.validated_data_revision,
        )

        self.assertEqual(
            repository.put(PLUGIN_ID, "after-recovery", True).revision,
            1,
        )

    def test_quiesce_revokes_closes_and_interrupts_only_owned_jobs(self) -> None:
        selected, validated, record = self._enable()
        self.adapter.admit(ENABLE, record, validated, expected_data_revision=0)
        serving = self.adapter.serving(PLUGIN_ID)
        assert serving is not None
        owner = JobOwner(PLUGIN_ID, serving.identity.activation_id)
        job = self.jobs.create(CreateJobCommand(owner, "invocation-a", "org.example.export"))
        other_owner = JobOwner(PLUGIN_ID, str(uuid.uuid4()))
        other = self.jobs.create(CreateJobCommand(
            other_owner, "invocation-b", "org.example.other",
        ))

        self.adapter.quiesce(DISABLE, record, deadline_ms=1000)
        self.assertIsNone(self.adapter.serving(PLUGIN_ID))
        self.assertEqual(self.authority.states[owner.activation_id], "revoked")
        self.assertIs(self.jobs.get(GetJobCommand(job.job_id)).state, JobState.INTERRUPTED)
        self.assertIs(self.jobs.get(GetJobCommand(other.job_id)).state, JobState.QUEUED)
        with self.assertRaises(ProcessRuntimeError):
            serving.invocation_channel.invoke(
                "org.example.echo", {"after": "quiesce"},
                {"activation_id": owner.activation_id, "plugin_id": PLUGIN_ID,
                 "invocation_handle": "revoked", "revocation_generation": 0},
            )

        revision = self.store.freeze(DISABLE, selected).final_revision
        disabled = replace(selected, activation_generation=2)
        disabled_record = ExtensionRecord(
            PLUGIN_ID, 3, ExtensionStatus.DISABLED, disabled,
        )
        self.adapter.admit(DISABLE, disabled_record, None, expected_data_revision=revision)
        with self.assertRaises(PluginDataRevisionConflictError):
            self.store.repository_for(PluginDataBinding(
                PLUGIN_ID, disabled.data_ref, 2,
            )).put(PLUGIN_ID, "blocked", True)

    def test_disabled_generation_can_be_enabled_without_an_intervening_update(self) -> None:
        selected, validated, enabled_record = self._enable()
        self.adapter.admit(ENABLE, enabled_record, validated, expected_data_revision=0)
        repository = self.store.repository_for(
            PluginDataBinding(PLUGIN_ID, selected.data_ref, 1)
        )
        repository.put(PLUGIN_ID, "retained", {"value": 1})

        self.adapter.quiesce(DISABLE, enabled_record, deadline_ms=1000)
        disabled_revision = self.store.freeze(DISABLE, selected).final_revision
        disabled = replace(selected, activation_generation=2)
        disabled_record = ExtensionRecord(
            PLUGIN_ID, 3, ExtensionStatus.DISABLED, disabled,
        )
        self.adapter.admit(
            DISABLE,
            disabled_record,
            None,
            expected_data_revision=disabled_revision,
        )

        reenable_operation = str(uuid.uuid4())
        self.adapter.quiesce(
            reenable_operation,
            disabled_record,
            deadline_ms=1000,
        )
        self.store.freeze(reenable_operation, disabled)
        reenabled = replace(disabled, activation_generation=3)
        revalidation = self.adapter.validate(reenable_operation, reenabled)
        self.addCleanup(self.adapter.revoke, reenable_operation, revalidation)
        reenabled_record = ExtensionRecord(
            PLUGIN_ID, 4, ExtensionStatus.ENABLED, reenabled,
        )
        self.adapter.admit(
            reenable_operation,
            reenabled_record,
            revalidation,
            expected_data_revision=revalidation.validated_data_revision,
        )

        reopened = self.store.repository_for(
            PluginDataBinding(PLUGIN_ID, reenabled.data_ref, 3)
        )
        self.assertEqual(
            reopened.get(PLUGIN_ID, "retained").value,
            {"value": 1},
        )
        self.assertEqual(reopened.put(PLUGIN_ID, "after-enable", True).revision, 1)

    def test_stale_activation_reference_cannot_be_admitted_after_restart(self) -> None:
        _, validated, record = self._enable()
        restarted = self._adapter()
        with self.assertRaises(ActivationLifecycleConflictError):
            restarted.admit(ENABLE, record, validated, expected_data_revision=0)
        self.assertIsNone(restarted.serving(PLUGIN_ID))
        self.adapter.revoke(ENABLE, validated)

    def test_restart_quiesce_revokes_old_engine_identity_and_interrupts_jobs(self) -> None:
        identity = ActivationIdentity(
            "prior-engine", "plugin-test", str(uuid.uuid4()),
            PLUGIN_ID, self.v1.version,
        )
        self.authority.identities[self.installed] = identity
        self.authority.states[identity.activation_id] = "serving"
        job = self.jobs.create(CreateJobCommand(
            JobOwner(PLUGIN_ID, identity.activation_id),
            "prior-invocation", "org.example.prior",
        ))
        enabled = replace(self.installed_record, status=ExtensionStatus.ENABLED)

        self.adapter.quiesce(DISABLE, enabled, deadline_ms=1000)

        self.assertEqual(self.authority.states[identity.activation_id], "revoked")
        self.assertIs(self.jobs.get(GetJobCommand(job.job_id)).state, JobState.INTERRUPTED)
        self.assertIsNone(self.adapter.serving(PLUGIN_ID))

    def test_revoked_validation_cannot_later_publish_or_reuse_its_process(self) -> None:
        _, validated, record = self._enable()
        identity = self.brokers.bindings[-1][0]
        proof = self.adapter.revoke(ENABLE, validated)

        self.assertEqual(proof, self.adapter.revoke(ENABLE, validated))
        self.assertEqual(self.authority.states[identity.activation_id], "revoked")
        with self.assertRaises(ActivationLifecycleConflictError):
            self.adapter.admit(ENABLE, record, validated, expected_data_revision=0)
        self.assertIsNone(self.adapter.serving(PLUGIN_ID))

    def test_rollback_restores_prior_data_with_a_fresh_process_identity(self) -> None:
        selected_v1, validated_v1, enabled_v1 = self._enable()
        self.adapter.admit(ENABLE, enabled_v1, validated_v1, expected_data_revision=0)
        first = self.adapter.serving(PLUGIN_ID)
        assert first is not None

        self.adapter.quiesce(UPDATE, enabled_v1, deadline_ms=1000)
        frozen_v1 = self.store.freeze(UPDATE, selected_v1)
        staged_v2 = self.store.stage(UPDATE, self.v2, frozen_v1)
        selected_v2 = SelectedInstallation(self.v2, staged_v2.data_ref, (), 1, 2)
        validated_v2 = self.adapter.validate(UPDATE, selected_v2)
        self.addCleanup(self.adapter.revoke, UPDATE, validated_v2)
        enabled_v2 = ExtensionRecord(PLUGIN_ID, 3, ExtensionStatus.ENABLED, selected_v2)
        self.adapter.admit(UPDATE, enabled_v2, validated_v2, expected_data_revision=0)

        self.store.freeze(UPDATE, selected_v2)
        self.adapter.revoke(UPDATE, validated_v2)
        self.store.thaw(UPDATE, frozen_v1)
        restored = replace(selected_v1, grant_generation=2, activation_generation=3)
        restarted = self._adapter()
        restored_validation = restarted.validate(UPDATE, restored)
        self.addCleanup(restarted.revoke, UPDATE, restored_validation)
        restored_record = ExtensionRecord(PLUGIN_ID, 4, ExtensionStatus.ENABLED, restored)
        restarted.admit(
            UPDATE, restored_record, restored_validation,
            expected_data_revision=restored_validation.validated_data_revision,
        )

        serving = restarted.serving(PLUGIN_ID)
        assert serving is not None
        self.assertEqual(serving.selected, restored)
        self.assertNotEqual(serving.identity.activation_id, first.identity.activation_id)
        self.assertEqual(self.authority.states[first.identity.activation_id], "revoked")

        recovered_restoration = restarted.validate(UPDATE, restored)
        self.addCleanup(restarted.revoke, UPDATE, recovered_restoration)
        restarted.admit(
            UPDATE,
            restored_record,
            recovered_restoration,
            expected_data_revision=recovered_restoration.validated_data_revision,
        )
        restored_repository = self.store.repository_for(
            PluginDataBinding(PLUGIN_ID, restored.data_ref, 3)
        )
        self.assertEqual(
            restored_repository.put(PLUGIN_ID, "restored-write", True).revision,
            1,
        )


if __name__ == "__main__":
    unittest.main()
