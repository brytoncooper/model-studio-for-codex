"""Explicit job resume, end to end against a real plugin subprocess.

Nothing here is simulated except worker loss itself: the plugin is a separate
isolated Python process speaking the real broker protocol, the job state is the
real SQLite repository, the broker and wire are the real ones, and every public
call goes through ``EngineDispatch`` so the domain error codes are the ones a
client would actually see.

The plugin performs a four-step job. It gets through two steps, checkpoints,
and is then lost. ``engine.v1.jobs.resume`` hands it back its own checkpoint
and it performs only the remaining two, which is what makes duplicate work
observable rather than assumed.

The same plugin also runs the other branch: lost before it ever checkpointed,
resumed from a null revision with ``checkpoint: null``, performing every step
in the resumed run.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.engine.dispatch import EngineDispatch, principal_id_for_client_name
from model_deck.engine.jobs.ports import (
    GetJobCommand,
    JobOwner,
    JobState,
    ResumeTarget,
)
from model_deck.engine.jobs.service import PluginJobBroker
from model_deck.engine.jobs.use_cases import (
    CancelJobUseCase,
    GetJobUseCase,
    ResumeJobUseCase,
)
from model_deck.engine.jobs.wire import PluginJobWireAdapter
from model_deck.engine.plugin_authority import (
    ActivationIdentity,
    ActivationState,
    OperationAuthority,
    OriginState,
    PluginAuthority,
)
from model_deck.plugins.lifecycle_session import LifecycleSession
from model_deck.plugins.process_runtime import ProcessRuntime, ProcessRuntimeConfig

PLUGIN_ID = "org.example.jobs"
PLUGIN_VERSION = "1.0.0"
ACTIVATION_ID = "11111111-2222-4333-8444-555555555555"
OPERATION_ID = "org.example.jobs.work"
RESUME_OPERATION_ID = OPERATION_ID + ".resume"
CHECKPOINT_SCHEMA_ID = "ckpt.v1"
CLIENT_NAME = "job-resume-test"
UNKNOWN_JOB_ID = "00000000-0000-4000-8000-000000000000"
TOTAL_STEPS = 4
STEPS_BEFORE_LOSS = 2

JOB_METHODS = (
    "plugin.v1.broker.jobs.create",
    "plugin.v1.broker.jobs.progress",
    "plugin.v1.broker.jobs.checkpoint",
    "plugin.v1.broker.jobs.complete",
    "plugin.v1.broker.jobs.fail",
    "plugin.v1.broker.jobs.check_cancelled",
)
WRITE_GRANT = ("write", "jobs.own", "jobs.own")
READ_GRANT = ("read", "jobs.own", "jobs.own")
GRANTS = {
    "create": WRITE_GRANT,
    "progress": WRITE_GRANT,
    "checkpoint": WRITE_GRANT,
    "complete": WRITE_GRANT,
    "fail": WRITE_GRANT,
    "check_cancelled": READ_GRANT,
}


# The plugin. It checkpoints the steps it has finished and, on resume, does
# only what the checkpoint says is left, reporting exactly those steps back.
CHILD_CODE = f"""
import importlib.util
import json
import sys

PLUGIN_ID = {PLUGIN_ID!r}
ACTIVATION_ID = {ACTIVATION_ID!r}
OPERATION_ID = {OPERATION_ID!r}
RESUME_OPERATION_ID = {RESUME_OPERATION_ID!r}
CHECKPOINT_SCHEMA_ID = {CHECKPOINT_SCHEMA_ID!r}
TOTAL_STEPS = {TOTAL_STEPS}
STEPS_BEFORE_LOSS = {STEPS_BEFORE_LOSS}

if not sys.flags.isolated or importlib.util.find_spec("model_deck") is not None:
    raise RuntimeError("fixture requires isolated standard-library imports")


def send(payload):
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\\n")
    sys.stdout.flush()


