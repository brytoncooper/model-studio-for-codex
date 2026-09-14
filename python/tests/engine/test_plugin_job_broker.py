import math
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.engine.jobs.ports import GetJobCommand, JobOwner, JobState, RequestCancelCommand
from model_deck.engine.jobs.service import (
    BrokerJobConflictError,
    BrokerJobGuardError,
    BrokerJobOperationError,
    BrokerJobDeniedError,
    BrokerJobInvalidRequestError,
    BrokerJobNotFoundError,
    BrokerJobTerminalError,
    PluginJobBroker,
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
MISSING_UUID = "00000000-0000-4000-8000-000000000000"


class MemoryContexts:
    def __init__(self):
        self.contexts = {}

    def put(self, context):
        if context.invocation_id in self.contexts:
            raise ValueError("collision")
        self.contexts[context.invocation_id] = context

    def get(self, invocation_id):
        return self.contexts.get(invocation_id)


class FakeState:
    def __init__(self, activation_state, origin_state, operation_state):
        self.activation_state = activation_state
        self.origin_state = origin_state
        self.operation_state = operation_state

    def activation(self, identity):
        return self.activation_state

    def origin(self, principal_id):
        return self.origin_state

    def operation(self, operation_id):
        return self.operation_state


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 9, 12, tzinfo=timezone.utc)
        self.deadline = self.now + timedelta(minutes=5)
        self.identity = ActivationIdentity(
            "engine", "broker", "act-1", "com.example.worker", "1.0.0"
        )
        perms = dict(
            effects=frozenset({"read", "write"}),
            resource_scopes=frozenset({"jobs.private"}),
            capability_grants=frozenset({"jobs.access"}),
        )
        self.state = FakeState(
            ActivationState(self.identity, **perms, expires_at=self.deadline,
                            revocation_generation=2),
            OriginState("origin", "engine", "broker", **perms,
                        expires_at=self.deadline, revocation_generation=3),
            OperationAuthority("jobs.run", **perms),
        )
        self.contexts = MemoryContexts()

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
            contexts=self.contexts, clock=lambda: self.now,
        )
        self.broker = PluginJobBroker(
            authority=self.authority, repository=self.repo, grants=dict(GRANTS),
            mutation_guard=guard,
        )
        self.handle = self.authority.issue(
            self.identity, "origin", "jobs.run", expires_at=self.deadline
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _create(self, operation_id="jobs.run"):
        params = {"invocation_handle": self.handle, "operation_id": operation_id}
        return self.broker.create(
            params.pop("invocation_handle"), self.identity, **params
        )["job_id"]

    def _bump_activation(self):
        self.state.activation_state = replace(
            self.state.activation_state,
            revocation_generation=self.state.activation_state.revocation_generation + 1,
        )

    def test_create_persists_captured_invocation_and_owner(self):
        job_id = self._create()
        stored = self.repo.get(GetJobCommand(job_id=job_id))
        self.assertEqual(stored.plugin_id, "com.example.worker")
        self.assertEqual(stored.activation_id, "act-1")
        self.assertEqual(stored.operation_id, "jobs.run")
        # Origin is captured from the live trusted context, never the worker.
        self.assertEqual(stored.origin_principal_id, "origin")
        # Claim is folded into create; job is RUNNING by the time create returns.
        self.assertEqual(stored.state, JobState.RUNNING)
        ctx = self.contexts.get(stored.invocation_id)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.operation_id, "jobs.run")

    def test_create_operation_mismatch_denied_no_upgrade(self):
        with self.assertRaises(BrokerJobDeniedError):
            params = {"operation_id": "jobs.admin"}
            self.broker.create(self.handle, self.identity, **params)

    def test_cross_activation_followup_denied(self):
        job_id = self._create()
        other = replace(self.identity, activation_id="act-2")
        with self.assertRaises(BrokerJobDeniedError):
            params = {"job_id": job_id, "progress": 0.5}
            self.broker.report_progress(other, **params)

    def test_revoked_progress_and_completion_denied(self):
        job_id = self._create()
        self._bump_activation()
        with self.assertRaises(BrokerJobDeniedError):
            params = {"job_id": job_id, "progress": 0.5}
            self.broker.report_progress(self.identity, **params)
        with self.assertRaises(BrokerJobDeniedError):
            params = {"job_id": job_id}
            self.broker.complete(self.identity, **params)

    def test_cancel_flag_vs_confirmed(self):
        job_id = self._create()
        params = {"job_id": job_id}
        out = self.broker.check_cancelled(self.identity, **dict(params))
        self.assertEqual(out, {"cancelled": False})
        self.repo.request_cancel(
            RequestCancelCommand(
                job_id=job_id,
                owner=JobOwner(plugin_id="com.example.worker", activation_id="act-1"),
            )
        )
        # check_cancelled is the worker ack point; it confirms the cancel
        # by transitioning RUNNING -> CANCELLED.
        out = self.broker.check_cancelled(self.identity, **dict(params))
        self.assertEqual(out, {"cancelled": True})
        self.assertEqual(
            self.repo.get(GetJobCommand(job_id=job_id)).state, JobState.CANCELLED
        )

    def test_terminal_once(self):
        job_id = self._create()
        # Create folds claim; job is RUNNING by the time create() returns.
        self.assertEqual(
            self.repo.get(GetJobCommand(job_id=job_id)).state, JobState.RUNNING
        )
        out = self.broker.complete(self.identity, **{"job_id": job_id})
        self.assertEqual(out, {"completed": True})
        with self.assertRaises(BrokerJobTerminalError):
            self.broker.fail(
                self.identity, **{"job_id": job_id, "error": {"code": "internal", "retryable": False}}
            )
        with self.assertRaises(BrokerJobTerminalError):
            self.broker.report_progress(
                self.identity, **{"job_id": job_id, "progress": 0.5}
            )

    def test_complete_output_is_ordinary_json(self):
        job_id = self._create()
        out = self.broker.complete(
            self.identity,
            **{"job_id": job_id, "output": {"attachment_ref": "att-1", "n": [1, 2]}, "output_present": True},
        )
        self.assertEqual(out, {"completed": True})
        job_id2 = self._create()
        out = self.broker.complete(
            self.identity,
            **{"job_id": job_id2, "output": {"attachment_ref": "foreign-att"}, "output_present": True},
        )
        self.assertEqual(out, {"completed": True})

    def test_complete_strict_json_bounds(self):
        job_id = self._create()
        with self.assertRaises(BrokerJobInvalidRequestError):
            self.broker.complete(
                self.identity, **{"job_id": job_id, "output": float("nan"), "output_present": True}
            )
        with self.assertRaises(BrokerJobInvalidRequestError):
            self.broker.complete(
                self.identity, **{"job_id": job_id, "output": float("inf"), "output_present": True}
            )
        with self.assertRaises(BrokerJobInvalidRequestError):
            self.broker.complete(
                self.identity, **{"job_id": job_id, "output": {"k" * 1: object()}, "output_present": True}
            )
        cyclic: dict = {}
        cyclic["self"] = cyclic
        with self.assertRaises(BrokerJobInvalidRequestError):
            self.broker.complete(self.identity, **{"job_id": job_id, "output": cyclic, "output_present": True})
        out = self.broker.complete(self.identity, **{"job_id": job_id})
        self.assertEqual(out, {"completed": True})

    def test_fail_error_object_code_only(self):
        job_id = self._create()
        out = self.broker.fail(
            self.identity,
            **{"job_id": job_id, "error": {"code": "internal", "retryable": True, "message": "raw detail", "request_id": "r-1"}},
        )
        self.assertEqual(out, {"failed": True})
        stored = self.repo.get(GetJobCommand(job_id=job_id))
        self.assertEqual(stored.failure_code, "internal")
        self.assertEqual(stored.state, JobState.FAILED)

    def test_unknown_job_not_found_and_bad_code_invalid(self):
        with self.assertRaises(BrokerJobNotFoundError):
            self.broker.check_cancelled(self.identity, **{"job_id": MISSING_UUID})
        job_id = self._create()
        with self.assertRaises(BrokerJobInvalidRequestError):
            self.broker.fail(
                self.identity, **{"job_id": job_id, "error": {"code": "nope", "retryable": False}}
            )
        with self.assertRaises(BrokerJobInvalidRequestError):
            self.broker.fail(self.identity, **{"job_id": job_id, "error": {"code": "internal"}})
        with self.assertRaises(BrokerJobInvalidRequestError):
            self.broker.fail(self.identity, **{"job_id": job_id, "error": {"code": "internal", "retryable": "yes"}})

    def test_revocation_barrier_uses_same_guard(self):
        seen = []

        @contextmanager
        def guard():
            seen.append("enter")
            try:
                yield
            finally:
                seen.append("exit")

        broker = PluginJobBroker(
            authority=self.authority, repository=self.repo, grants=dict(GRANTS),
            mutation_guard=guard,
        )
        job_id = self._create()
        with broker.revocation_barrier():
            self._bump_activation()
        self.assertEqual(seen, ["enter", "exit"])
        with self.assertRaises(BrokerJobDeniedError):
            broker.check_cancelled(self.identity, **{"job_id": job_id})

    def test_regression_progress_conflict_maps(self):
        job_id = self._create()
        self.broker.report_progress(self.identity, **{"job_id": job_id, "progress": 0.5})
        with self.assertRaises(BrokerJobConflictError):
            self.broker.report_progress(self.identity, **{"job_id": job_id, "progress": 0.1})
        # State is RUNNING because claim is folded into create().
        self.assertEqual(
            self.repo.get(GetJobCommand(job_id=job_id)).state, JobState.RUNNING
        )

    def test_schema_valid_results(self):
        import json
        from pathlib import Path as _P
        base = _P("/Users/brytoncooper/Documents/Model Deck Architecture/contracts/plugin.v1/broker")
        job_id = self._create()
        # create() folds claim; job is RUNNING before the test does anything.
        self.assertEqual(
            self.repo.get(GetJobCommand(job_id=job_id)).state, JobState.RUNNING
        )
        progress_params = {"job_id": job_id, "progress": 0.5}
        progress_result = self.broker.report_progress(self.identity, **dict(progress_params))
        self.assertEqual(set(progress_result), {"accepted"})
        self.assertIs(progress_result["accepted"], True)
        check_result = self.broker.check_cancelled(self.identity, **{"job_id": job_id})
        self.assertEqual(set(check_result), {"cancelled"})
        complete_result = self.broker.complete(self.identity, **{"job_id": job_id})
        self.assertEqual(complete_result, {"completed": True})
        job_id2 = self._create()
        fail_result = self.broker.fail(
            self.identity, **{"job_id": job_id2, "error": {"code": "internal", "retryable": False}}
        )
        self.assertEqual(fail_result, {"failed": True})
        create_result = {"job_id": job_id2}
        for name in ("jobs.create.result", "jobs.progress.result", "jobs.check_cancelled.result", "jobs.complete.result", "jobs.fail.result"):
            schema = json.loads((base / (name + ".schema.json")).read_text())
            self.assertEqual(schema["type"], "object")
            self.assertFalse(schema.get("additionalProperties", True))
        _ = create_result


