import math
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.engine.jobs.first_party import (
    FIRST_PARTY_PLUGIN_ID,
    FIRST_PARTY_PLUGIN_PREFIX,
)
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


class BrokerFixture(unittest.TestCase):
    """The real authority / SQLite / broker composition every case below uses."""

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


class BrokerTests(BrokerFixture):

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

    def test_plugin_claiming_the_reserved_engine_namespace_is_denied(self):
        """A plugin must never borrow the engine's authority over its own jobs."""
        for plugin_id in (
            FIRST_PARTY_PLUGIN_ID,
            FIRST_PARTY_PLUGIN_PREFIX + "prices.refresh",
        ):
            impostor = replace(self.identity, plugin_id=plugin_id)
            with self.subTest(plugin_id=plugin_id):
                with self.assertRaises(BrokerJobDeniedError):
                    self.broker.create(self.handle, impostor, operation_id="jobs.run")
                # Every follow-up is closed too, not just creation.
                job_id = self._create()
                with self.assertRaises(BrokerJobDeniedError):
                    self.broker.check_cancelled(impostor, job_id=job_id)
                with self.assertRaises(BrokerJobDeniedError):
                    self.broker.fail(
                        impostor,
                        job_id=job_id,
                        error={"code": "internal", "retryable": False},
                    )

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


class CheckpointTests(BrokerFixture):
    """The sixth broker method: compare-and-swap resume state.

    Inherits the real authority / SQLite / broker composition above, so every
    assertion here runs against the same trusted path the worker uses.
    """

    def _schema_repo(self):
        """A broker whose jobs declare a checkpoint schema at create time."""
        return self.broker

    def _create_with_schema(self, schema_id="ckpt.v1"):
        return self.broker.create(
            self.handle,
            self.identity,
            operation_id="jobs.run",
            checkpoint_schema_id=schema_id,
        )["job_id"]

    def test_checkpoint_saves_and_advances_the_revision(self):
        job_id = self._create()
        first = self.broker.checkpoint(
            self.identity, job_id=job_id, checkpoint={"index": 1}, expected_revision=0
        )
        self.assertEqual(first, {"revision": 1})
        second = self.broker.checkpoint(
            self.identity, job_id=job_id, checkpoint={"index": 2}, expected_revision=1
        )
        self.assertEqual(second, {"revision": 2})
        stored = self.repo.get(GetJobCommand(job_id))
        self.assertEqual(stored.checkpoint_revision, 2)
        self.assertEqual(stored.checkpoint_json, '{"index":2}')

    def test_null_expected_revision_means_nothing_saved_yet(self):
        job_id = self._create()
        self.assertEqual(
            self.broker.checkpoint(
                self.identity,
                job_id=job_id,
                checkpoint={"index": 1},
                expected_revision=None,
            ),
            {"revision": 1},
        )

    def test_stale_revision_conflicts_and_stores_nothing(self):
        job_id = self._create()
        self.broker.checkpoint(
            self.identity, job_id=job_id, checkpoint={"index": 1}, expected_revision=0
        )
        with self.assertRaises(BrokerJobConflictError):
            self.broker.checkpoint(
                self.identity,
                job_id=job_id,
                checkpoint={"index": 99},
                expected_revision=0,
            )
        self.assertEqual(self.repo.get(GetJobCommand(job_id)).checkpoint_json, '{"index":1}')

    def test_omitting_schema_id_keeps_the_schema_the_job_declared(self):
        """The published params schema says schema_id is optional; it is.

        Omitting it is the documented default, not a mistake. Storage keeps
        the declared schema and validates the checkpoint against it, so an
        unlabelled save is stored labelled and checked all the same. A worker
        written from the contract calls exactly this way.
        """
        job_id = self._create_with_schema()
        self.assertEqual(
            self.broker.checkpoint(
                self.identity,
                job_id=job_id,
                checkpoint={"index": 1},
                expected_revision=0,
            ),
            {"revision": 1},
        )
        stored = self.repo.get(GetJobCommand(job_id))
        self.assertEqual(stored.checkpoint_schema_id, "ckpt.v1")
        self.assertEqual(stored.checkpoint_json, '{"index":1}')

    def test_a_declared_schema_cannot_be_replaced_by_another(self):
        job_id = self._create_with_schema()
        with self.assertRaises(BrokerJobInvalidRequestError):
            self.broker.checkpoint(
                self.identity,
                job_id=job_id,
                checkpoint={"index": 1},
                expected_revision=0,
                schema_id="ckpt.v2",
            )
        self.assertIsNone(self.repo.get(GetJobCommand(job_id)).checkpoint_json)
        self.assertEqual(
            self.broker.checkpoint(
                self.identity,
                job_id=job_id,
                checkpoint={"index": 1},
                expected_revision=0,
                schema_id="ckpt.v1",
            ),
            {"revision": 1},
        )

    def test_schema_id_cannot_be_introduced_where_none_was_declared(self):
        job_id = self._create()
        with self.assertRaises(BrokerJobInvalidRequestError):
            self.broker.checkpoint(
                self.identity,
                job_id=job_id,
                checkpoint={"index": 1},
                expected_revision=0,
                schema_id="ckpt.v1",
            )
        self.assertIsNone(self.repo.get(GetJobCommand(job_id)).checkpoint_json)

    def test_checkpoint_requires_the_owning_activation(self):
        job_id = self._create()
        stranger = replace(self.identity, activation_id="act-2")
        with self.assertRaises(BrokerJobDeniedError):
            self.broker.checkpoint(
                stranger, job_id=job_id, checkpoint={"index": 1}, expected_revision=0
            )
        self.assertIsNone(self.repo.get(GetJobCommand(job_id)).checkpoint_json)

    def test_revoked_activation_cannot_checkpoint(self):
        job_id = self._create()
        self._bump_activation()
        with self.assertRaises(BrokerJobDeniedError):
            self.broker.checkpoint(
                self.identity, job_id=job_id, checkpoint={"index": 1}, expected_revision=0
            )
        self.assertIsNone(self.repo.get(GetJobCommand(job_id)).checkpoint_json)

    def test_terminal_job_cannot_checkpoint(self):
        job_id = self._create()
        self.broker.complete(self.identity, job_id=job_id)
        with self.assertRaises((BrokerJobConflictError, BrokerJobTerminalError)):
            self.broker.checkpoint(
                self.identity, job_id=job_id, checkpoint={"index": 1}, expected_revision=0
            )

    def test_oversized_checkpoint_is_refused_and_stores_nothing(self):
        job_id = self._create()
        with self.assertRaises(BrokerJobInvalidRequestError):
            self.broker.checkpoint(
                self.identity,
                job_id=job_id,
                checkpoint={"blob": "x" * (1024 * 1024 + 8)},
                expected_revision=0,
            )
        self.assertIsNone(self.repo.get(GetJobCommand(job_id)).checkpoint_json)

    def test_malformed_requests_are_refused(self):
        job_id = self._create()
        for expected_revision in (-1, 1.5, True, "1"):
            with self.assertRaises(BrokerJobInvalidRequestError):
                self.broker.checkpoint(
                    self.identity,
                    job_id=job_id,
                    checkpoint={"index": 1},
                    expected_revision=expected_revision,
                )
        with self.assertRaises(BrokerJobInvalidRequestError):
            self.broker.checkpoint(
                self.identity,
                job_id="not-a-uuid",
                checkpoint={"index": 1},
                expected_revision=0,
            )
        with self.assertRaises(BrokerJobInvalidRequestError):
            self.broker.checkpoint(
                self.identity,
                job_id=job_id,
                checkpoint={"bad": float("nan")},
                expected_revision=0,
            )

    def test_unknown_job_is_not_found(self):
        with self.assertRaises(BrokerJobNotFoundError):
            self.broker.checkpoint(
                self.identity,
                job_id=MISSING_UUID,
                checkpoint={"index": 1},
                expected_revision=0,
            )

    def test_an_explicit_checkpoint_grant_is_accepted(self):
        broker = PluginJobBroker(
            authority=self.authority,
            repository=self.repo,
            grants={**GRANTS, "checkpoint": WRITE},
            mutation_guard=self.guard,
        )
        job_id = self._create()
        self.assertEqual(
            broker.checkpoint(
                self.identity, job_id=job_id, checkpoint={"index": 1}, expected_revision=0
            ),
            {"revision": 1},
        )

    def test_a_denied_checkpoint_grant_refuses_the_save(self):
        """The grant is real authority, not decoration."""
        broker = PluginJobBroker(
            authority=self.authority,
            repository=self.repo,
            grants={**GRANTS, "checkpoint": ("write", "jobs.private", "not.granted")},
            mutation_guard=self.guard,
        )
        job_id = self._create()
        with self.assertRaises(BrokerJobDeniedError):
            broker.checkpoint(
                self.identity, job_id=job_id, checkpoint={"index": 1}, expected_revision=0
            )


