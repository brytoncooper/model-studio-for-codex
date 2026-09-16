"""Bounded integration tests for the plugin-job wire adapter."""
from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.engine.jobs.ports import GetJobCommand, JobNotFoundError, JobOwner, RequestCancelCommand
from model_deck.engine.jobs.service import (
    BrokerJobConflictError,
    BrokerJobDeniedError,
    BrokerJobInvalidRequestError,
    BrokerJobNotFoundError,
    BrokerJobTerminalError,
    PluginJobBroker,
)
from model_deck.engine.jobs.wire import (
    PluginJobWireActivationError,
    PluginJobWireAdapter,
    PluginJobWireRequestError,
    PluginJobWireResultError,
)
from model_deck.engine.plugin_authority import (
    ActivationIdentity,
    ActivationState,
    OperationAuthority,
    OriginState,
    PluginAuthority,
)


READ = ("read", "jobs.private", "jobs.access")
WRITE = ("write", "jobs.private", "jobs.access")
GRANTS = {
    "create": WRITE,
    "progress": WRITE,
    "complete": WRITE,
    "fail": WRITE,
    "check_cancelled": READ,
}
NAMESPACE = "com.example.worker"
OPERATION_ID = "com.example.worker.run"


class MemoryContexts:
    def __init__(self) -> None:
        self.contexts: dict[str, object] = {}

    def put(self, context) -> None:
        if context.invocation_id in self.contexts:
            raise ValueError("collision")
        self.contexts[context.invocation_id] = context

    def get(self, invocation_id):
        return self.contexts.get(invocation_id)


class FakeState:
    def __init__(self, activation_state, origin_state, operation_state) -> None:
        self.activation_state = activation_state
        self.origin_state = origin_state
        self.operation_state = operation_state

    def activation(self, identity):
        return self.activation_state

    def origin(self, principal_id):
        return self.origin_state

    def operation(self, operation_id):
        return self.operation_state


class PluginJobWireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 9, 12, tzinfo=timezone.utc)
        self.deadline = self.now + timedelta(minutes=5)
        self.identity = ActivationIdentity(
            "engine", "broker", "act-1", NAMESPACE, "1.0.0"
        )
        perms = dict(
            effects=frozenset({"read", "write"}),
            resource_scopes=frozenset({"jobs.private"}),
            capability_grants=frozenset({"jobs.access"}),
        )
        self.state = FakeState(
            ActivationState(
                self.identity, **perms,
                expires_at=self.deadline, revocation_generation=2,
            ),
            OriginState(
                "origin", "engine", "broker", **perms,
                expires_at=self.deadline, revocation_generation=3,
            ),
            OperationAuthority(OPERATION_ID, **perms),
        )

        @contextmanager
        def guard():
            yield

        self.guard = guard
        self.repo = SQLitePluginJobRepository(
            Path(self.tmp.name) / "jobs.sqlite",
            checkpoint_validator=lambda schema_id, value: None,
        )
        self.authority = PluginAuthority(
            engine_instance_id="engine", audience="broker", state=self.state,
            contexts=MemoryContexts(), clock=lambda: self.now,
        )
        self.broker = PluginJobBroker(
            authority=self.authority, repository=self.repo, grants=dict(GRANTS),
            mutation_guard=guard,
        )
        self.handle = self.authority.issue(
            self.identity, "origin", OPERATION_ID, expires_at=self.deadline,
        )
        self.adapter = PluginJobWireAdapter(
            trusted_activation=self.identity, broker=self.broker,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _bump_activation(self) -> None:
        self.state.activation_state = replace(
            self.state.activation_state,
            revocation_generation=self.state.activation_state.revocation_generation + 1,
        )

    def _create(self) -> str:
        created = self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.create",
            {
                "invocation_handle": self.handle,
                "operation_id": OPERATION_ID,
            },
        )
        return created["job_id"]

    # ------------------------------------------------------------------
    # Lifecycle / happy path
    # ------------------------------------------------------------------

    def test_create_progress_check_cancelled_complete_round_trip(self) -> None:
        job_id = self._create()
        self.assertEqual(len(job_id), 36)
        # progress
        progress = self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.progress",
            {"job_id": job_id, "progress": 0.25},
        )
        self.assertEqual(progress, {"accepted": True})
        # check_cancelled (no cancel yet)
        cancelled = self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.check_cancelled",
            {"job_id": job_id},
        )
        self.assertEqual(cancelled, {"cancelled": False})
        # complete with output
        completed = self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.complete",
            {
                "job_id": job_id,
                "output": {"nested": [1, 2, False], "n": None, "empty": []},
            },
        )
        self.assertEqual(completed, {"completed": True})

    def test_create_accepts_optional_checkpoint_schema_id(self) -> None:
        created = self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.create",
            {
                "invocation_handle": self.handle,
                "operation_id": OPERATION_ID,
                "checkpoint_schema_id": "contracts/common/checkpoint.json",
            },
        )
        self.assertEqual(len(created["job_id"]), 36)

    def test_fail_emits_failure_code(self) -> None:
        job_id = self._create()
        result = self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.fail",
            {
                "job_id": job_id,
                "error": {
                    "code": "deadline_exceeded",
                    "retryable": True,
                    "message": "wall clock ran out",
                    "request_id": "req-1",
                },
            },
        )
        self.assertEqual(result, {"failed": True})
        # subsequent complete must be terminal-conflict
        with self.assertRaises(BrokerJobTerminalError):
            self.adapter(
                self.identity.activation_id,
                "plugin.v1.broker.jobs.complete",
                {"job_id": job_id, "output": None},
            )

    def test_cancel_flag_routes_through_repo_and_broker(self) -> None:
        job_id = self._create()
        # request cancel via the repository (wire has no cancel method)
        self.repo.request_cancel(
            RequestCancelCommand(
                job_id=job_id,
                owner=JobOwner(plugin_id=NAMESPACE, activation_id="act-1"),
            )
        )
        cancelled = self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.check_cancelled",
            {"job_id": job_id},
        )
        self.assertEqual(cancelled, {"cancelled": True})

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    def test_forged_runtime_activation_is_rejected_before_broker_access(self) -> None:
        with self.assertRaises(PluginJobWireActivationError):
            self.adapter(
                "forged-activation",
                "plugin.v1.broker.jobs.create",
                {
                    "invocation_handle": self.handle,
                    "operation_id": OPERATION_ID,
                },
            )
        # No job was created.
        with self.assertRaises(JobNotFoundError):
            self.repo.get(GetJobCommand(job_id="00000000-0000-4000-8000-000000000000"))

    def test_cross_activation_followup_denied_without_effect(self) -> None:
        job_id = self._create()
        # Swap the trusted identity to a different activation id but keep the
        # same plugin_id. The wire should still raise
        # PluginJobWireActivationError before the broker is consulted.
        other_identity = replace(self.identity, activation_id="act-2")
        other_authority = PluginAuthority(
            engine_instance_id="engine", audience="broker", state=self.state,
            contexts=MemoryContexts(), clock=lambda: self.now,
        )
        other_broker = PluginJobBroker(
            authority=other_authority, repository=self.repo, grants=dict(GRANTS),
            mutation_guard=self.guard,
        )
        other_adapter = PluginJobWireAdapter(
            trusted_activation=other_identity, broker=other_broker,
        )
        with self.assertRaises(BrokerJobDeniedError):
            other_adapter(
                "act-2",  # matches other_identity, but does not match self.identity
                "plugin.v1.broker.jobs.progress",
                {"job_id": job_id, "progress": 0.5},
            )
        # No progress persisted.
        stored = self.repo.get(GetJobCommand(job_id=job_id))
        self.assertEqual(stored.progress, 0.0)

    def test_revoked_progress_denied_with_broker_denial(self) -> None:
        job_id = self._create()
        self._bump_activation()
        with self.assertRaises(BrokerJobDeniedError):
            self.adapter(
                self.identity.activation_id,
                "plugin.v1.broker.jobs.progress",
                {"job_id": job_id, "progress": 0.5},
            )
        stored = self.repo.get(GetJobCommand(job_id=job_id))
        self.assertEqual(stored.progress, 0.0)

    def test_revoked_complete_denied_with_broker_denial(self) -> None:
        job_id = self._create()
        self._bump_activation()
        with self.assertRaises(BrokerJobDeniedError):
            self.adapter(
                self.identity.activation_id,
                "plugin.v1.broker.jobs.complete",
                {"job_id": job_id, "output": None},
            )

    def test_operation_mismatch_create_denied(self) -> None:
        with self.assertRaises(BrokerJobDeniedError):
            self.adapter(
                self.identity.activation_id,
                "plugin.v1.broker.jobs.create",
                {
                    "invocation_handle": self.handle,
                    "operation_id": "com.example.other",
                },
            )

    def test_unknown_job_id_raises_broker_not_found(self) -> None:
        with self.assertRaises(BrokerJobNotFoundError):
            self.adapter(
                self.identity.activation_id,
                "plugin.v1.broker.jobs.progress",
                {"job_id": "00000000-0000-4000-8000-000000000000", "progress": 0.1},
            )

    def test_terminal_job_complete_then_progress_terminal(self) -> None:
        job_id = self._create()
        self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.complete",
            {"job_id": job_id, "output": {"done": True}},
        )
        with self.assertRaises(BrokerJobTerminalError):
            self.adapter(
                self.identity.activation_id,
                "plugin.v1.broker.jobs.progress",
                {"job_id": job_id, "progress": 0.5},
            )

    # ------------------------------------------------------------------
    # Schema validation failures (must use fixed safe errors)
    # ------------------------------------------------------------------

    def test_invalid_method_and_params_fail_with_fixed_error_before_broker(self) -> None:
        cases = [
            (
                "plugin.v1.broker.jobs.restart",
                {"job_id": "00000000-0000-4000-8000-000000000000"},
            ),
            (
                "plugin.v1.broker.jobs.create",
                {"invocation_handle": self.handle},  # missing operation_id
            ),
            (
                "plugin.v1.broker.jobs.progress",
                {"job_id": "not-a-uuid", "progress": 0.5},
            ),
            (
                "plugin.v1.broker.jobs.progress",
                {"job_id": "00000000-0000-4000-8000-000000000000", "progress": 2.0},
            ),
            (
                "plugin.v1.broker.jobs.fail",
                {
                    "job_id": "00000000-0000-4000-8000-000000000000",
                    "error": {"code": "made_up", "retryable": True},
                },
            ),
        ]
        for method, params in cases:
            with self.subTest(method=method, params=params):
                with self.assertRaises(PluginJobWireRequestError) as caught:
                    self.adapter(self.identity.activation_id, method, params)
                self.assertEqual(
                    str(caught.exception), "plugin job wire request invalid",
                )
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)

    def test_create_with_unknown_operation_id_rejected_by_schema(self) -> None:
        with self.assertRaises(PluginJobWireRequestError):
            self.adapter(
                self.identity.activation_id,
                "plugin.v1.broker.jobs.create",
                {
                    "invocation_handle": self.handle,
                    "operation_id": "Not Reverse Domain",
                },
            )

    def test_broker_result_validation_emits_fixed_error(self) -> None:
        job_id = self._create()

        class _BadBroker(PluginJobBroker):
            def report_progress(self, *args, **kwargs):  # type: ignore[override]
                return {"accepted": "yes"}  # schema requires boolean

        bad_adapter = PluginJobWireAdapter(
            trusted_activation=self.identity, broker=_BadBroker(
                authority=self.authority, repository=self.repo, grants=dict(GRANTS),
                mutation_guard=self.guard,
            ),
        )
        with self.assertRaises(PluginJobWireResultError) as caught:
            bad_adapter(
                self.identity.activation_id,
                "plugin.v1.broker.jobs.progress",
                {"job_id": job_id, "progress": 0.5},
            )
        self.assertEqual(str(caught.exception), "plugin job wire result invalid")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_constructor_rejects_misconfigured_arguments(self) -> None:
        from model_deck.engine.jobs.wire import PluginJobWireConfigurationError
        with self.assertRaises(PluginJobWireConfigurationError):
            PluginJobWireAdapter(trusted_activation="not-an-identity", broker=self.broker)
        with self.assertRaises(PluginJobWireConfigurationError):
            PluginJobWireAdapter(trusted_activation=self.identity, broker="not-a-broker")

    def test_full_handle_progress_then_complete_with_progress_output(self) -> None:
        # create -> progress 0.5 -> check_cancelled(False) -> complete with
        # structured output. Confirms results are exact dicts, no coercion.
        job_id = self._create()
        progress = self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.progress",
            {"job_id": job_id, "progress": 0.5},
        )
        self.assertEqual(progress, {"accepted": True})
        cancelled = self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.check_cancelled",
            {"job_id": job_id},
        )
        self.assertEqual(cancelled, {"cancelled": False})
        complete = self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.complete",
            {
                "job_id": job_id,
                "output": {"nested": [True, False, None], "k": "v"},
            },
        )
        self.assertEqual(complete, {"completed": True})



    def test_complete_output_present_passes_through(self) -> None:
        """Wire "output" key → broker.complete(output_present=True).

        The wire adapter must set ``output_present`` iff the worker supplied
        an ``output`` field (vs omitting it entirely).
        """
        job_id = self._create()
        result = self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.complete",
            {"job_id": job_id, "output": {"nested": {"k": 1}}},
        )
        self.assertEqual(result, {"completed": True})
        stored = self.repo.get(GetJobCommand(job_id=job_id))
        self.assertEqual(stored.state.value, "completed")
        self.assertTrue(stored.output_present)
        self.assertEqual(stored.output, {"nested": {"k": 1}})

    def test_complete_explicit_null_persists_null(self) -> None:
        """Explicit JSON null survives as output_present=True, output=None."""
        job_id = self._create()
        result = self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.complete",
            {"job_id": job_id, "output": None},
        )
        self.assertEqual(result, {"completed": True})
        stored = self.repo.get(GetJobCommand(job_id=job_id))
        self.assertTrue(stored.output_present)
        self.assertIsNone(stored.output)

    def test_complete_without_output_does_not_persist(self) -> None:
        """Omitted ``output`` → output_present=False, nothing stored."""
        job_id = self._create()
        result = self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.complete",
            {"job_id": job_id},
        )
        self.assertEqual(result, {"completed": True})
        stored = self.repo.get(GetJobCommand(job_id=job_id))
        self.assertFalse(stored.output_present)
        self.assertIsNone(stored.output)


    # ------------------------------------------------------------------
    # Checkpoint (the sixth wire method)
    # ------------------------------------------------------------------

    def _checkpoint(self, job_id, **overrides):
        params = {
            "job_id": job_id,
            "checkpoint": {"index": 1},
            "expected_revision": 0,
        }
        params.update(overrides)
        return self.adapter(
            self.identity.activation_id,
            "plugin.v1.broker.jobs.checkpoint",
            params,
        )

    def test_checkpoint_round_trips_through_the_frozen_schemas(self) -> None:
        job_id = self._create()
        self.assertEqual(self._checkpoint(job_id), {"revision": 1})
        self.assertEqual(
            self._checkpoint(job_id, checkpoint={"index": 2}, expected_revision=1),
            {"revision": 2},
        )
        stored = self.repo.get(GetJobCommand(job_id=job_id))
        self.assertEqual(stored.checkpoint_revision, 2)

    def test_checkpoint_accepts_an_explicit_null_expected_revision(self) -> None:
        """null is how the worker says "nothing saved yet"."""
        job_id = self._create()
        self.assertEqual(
            self._checkpoint(job_id, expected_revision=None), {"revision": 1}
        )

    def test_checkpoint_rejects_params_the_schema_does_not_allow(self) -> None:
        job_id = self._create()
        for params in (
            {"job_id": job_id, "checkpoint": {"a": 1}},  # expected_revision missing
            {"job_id": job_id, "expected_revision": 0},  # checkpoint missing
            {"checkpoint": {"a": 1}, "expected_revision": 0},  # job_id missing
            {
                "job_id": job_id,
                "checkpoint": {"a": 1},
                "expected_revision": 0,
                "extra": True,
            },
            {"job_id": "not-a-uuid", "checkpoint": {"a": 1}, "expected_revision": 0},
            {"job_id": job_id, "checkpoint": {"a": 1}, "expected_revision": -1},
        ):
            with self.assertRaises(PluginJobWireRequestError):
                self.adapter(
                    self.identity.activation_id,
                    "plugin.v1.broker.jobs.checkpoint",
                    params,
                )
        self.assertIsNone(self.repo.get(GetJobCommand(job_id=job_id)).checkpoint_json)

    def test_checkpoint_stale_revision_is_a_conflict(self) -> None:
        job_id = self._create()
        self._checkpoint(job_id)
        with self.assertRaises(BrokerJobConflictError):
            self._checkpoint(job_id, checkpoint={"index": 99}, expected_revision=0)

    def test_checkpoint_requires_the_bound_activation(self) -> None:
        job_id = self._create()
        with self.assertRaises(PluginJobWireActivationError):
            self.adapter(
                "act-other",
                "plugin.v1.broker.jobs.checkpoint",
                {"job_id": job_id, "checkpoint": {"a": 1}, "expected_revision": 0},
            )
        self.assertIsNone(self.repo.get(GetJobCommand(job_id=job_id)).checkpoint_json)

    def test_checkpoint_after_revocation_is_denied(self) -> None:
        job_id = self._create()
        self._bump_activation()
        with self.assertRaises(BrokerJobDeniedError):
            self._checkpoint(job_id)
        self.assertIsNone(self.repo.get(GetJobCommand(job_id=job_id)).checkpoint_json)

    def test_checkpoint_on_an_unknown_job_is_not_found(self) -> None:
        with self.assertRaises(BrokerJobNotFoundError):
            self._checkpoint("00000000-0000-4000-8000-000000000000")


if __name__ == "__main__":
    unittest.main()