class RepairTests(unittest.TestCase):
    setUp = BrokerTests.setUp
    tearDown = BrokerTests.tearDown
    _create = BrokerTests._create
    _bump_activation = BrokerTests._bump_activation

    def test_optional_error_strings_follow_frozen_schema(self):
        for field in ('message', 'request_id'):
            job_id = self._create()
            with self.subTest(field=field), self.assertRaises(BrokerJobInvalidRequestError):
                self.broker.fail(self.identity, job_id=job_id,
                                 error={'code':'internal','retryable':False,field:None})
            # Claim is folded into create; the job is RUNNING, not QUEUED.
            self.assertEqual(self.repo.get(GetJobCommand(job_id)).state, JobState.RUNNING)
        job_id = self._create()
        self.assertEqual(self.broker.fail(self.identity, job_id=job_id,
                         error={'code':'internal','retryable':False,'message':'','request_id':''}),
                         {'failed':True})

    def test_large_integer_output_and_invalid_progress(self):
        job_id = self._create()
        self.assertEqual(self.broker.complete(self.identity, job_id=job_id,
                         output={'integer':10**400}, output_present=True), {'completed':True})
        job_id = self._create()
        with self.assertRaises(BrokerJobInvalidRequestError):
            self.broker.report_progress(self.identity, job_id=job_id, progress=10**400)
        self.assertEqual(self.repo.get(GetJobCommand(job_id)).progress, 0)

    def test_guard_factory_and_entry_failure_prevent_mutation(self):
        class BrokenEnter:
            def __enter__(self):
                raise RuntimeError('private entry secret')
            def __exit__(self, *args):
                raise AssertionError('entry did not succeed')
        def broken_factory():
            raise RuntimeError('private factory secret')
        for factory in (BrokenEnter, broken_factory):
            broker = PluginJobBroker(authority=self.authority, repository=self.repo,
                                    grants=dict(GRANTS), mutation_guard=factory)
            job_id = self._create()
            with self.subTest(factory=factory.__name__), self.assertRaisesRegex(
                    BrokerJobGuardError, '^broker guard failed$'):
                broker.complete(self.identity, job_id=job_id)
            self.assertEqual(self.repo.get(GetJobCommand(job_id)).state, JobState.RUNNING)

    def test_exit_failure_can_follow_a_committed_write(self):
        @contextmanager
        def broken_exit():
            yield
            raise RuntimeError('private exit secret')
        broker = PluginJobBroker(authority=self.authority, repository=self.repo,
                                grants=dict(GRANTS), mutation_guard=broken_exit)
        job_id = self._create()
        with self.assertRaisesRegex(BrokerJobGuardError, '^broker guard failed$'):
            broker.complete(self.identity, job_id=job_id)
        self.assertEqual(self.repo.get(GetJobCommand(job_id)).state, JobState.COMPLETED)

    def test_guard_cannot_suppress_authority_denial(self):
        class SuppressingGuard:
            def __enter__(self):
                return None
            def __exit__(self, *args):
                return True
        broker = PluginJobBroker(authority=self.authority, repository=self.repo,
                                grants=dict(GRANTS), mutation_guard=SuppressingGuard)
        job_id = self._create()
        self._bump_activation()
        with self.assertRaises(BrokerJobDeniedError):
            broker.complete(self.identity, job_id=job_id)
        self.assertEqual(self.repo.get(GetJobCommand(job_id)).state, JobState.RUNNING)

    def test_repository_failure_is_safe(self):
        job_id = self._create()
        original = self.repo.complete
        def broken_complete(command):
            raise RuntimeError('private repository secret')
        self.repo.complete = broken_complete
        try:
            with self.assertRaisesRegex(BrokerJobOperationError, '^broker operation failed$'):
                self.broker.complete(self.identity, job_id=job_id)
        finally:
            self.repo.complete = original
        self.assertEqual(self.repo.get(GetJobCommand(job_id)).state, JobState.RUNNING)


if __name__ == "__main__":
    unittest.main()