def call(request_id, method, params):
    send({{"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}})
    return json.loads(sys.stdin.readline())


def save_checkpoint(job_id, done, expected_revision, request_id, name_schema):
    # schema_id is optional in the published contract: omitting it keeps the
    # schema the job declared at create. The first run takes that default and
    # the resumed run names the schema explicitly, so one pass over the real
    # wire covers both spellings a plugin author might write.
    params = {{
        "job_id": job_id,
        "checkpoint": {{"done": done}},
        "expected_revision": expected_revision,
    }}
    if name_schema:
        params["schema_id"] = CHECKPOINT_SCHEMA_ID
    response = call(request_id, "plugin.v1.broker.jobs.checkpoint", params)
    return response["result"]["revision"]


def start(invocation):
    broker_context = invocation["broker_context"]
    steps_before_loss = (invocation["input"] or {{}}).get(
        "steps_before_loss", STEPS_BEFORE_LOSS
    )
    created = call(
        6001,
        "plugin.v1.broker.jobs.create",
        {{
            "invocation_handle": broker_context["invocation_handle"],
            "operation_id": OPERATION_ID,
            "checkpoint_schema_id": CHECKPOINT_SCHEMA_ID,
        }},
    )
    job_id = created["result"]["job_id"]
    done = []
    revision = None
    for step in range(steps_before_loss):
        done.append(step)
        call(
            6100 + step,
            "plugin.v1.broker.jobs.progress",
            {{"job_id": job_id, "progress": len(done) / TOTAL_STEPS}},
        )
        revision = save_checkpoint(job_id, done, revision, 6200 + step, False)
    # The job is deliberately left running: this worker is about to be lost.
    return job_id, {{"job_id": job_id, "performed": done}}


def resume(invocation_input):
    job_id = invocation_input["job_id"]
    checkpoint = invocation_input["checkpoint"] or {{"done": []}}
    revision = invocation_input["checkpoint_revision"]
    done = list(checkpoint["done"])
    performed_now = []
    for step in range(TOTAL_STEPS):
        if step in done:
            continue
        done.append(step)
        performed_now.append(step)
        call(
            6300 + step,
            "plugin.v1.broker.jobs.progress",
            {{"job_id": job_id, "progress": len(done) / TOTAL_STEPS}},
        )
        revision = save_checkpoint(job_id, done, revision, 6400 + step, True)
    call(
        6500,
        "plugin.v1.broker.jobs.complete",
        {{
            "job_id": job_id,
            "output": {{"performed_after_resume": performed_now, "done": done}},
        }},
    )
    return {{"performed_after_resume": performed_now}}


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method", "")
    request_id = request.get("id")
    if method == "plugin.v1.lifecycle.hello":
        send({{"jsonrpc": "2.0", "id": request_id, "result": {{
            "plugin_id": PLUGIN_ID,
            "plugin_version": {PLUGIN_VERSION!r},
            "capabilities": [],
        }}}})
        continue
    if method == "plugin.v1.lifecycle.activate":
        send({{"jsonrpc": "2.0", "id": request_id, "result": {{
            "activation_id": ACTIVATION_ID,
            "invocation_handle_prefix": "jobs:",
        }}}})
        continue
    if method != "plugin.v1.invoke":
        raise RuntimeError("unexpected method")

    invocation = request["params"]
    if invocation["operation_id"] == RESUME_OPERATION_ID:
        send({{
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {{"output": resume(invocation["input"])}},
        }})
        continue
    job_id, output = start(invocation)
    send({{
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {{"output": output, "job_id": job_id}},
    }})
"""


class MemoryContexts:
    def __init__(self) -> None:
        self.contexts: dict[str, object] = {}

    def put(self, context) -> None:
        if context.invocation_id in self.contexts:
            raise ValueError("context collision")
        self.contexts[context.invocation_id] = context

    def get(self, invocation_id):
        return self.contexts.get(invocation_id)


class AuthorityState:
    """The supervisor's view of who may do what, as the host would hold it."""

    def __init__(self, activation, origin, operations) -> None:
        self.activation_state = activation
        self.origin_state = origin
        self.operations = operations

    def activation(self, _identity):
        return self.activation_state

    def origin(self, _principal_id):
        return self.origin_state

    def operation(self, operation_id):
        return self.operations.get(operation_id)


class HostResumeInvoker:
    """What the extension host does to hand one job back to its plugin.

    ``prepare`` resolves the plugin's current serving activation and captures
    a fresh invocation authority for ``"<op>.resume"``; ``invoke`` runs it on
    the real invocation channel. ``serving`` standing in for the enabled /
    disabled state is the only shortcut.
    """

    def __init__(self, *, authority, identity, channel, deadline, timeout_s=5.0) -> None:
        self._authority = authority
        self._identity = identity
        self._channel = channel
        self._deadline = deadline
        self._timeout_s = timeout_s
        self.serving = True
        self.handles: list[str] = []

    def prepare(self, request) -> ResumeTarget | None:
        if not self.serving:
            return None
        handle = self._authority.issue(
            self._identity,
            request.origin_principal_id,
            request.resume_operation_id,
            expires_at=self._deadline,
        )
        self.handles.append(handle)
        context = self._authority.capture(handle, self._identity)
        return ResumeTarget(
            activation_id=self._identity.activation_id,
            invocation_id=context.invocation_id,
        )

    def invoke(self, request, target) -> None:
        self._channel.invoke(
            request.resume_operation_id,
            request.invocation_params(),
            {
                "activation_id": target.activation_id,
                "plugin_id": request.plugin_id,
                "invocation_handle": self.handles[-1],
                "revocation_generation": 2,
            },
            timeout_s=self._timeout_s,
        )


class JobDirectory:
    """The host's job methods, shaped exactly as dispatch expects them."""

    def __init__(self, repository, invoker) -> None:
        self._get = GetJobUseCase(repository)
        self._cancel = CancelJobUseCase(repository)
        self._resume = ResumeJobUseCase(repository, invoker=invoker)

    def job_get(self, params, *, principal):
        return self._get.execute(params, caller_principal_id=principal)

    def job_cancel(self, params, *, principal):
        return self._cancel.execute(params, caller_principal_id=principal)

    def job_resume(self, params, *, principal):
        return self._resume.execute(params, caller_principal_id=principal)

    def operation_catalog(self):
        """This stand-in contributes no plugin operations of its own."""
        return ()


class ListModelsStub:
    def execute(self, params):
        return {"items": []}


class EnrollmentStub:
    def verify(self, engine_instance_id, instance_nonce, credential):
        return True


class JobResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="md-resume-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.engine_instance_id = str(uuid4())
        self.principal = principal_id_for_client_name(
            CLIENT_NAME, self.engine_instance_id
        )
        now = datetime(2026, 9, 12, tzinfo=timezone.utc)
        self.deadline = now + timedelta(minutes=5)
        permissions = {
            "effects": frozenset({"read", "write"}),
            "resource_scopes": frozenset({"jobs.own"}),
            "capability_grants": frozenset({"jobs.own"}),
        }
        self.identity = ActivationIdentity(
            "engine-test", "jobs-test", ACTIVATION_ID, PLUGIN_ID, PLUGIN_VERSION
        )
        self.state = AuthorityState(
            ActivationState(
                self.identity,
                **permissions,
                expires_at=self.deadline,
                revocation_generation=2,
            ),
            OriginState(
                self.principal,
                "engine-test",
                "jobs-test",
                **permissions,
                expires_at=self.deadline,
                revocation_generation=3,
            ),
            {
                OPERATION_ID: OperationAuthority(OPERATION_ID, **permissions),
                RESUME_OPERATION_ID: OperationAuthority(
                    RESUME_OPERATION_ID, **permissions
                ),
            },
        )
        self.authority = PluginAuthority(
            engine_instance_id="engine-test",
            audience="jobs-test",
            state=self.state,
            contexts=MemoryContexts(),
            clock=lambda: now,
        )
        self.repository = SQLitePluginJobRepository(
            self.root / "jobs.sqlite3",
            checkpoint_validator=self._validate_checkpoint,
        )
        mutation_lock = threading.RLock()

        @contextmanager
        def mutation_guard():
            with mutation_lock:
                yield

        broker = PluginJobBroker(
            authority=self.authority,
            repository=self.repository,
            grants=dict(GRANTS),
            mutation_guard=mutation_guard,
            # What the manifest's `resumable: true` looks like once composed.
            resumable_operations=lambda operation_id: operation_id == OPERATION_ID,
        )
        wire = PluginJobWireAdapter(trusted_activation=self.identity, broker=broker)
        self.runtime = ProcessRuntime(
            ProcessRuntimeConfig(
                argv=(sys.executable, "-I", "-c", CHILD_CODE),
                package_dir=str(self.root),
                timeout_s=5,
            ),
            allowed_broker_methods=JOB_METHODS,
            broker_request_handler=wire,
        )
        self.addCleanup(self.runtime.close)
        watchdog = threading.Timer(20, self.runtime.close)
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        self.runtime.spawn()
        lifecycle = LifecycleSession(
            expected_plugin_id=PLUGIN_ID,
            expected_plugin_version=PLUGIN_VERSION,
            offered_api_major=1,
            offered_api_minor=0,
            activation_token="job-resume-token",
            allowed_broker_methods=JOB_METHODS,
        )
        self.runtime.run_hello(lifecycle, "job-resume-nonce")
        self.runtime.run_activation(lifecycle)
        self.channel = self.runtime.invocation_channel()
        self.invoker = HostResumeInvoker(
            authority=self.authority,
            identity=self.identity,
            channel=self.channel,
            deadline=self.deadline,
        )
        self.dispatch = EngineDispatch(
            ListModelsStub(),
            SimpleNamespace(
                engine_instance_id=self.engine_instance_id,
                instance_nonce="job-resume-rendezvous",
            ),
            EnrollmentStub(),
            external_extension_host=JobDirectory(self.repository, self.invoker),
        )
        self._authenticate()
        self._next_request_id = 100

    @staticmethod
    def _validate_checkpoint(schema_id, value):
        if schema_id != CHECKPOINT_SCHEMA_ID:
            raise ValueError(f"unknown checkpoint schema: {schema_id}")
        if not isinstance(value, dict) or not isinstance(value.get("done"), list):
            raise ValueError("checkpoint must carry a done list")

    def _authenticate(self) -> None:
        response = self.dispatch.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "engine.v1.hello",
                "params": {
                    "client_name": CLIENT_NAME,
                    "offered_api": {"major": 1, "minor": 0},
                    "authentication": {
                        "engine_instance_id": self.engine_instance_id,
                        "instance_nonce": "job-resume-rendezvous",
                        "credential": "enrolled",
                    },
                },
            },
            1,
        )
        self.assertTrue(response["result"]["authenticated"])

    def _call(self, method, params):
        self._next_request_id += 1
        return self.dispatch.handle(
            {
                "jsonrpc": "2.0",
                "id": self._next_request_id,
                "method": method,
                "params": params,
            },
            1,
        )

    def _resume(self, job_id, key="resume-1"):
        return self._call(
            "engine.v1.jobs.resume", {"job_id": job_id, "idempotency_key": key}
        )

    def _owner(self) -> JobOwner:
        return JobOwner(plugin_id=PLUGIN_ID, activation_id=ACTIVATION_ID)

    def _start_job(self, steps_before_loss: int = STEPS_BEFORE_LOSS) -> str:
        """Run the operation until the worker has done part of the work.

        ``steps_before_loss=0`` is the job that crashes before it ever
        checkpoints, which is the branch that resumes from a null revision.
        """
        handle = self.authority.issue(
            self.identity, self.principal, OPERATION_ID, expires_at=self.deadline
        )
        response = self.channel.invoke(
            OPERATION_ID,
            {"steps_before_loss": steps_before_loss},
            {
                "activation_id": ACTIVATION_ID,
                "plugin_id": PLUGIN_ID,
                "invocation_handle": handle,
                "revocation_generation": 2,
            },
            timeout_s=5,
        )
        return response["job_id"]

    def _lose_worker(self, job_id: str) -> None:
        """Exactly what activation loss does to still-active jobs."""
        self.repository.mark_worker_crashed(self._owner())
        self.assertEqual(
            self.repository.get(GetJobCommand(job_id=job_id)).state,
            JobState.INTERRUPTED,
        )

    def _interrupted_job(self) -> str:
        job_id = self._start_job()
        self._lose_worker(job_id)
        return job_id

    def test_interrupted_job_resumes_and_completes_without_repeating_work(self) -> None:
        job_id = self._interrupted_job()
        before = self._call("engine.v1.jobs.get", {"job_id": job_id})["result"]
        self.assertEqual(before["state"], "interrupted")
        self.assertTrue(before["resumable"])
        self.assertEqual(before["resume_count"], 0)

        resumed = self._resume(job_id)["result"]
        self.assertEqual(
            resumed,
            {"accepted": True, "job_id": job_id, "resumed_from_revision": 2},
        )

        after = self._call("engine.v1.jobs.get", {"job_id": job_id})["result"]
        self.assertEqual(after["state"], "completed")
        self.assertEqual(after["resume_count"], 1)
        self.assertEqual(after["progress"], 1.0)
        # The plugin performed only the steps the checkpoint said were left:
        # the two it already did are not repeated.
        self.assertEqual(after["output"]["performed_after_resume"], [2, 3])
        self.assertEqual(after["output"]["done"], [0, 1, 2, 3])

    def test_a_job_lost_before_its_first_checkpoint_resumes_from_null(self) -> None:
        """The branch a crash on step one takes: there is no checkpoint yet.

        ``resumed_from_revision`` is required and nullable precisely for this,
        and the plugin is handed ``checkpoint: null`` rather than a made-up
        empty one. Nothing was done before the loss, so the resumed run has to
        perform every step itself.
        """
        job_id = self._start_job(steps_before_loss=0)
        record = self.repository.get(GetJobCommand(job_id=job_id))
        self.assertIsNone(record.checkpoint_json)
        self.assertEqual(record.checkpoint_revision, 0)
        self._lose_worker(job_id)

        resumed = self._resume(job_id)["result"]
        self.assertEqual(
            resumed,
            {"accepted": True, "job_id": job_id, "resumed_from_revision": None},
        )

        after = self._call("engine.v1.jobs.get", {"job_id": job_id})["result"]
        self.assertEqual(after["state"], "completed")
        self.assertEqual(after["resume_count"], 1)
        self.assertEqual(after["progress"], 1.0)
        self.assertEqual(after["output"]["performed_after_resume"], [0, 1, 2, 3])

    def test_resume_rebinds_the_job_to_a_fresh_invocation_authority(self) -> None:
        job_id = self._interrupted_job()
        before = self.repository.get(GetJobCommand(job_id=job_id))
        self._resume(job_id)
        after = self.repository.get(GetJobCommand(job_id=job_id))
        # The interrupted run's invocation is gone, so the row is rebound to
        # the authority the resume captured; otherwise the worker's first
        # progress call could not re-authorize.
        self.assertNotEqual(after.invocation_id, before.invocation_id)
        self.assertEqual(after.plugin_id, before.plugin_id)

    def test_repeating_the_same_key_returns_the_same_answer(self) -> None:
        job_id = self._interrupted_job()
        first = self._resume(job_id, key="resume-1")["result"]
        again = self._resume(job_id, key="resume-1")["result"]
        self.assertEqual(again, first)
        self.assertEqual(
            self.repository.get(GetJobCommand(job_id=job_id)).resume_count, 1
        )

    def test_a_different_key_on_an_already_resumed_job_is_a_conflict(self) -> None:
        job_id = self._interrupted_job()
        # A worker that has been handed the job and has not finished it yet.
        # That is exactly when a second resume would run the same work twice,
        # so it is refused rather than accepted.
        self.invoker.invoke = lambda request, target: None
        self._resume(job_id, key="resume-1")
        self.assertEqual(
            self.repository.get(GetJobCommand(job_id=job_id)).state, JobState.RUNNING
        )
        conflict = self._resume(job_id, key="resume-2")
        self.assertEqual(conflict["error"]["data"]["code"], "conflict")
        self.assertEqual(
            self.repository.get(GetJobCommand(job_id=job_id)).resume_count, 1
        )

    def test_resuming_a_running_job_is_unavailable(self) -> None:
        job_id = self._start_job()
        response = self._resume(job_id)
        self.assertEqual(response["error"]["data"]["code"], "resume_unavailable")
        self.assertEqual(
            self.repository.get(GetJobCommand(job_id=job_id)).state, JobState.RUNNING
        )

    def test_resuming_a_job_whose_operation_is_not_resumable_is_unavailable(self) -> None:
        """The manifest flag is the only thing that makes a job resumable."""
        job_id = self._start_job()
        self._lose_worker(job_id)
        record = self.repository.get(GetJobCommand(job_id=job_id))
        self.assertTrue(record.resumable)
        # The same job, stored as a contribution that never declared the flag.
        import sqlite3

        connection = sqlite3.connect(str(self.root / "jobs.sqlite3"))
        connection.execute(
            "UPDATE plugin_jobs SET resumable = 0 WHERE job_id = ?", (job_id,)
        )
        connection.commit()
        connection.close()

        response = self._resume(job_id)
        self.assertEqual(response["error"]["data"]["code"], "resume_unavailable")

    def test_resuming_an_unknown_job_is_not_found(self) -> None:
        response = self._resume(UNKNOWN_JOB_ID)
        self.assertEqual(response["error"]["data"]["code"], "not_found")

    def test_resume_while_the_plugin_is_not_serving_is_plugin_unavailable(self) -> None:
        job_id = self._interrupted_job()
        self.invoker.serving = False
        response = self._resume(job_id)
        self.assertEqual(response["error"]["data"]["code"], "plugin_unavailable")
        # Nothing was spent: the job is still resumable under the same key.
        self.assertEqual(
            self.repository.get(GetJobCommand(job_id=job_id)).state,
            JobState.INTERRUPTED,
        )
        self.invoker.serving = True
        self.assertTrue(self._resume(job_id)["result"]["accepted"])

    def test_another_principal_cannot_resume_the_job(self) -> None:
        job_id = self._interrupted_job()
        stranger = EngineDispatch(
            ListModelsStub(),
            SimpleNamespace(
                engine_instance_id=self.engine_instance_id,
                instance_nonce="job-resume-rendezvous",
            ),
            EnrollmentStub(),
            external_extension_host=JobDirectory(self.repository, self.invoker),
        )
        stranger.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "engine.v1.hello",
                "params": {
                    "client_name": "someone-else",
                    "offered_api": {"major": 1, "minor": 0},
                    "authentication": {
                        "engine_instance_id": self.engine_instance_id,
                        "instance_nonce": "job-resume-rendezvous",
                        "credential": "enrolled",
                    },
                },
            },
            2,
        )
        response = stranger.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "engine.v1.jobs.resume",
                "params": {"job_id": job_id, "idempotency_key": "resume-1"},
            },
            2,
        )
        self.assertEqual(response["error"]["data"]["code"], "capability_denied")

    def test_malformed_resume_params_are_rejected_before_any_effect(self) -> None:
        job_id = self._interrupted_job()
        for params in (
            {"job_id": job_id},
            {"idempotency_key": "resume-1"},
            {"job_id": "not-a-uuid", "idempotency_key": "resume-1"},
            {"job_id": job_id, "idempotency_key": "resume-1", "extra": True},
        ):
            response = self._call("engine.v1.jobs.resume", params)
            self.assertIn("error", response)
        self.assertEqual(
            self.repository.get(GetJobCommand(job_id=job_id)).state,
            JobState.INTERRUPTED,
        )

    def test_resume_is_advertised_as_a_public_operation(self) -> None:
        operations = self._call("engine.v1.operations.list", {})["result"]["operations"]
        self.assertIn(
            "engine.v1.jobs.resume", [item["operation_id"] for item in operations]
        )


if __name__ == "__main__":
    unittest.main()