class ResumableFlagTests(BrokerFixture):
    """Only the manifest can make a job resumable, through composition."""

    def test_jobs_are_not_resumable_without_a_lookup(self):
        job_id = self._create()
        self.assertIs(self.repo.get(GetJobCommand(job_id)).resumable, False)

    def test_create_persists_what_the_lookup_says(self):
        seen = []

        def lookup(operation_id):
            seen.append(operation_id)
            return operation_id == "jobs.run"

        broker = PluginJobBroker(
            authority=self.authority,
            repository=self.repo,
            grants=dict(GRANTS),
            mutation_guard=self.guard,
            resumable_operations=lookup,
        )
        job_id = broker.create(self.handle, self.identity, operation_id="jobs.run")["job_id"]
        self.assertIs(self.repo.get(GetJobCommand(job_id)).resumable, True)
        # The operation asked about is the one the trusted context settled on,
        # never a value the worker supplied.
        self.assertEqual(seen, ["jobs.run"])

    def test_a_failing_lookup_fails_the_create_instead_of_guessing(self):
        """A broken lookup must not be written down as "never declared".

        Answering False would put a durable, permanent lie in the row: the
        job would report resume_unavailable for life, indistinguishable from
        an operation that really never declared the flag. Failing the create
        is recoverable; a silently unresumable job is not.
        """

        def lookup(operation_id):
            raise RuntimeError("private manifest secret")

        broker = PluginJobBroker(
            authority=self.authority,
            repository=self.repo,
            grants=dict(GRANTS),
            mutation_guard=self.guard,
            resumable_operations=lookup,
        )
        with self.assertRaises(BrokerJobOperationError) as captured:
            broker.create(self.handle, self.identity, operation_id="jobs.run")
        # The lookup's own words stay inside the engine.
        self.assertNotIn("secret", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
