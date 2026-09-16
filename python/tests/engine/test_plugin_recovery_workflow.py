"""One ordered end-to-end proof of the plugin job recovery workflow.

Everything here runs against a real `build_engine_server` over a real Unix
socket, with the `examples/job-recovery-fixture` extension packed, installed
and enabled through the public extension operations and served by a real
isolated child process. Faults are injected only through the fixture's own
`control` operation, so the crash and the hang happen inside the worker for
the same reason they would in production.

The scenario is deliberately a single test: each step depends on the durable
state the previous one left, and splitting it would either re-run the whole
engine per assertion or hide the ordering the workflow is actually about.

What cannot be proven here, and why:

* Worker health, restart attempts and `gave_up` are read from
  `host.supervision_report(...)`, not from `engine.v1.extensions.get`. That
  result schema is a closed object with exactly extension_id / status /
  version / revision, so there is no member for supervision to ride on.
* Activation identity is deliberately absent from every public result, so the
  "a replacement is a different activation" assertions read `activation_id`
  off the durable job rows. Broker rejection of the *old* identity is proven
  by `tests.plugins.test_worker_loss_supervision`
  `test_replaced_worker_cannot_complete_its_job_with_a_stale_identity`.
* The shutdown report names each extension and whether a child was reaped, but
  carries no pid (ProcessRuntime exposes none), so the pid assertions use an
  independent `ps` census of this process's own children.

Measured step timings (Apple silicon, heartbeat shortened to interval 0.25s /
timeout 0.5s / max_missed 2), from the table this test prints on every run.
Five consecutive runs landed between 10.78 s and 10.88 s of module wall time:

    a start plugin                    0.19 s   (pack, install, enable, spawn)
    b run job and crash               0.03 s
    c detect worker failure           0.10 s
    d settle job                      0.00 s
    e restart safely                  0.58 s   (0.5 s backoff + respawn)
    f explicit resume                 1.23 s   (includes a 200-step job)
    g hung worker                     3.35 s   (heartbeat verdict + restart)
    h bounded restart                 4.25 s   (disable/enable, then 4 crashes
                                                at 0.5 / 1 / 2 s backoff)
    i stale authority                 0.00 s
    j shutdown and reopen             0.48 s

    steps total                      10.21 s
    module wall time                 10.8  s

Budget 45 s for this module in the gate. Every wait is bounded and the bounds
are roughly four times the observed times, so a loaded machine has room
without the test hanging.
"""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import build_engine_server
from model_deck.plugins.activation_lifecycle.restart import DEFAULT_RESTART_POLICY
from model_deck.plugins.authoring import pack_project_archive
from model_deck.plugins.external_host.host import (
    SHUTDOWN_QUIESCED,
    CHILD_REAPED,
    WorkerHeartbeatSettings,
)
from model_deck_contracts.paths import repo_root

EXTENSION_ID = "org.example.job-recovery"
COUNT_START = "org.example.job-recovery.count.start"
STREAM_START = "org.example.job-recovery.stream.start"
CONTROL = "org.example.job-recovery.control"

# Short enough that an unresponsive worker is detected in about a second, long
# enough that an ordinary invocation never looks like a missed beat.
FAST_HEARTBEAT = WorkerHeartbeatSettings(
    interval_s=0.25, timeout_s=0.5, max_missed=2
)

TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})
ACTIVE_JOB_STATES = frozenset({"queued", "running"})


class PluginRecoveryWorkflowTests(unittest.TestCase):
    """Drive the whole recovery workflow through public engine operations."""

    maxDiff = None

    def setUp(self) -> None:
        self._temps: list[tempfile.TemporaryDirectory[str]] = []
        self._runtime = None
        self._request_id = 1000
        self._timings: list[tuple[str, float]] = []
        self._sentinel: subprocess.Popen[bytes] | None = None

    def tearDown(self) -> None:
        if self._sentinel is not None and self._sentinel.poll() is None:
            self._sentinel.kill()
            self._sentinel.wait(timeout=5)
        if self._runtime is not None:
            self._runtime.server.stop()
        for temporary in reversed(self._temps):
            temporary.cleanup()
        if self._timings:
            print("\nplugin recovery workflow step timings (seconds):")
            for label, seconds in self._timings:
                print(f"  {label:<28} {seconds:6.2f}")
            print(f"  {'total':<28} {sum(s for _, s in self._timings):6.2f}")

    # ---------------------------------------------------------------- helpers

    def _directory(self) -> Path:
        # Short paths on purpose: the engine's Unix socket lives under one of
        # these and the sun_path limit is about a hundred bytes.
        temporary = tempfile.TemporaryDirectory(prefix="mdjr-", dir="/private/tmp")
        self._temps.append(temporary)
        return Path(temporary.name).resolve()

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _call(self, method: str, params: dict) -> dict:
        return self._session.call(
            {
                "jsonrpc": "2.0",
                "id": self._next_request_id(),
                "method": method,
                "params": params,
            }
        )

    def _result(self, method: str, params: dict) -> dict:
        response = self._call(method, params)
        self.assertNotIn("error", response, f"{method} failed: {response}")
        return response["result"]

    def _domain_error_code(self, method: str, params: dict) -> str:
        response = self._call(method, params)
        self.assertIn("error", response, f"{method} unexpectedly succeeded: {response}")
        return response["error"]["data"]["code"]

    def _authenticate(self) -> None:
        descriptor = load_rendezvous_file(self._runtime.rendezvous_path)
        credential = self._runtime.enrollment.credential_path.read_text(
            encoding="utf-8"
        ).strip()
        client = UnixSocketEngineClient(descriptor.socket_path)
        session_context = client.session()
        self._session = session_context.__enter__()
        self.addCleanup(session_context.__exit__, None, None, None)
        hello = self._result(
            "engine.v1.hello",
            {
                "client_name": "plugin-recovery-workflow-test",
                "offered_api": {"major": 1, "minor": 0},
                "authentication": {
                    "engine_instance_id": descriptor.engine_instance_id,
                    "instance_nonce": descriptor.instance_nonce,
                    "credential": credential,
                },
            },
        )
        self.assertTrue(hello["authenticated"])

    def _start_engine(self) -> None:
        self._runtime = build_engine_server(
            state_root=self._state,
            artifact_root=self._artifacts,
            socket_root=self._directory(),
            legacy_agents_dir=self._legacy,
            default_connection_id=str(uuid4()),
            source_root=repo_root(),
            enable_application_state=True,
            enable_external_extensions=True,
            extension_state_root=self._extension_state,
            extension_artifact_root=self._extension_artifacts,
            extension_worker_heartbeat=FAST_HEARTBEAT,
        )
        self._runtime.server.start()
        self._authenticate()

    def _job_rows(self) -> dict[str, sqlite3.Row]:
        """Durable job rows, read directly.

        The public surface deliberately never reports which activation owns a
        job, and there is no "list every job" operation, so the two assertions
        that need those — a replacement is a different activation, and no job
        was invented during recovery — read the rows instead.
        """

        connection = sqlite3.connect(str(self._extension_state / "host.sqlite3"))
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT job_id, activation_id, state, checkpoint_revision,"
                " resumable, resume_count FROM plugin_jobs"
            ).fetchall()
        finally:
            connection.close()
        return {row["job_id"]: row for row in rows}

    def _wait_until(self, predicate, *, timeout_s: float, description: str):
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            last = predicate()
            if last:
                return last
            time.sleep(0.02)
        self.fail(f"timed out after {timeout_s}s waiting for {description}: {last!r}")

    def _job_state(self, job_id: str) -> dict:
        return self._result("engine.v1.jobs.get", {"job_id": job_id})

    def _wait_for_job_state(self, job_id: str, state: str, *, timeout_s: float) -> dict:
        """Poll until the job reaches `state`, failing if it settles elsewhere.

        A job that reaches some other terminal state on the way is the whole
        bug this module exists to catch — an interrupted job that quietly
        completes means something replayed it — so the wait refuses to keep
        looking once the answer is final.
        """

        def observed():
            snapshot = self._job_state(job_id)
            if snapshot["state"] == state:
                return snapshot
            self.assertNotIn(
                snapshot["state"],
                TERMINAL_JOB_STATES,
                f"job {job_id} settled as {snapshot['state']}, expected {state}",
            )
            return None

        return self._wait_until(
            observed, timeout_s=timeout_s, description=f"job {job_id} to be {state}"
        )

    def _supervision(self):
        return self._host.supervision_report(EXTENSION_ID)

    def _wait_for_serving(self, *, timeout_s: float) -> None:
        self._wait_until(
            lambda: self._supervision().supervision_status == "healthy",
            timeout_s=timeout_s,
            description="the extension to be serving again",
        )

    def _set_control(self, key: str, **input_values) -> None:
        applied = self._result(
            "engine.v1.operations.invoke",
            {
                "operation": CONTROL,
                "input": dict(input_values),
                "idempotency_key": key,
            },
        )
        self.assertEqual(applied["output"], {"applied": True})

    def _start_job(self, operation: str, steps: int, key: str) -> str:
        started = self._result(
            "engine.v1.operations.invoke",
            {
                "operation": operation,
                "input": {"steps": steps},
                "idempotency_key": key,
            },
        )
        self.assertEqual(started["output"]["state"], "running")
        return started["job_id"]

    def _crash_the_worker(self, *, after_step: int, steps: int, key: str) -> str:
        """Set the crash fault, start a counting job, and return its id."""

        self._set_control(f"{key}-control", mode="crash", after_step=after_step)
        return self._start_job(COUNT_START, steps, f"{key}-job")

    def _child_pids(self) -> dict[int, str]:
        listing = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        children: dict[int, str] = {}
        for line in listing.splitlines():
            fields = line.split(None, 2)
            if len(fields) < 3:
                continue
            pid_text, parent_text, command = fields
            try:
                pid, parent = int(pid_text), int(parent_text)
            except ValueError:
                continue
            if parent == os.getpid():
                children[pid] = command
        return children

    @staticmethod
    def _process_is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _step(self, label: str):
        started = time.monotonic()

        class _Recorder:
            def __enter__(inner):
                return inner

            def __exit__(inner, *_exception):
                self._timings.append((label, time.monotonic() - started))
                return False

        return _Recorder()

    # ------------------------------------------------------------------ test

    def test_plugin_recovery_workflow_end_to_end(self) -> None:
        self._state = self._directory()
        self._artifacts = self._directory()
        self._legacy = self._directory()
        self._extension_state = self._directory()
        self._extension_artifacts = self._directory()
        archive = self._directory() / "job-recovery.zip"
        project_root = Path(__file__).resolve().parents[3]

        # (a) start plugin --------------------------------------------------
        with self._step("a start plugin"):
            pack_project_archive(
                project_root / "examples" / "job-recovery-fixture",
                output_path=archive,
            )
            self._start_engine()
            self._host = self._runtime.external_extension_host
            self.assertIsNotNone(self._host)

            inspected = self._result(
                "engine.v1.extensions.inspect", {"archive_path": str(archive)}
            )
            self.assertEqual(inspected["manifest"]["id"], EXTENSION_ID)
            installed = self._result(
                "engine.v1.extensions.install",
                {
                    "archive_path": str(archive),
                    "idempotency_key": "install",
                    "expected_revision": 0,
                },
            )
            self.assertEqual(installed["extension_id"], EXTENSION_ID)
            record = self._result(
                "engine.v1.extensions.get", {"extension_id": EXTENSION_ID}
            )
            self.assertEqual(record["status"], "installed")
            enabled = self._result(
                "engine.v1.extensions.enable",
                {
                    "extension_id": EXTENSION_ID,
                    "expected_revision": record["revision"],
                    "idempotency_key": "enable",
                },
            )
            self.assertEqual(enabled, {"enabled": True})

            serving = self._result(
                "engine.v1.extensions.get", {"extension_id": EXTENSION_ID}
            )
            self.assertEqual(serving["status"], "enabled")
            operations = {
                item["operation_id"]
                for item in self._result("engine.v1.operations.list", {})["operations"]
            }
            self.assertIn(COUNT_START, operations)
            self.assertIn(STREAM_START, operations)
            healthy = self._supervision()
            self.assertEqual(healthy.supervision_status, "healthy")
            self.assertEqual(healthy.worker_state.value, "healthy")
            self.assertEqual(healthy.restart_attempts, 0)
            self.assertFalse(healthy.gave_up)

        # (b) run a job that crashes its worker mid-flight -------------------
        with self._step("b run job and crash"):
            count_job = self._crash_the_worker(after_step=3, steps=6, key="count")
            running = self._job_state(count_job)
            self.assertEqual(running["state"], "running")
            self.assertTrue(running["resumable"])
            self.assertEqual(running["resume_count"], 0)
            crashed_activation = self._job_rows()[count_job]["activation_id"]

        # (c) detect the worker failure -------------------------------------
        with self._step("c detect worker failure"):
            loss = self._wait_until(
                lambda: (
                    self._supervision()
                    if self._supervision().last_failure_code is not None
                    else None
                ),
                timeout_s=15.0,
                description="the supervisor to record the worker loss",
            )
            self.assertEqual(loss.last_failure_code, "exited")
            self.assertEqual(loss.restart_attempts, 1)
            self.assertFalse(loss.gave_up)
            self.assertIn(loss.supervision_status, {"restarting", "healthy"})

        # (d) settle the job ------------------------------------------------
        with self._step("d settle job"):
            settled = self._wait_for_job_state(count_job, "interrupted", timeout_s=15.0)
            self.assertTrue(settled["resumable"])
            self.assertEqual(settled["resume_count"], 0)
            self.assertNotIn("output", settled)
            self.assertEqual(self._job_rows()[count_job]["checkpoint_revision"], 3)

        # (e) restart safely, replaying nothing ------------------------------
        with self._step("e restart safely"):
            self._wait_for_serving(timeout_s=20.0)
            after_restart = self._supervision()
            self.assertEqual(after_restart.restart_attempts, 1)
            self.assertFalse(after_restart.gave_up)
            self.assertEqual(set(self._job_rows()), {count_job})
            self.assertEqual(self._job_state(count_job)["state"], "interrupted")

            # The only public way to read the live activation id is to give the
            # replacement a job of its own and look at the row it owns.
            probe_job = self._start_job(STREAM_START, 1, "probe")
            probe_activation = self._job_rows()[probe_job]["activation_id"]
            self.assertNotEqual(probe_activation, crashed_activation)

        # (f) explicit resume -----------------------------------------------
        with self._step("f explicit resume"):
            resumed = self._result(
                "engine.v1.jobs.resume",
                {"job_id": count_job, "idempotency_key": "resume-count"},
            )
            self.assertEqual(
                resumed,
                {
                    "accepted": True,
                    "job_id": count_job,
                    "resumed_from_revision": 3,
                },
            )
            completed = self._wait_for_job_state(count_job, "completed", timeout_s=15.0)
            self.assertEqual(
                completed["output"],
                {"steps_performed_by_this_activation": 3, "final": 6},
            )
            self.assertEqual(completed["resume_count"], 1)
            resumed_activation = self._job_rows()[count_job]["activation_id"]
            self.assertNotEqual(resumed_activation, crashed_activation)

            replayed = self._result(
                "engine.v1.jobs.resume",
                {"job_id": count_job, "idempotency_key": "resume-count"},
            )
            self.assertEqual(replayed, resumed)
            self.assertEqual(self._job_state(count_job)["resume_count"], 1)

            # A resumable job that is merely running has nothing to resume from.
            long_job = self._start_job(COUNT_START, 200, "long")
            self.assertEqual(
                self._domain_error_code(
                    "engine.v1.jobs.resume",
                    {"job_id": long_job, "idempotency_key": "resume-running"},
                ),
                "resume_unavailable",
            )

            # An operation that never declared itself resumable stays
            # unresumable even after exactly the same crash.
            self._set_control("stream-control", mode="crash", after_step=2)
            stream_job = self._start_job(STREAM_START, 6, "stream")
            self.assertFalse(self._job_state(stream_job)["resumable"])
            self._wait_for_job_state(stream_job, "interrupted", timeout_s=15.0)
            self.assertEqual(
                self._domain_error_code(
                    "engine.v1.jobs.resume",
                    {"job_id": stream_job, "idempotency_key": "resume-stream"},
                ),
                "resume_unavailable",
            )
            # The same crash interrupted the long job with it; nothing replays.
            self.assertEqual(self._job_state(long_job)["state"], "interrupted")
            self._wait_for_serving(timeout_s=20.0)

        # (g) hung worker ----------------------------------------------------
        with self._step("g hung worker"):
            self._set_control("hang-control", mode="hang", after_step=2)
            hung_job = self._start_job(COUNT_START, 6, "hang")
            attempts_before_hang = self._supervision().restart_attempts
            unresponsive = self._wait_until(
                lambda: (
                    self._supervision()
                    if self._supervision().restart_attempts > attempts_before_hang
                    else None
                ),
                timeout_s=20.0,
                description="the heartbeat to declare the worker unresponsive",
            )
            self.assertEqual(unresponsive.last_failure_code, "unresponsive")
            self._wait_for_job_state(hung_job, "interrupted", timeout_s=15.0)
            self._wait_for_serving(timeout_s=20.0)

        # (h) bounded restart ------------------------------------------------
        with self._step("h bounded restart"):
            # Start the attempt budget clean so "gave up after max_attempts"
            # means what it says. Disable/enable is the operator's reset.
            self._cycle_extension("reset-before-budget")
            self.assertEqual(self._supervision().restart_attempts, 0)

            max_attempts = DEFAULT_RESTART_POLICY.max_attempts
            budget_jobs: list[str] = []
            for attempt in range(max_attempts + 1):
                self._wait_for_serving(timeout_s=20.0)
                before = self._supervision().restart_attempts
                budget_jobs.append(
                    self._crash_the_worker(
                        after_step=1, steps=6, key=f"budget-{attempt}"
                    )
                )
                self._wait_until(
                    lambda before=before: (
                        self._supervision().restart_attempts > before
                    ),
                    timeout_s=20.0,
                    description="the supervisor to count this crash",
                )
                if self._supervision().gave_up:
                    break

            exhausted = self._supervision()
            self.assertTrue(exhausted.gave_up)
            self.assertEqual(exhausted.restart_attempts, max_attempts + 1)
            self.assertEqual(exhausted.supervision_status, "degraded")
            self.assertEqual(
                self._domain_error_code(
                    "engine.v1.operations.invoke",
                    {
                        "operation": COUNT_START,
                        "input": {"steps": 1},
                        "idempotency_key": "while-degraded",
                    },
                ),
                "plugin_unavailable",
            )
            for job_id in budget_jobs:
                self.assertEqual(self._job_state(job_id)["state"], "interrupted")

            self._cycle_extension("reset-after-budget")
            recovered = self._supervision()
            self.assertEqual(recovered.restart_attempts, 0)
            self.assertFalse(recovered.gave_up)
            self.assertEqual(recovered.supervision_status, "healthy")
            self._set_control("after-reset", mode="normal")

        # (i) stale authority -------------------------------------------------
        with self._step("i stale authority"):
            # Not drivable from the public surface: no operation names an
            # activation. What is provable here is that every replacement is a
            # different activation, which is what makes the dead worker's
            # revoked identity useless. That the broker actually refuses the
            # old identity is proven by
            # tests.plugins.test_worker_loss_supervision
            # .test_replaced_worker_cannot_complete_its_job_with_a_stale_identity
            self.assertNotEqual(probe_activation, crashed_activation)
            self.assertNotEqual(resumed_activation, crashed_activation)
            activations = {
                row["activation_id"] for row in self._job_rows().values()
            }
            # Each replacement is a distinct activation, and the crashed one
            # owns nothing any more: the resume rebound its job to the live
            # activation, which is why the dead worker could not finish it.
            self.assertGreaterEqual(len(activations), 3)
            self.assertNotIn(crashed_activation, activations)

        # (j) shutdown and reopen ---------------------------------------------
        with self._step("j shutdown and reopen"):
            self._wait_for_serving(timeout_s=20.0)
            # A process this shutdown must not touch.
            self._sentinel = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"]
            )
            before_stop = self._child_pids()
            # The worker runs the staged artifact, so its command line names
            # this test's extension artifact root and nothing else does.
            plugin_pids = {
                pid
                for pid, command in before_stop.items()
                if str(self._extension_artifacts) in command
            }
            self.assertTrue(plugin_pids, f"no plugin child found in {before_stop}")
            self.assertIn(self._sentinel.pid, before_stop)

            expected_states = {
                job_id: snapshot["state"]
                for job_id, snapshot in (
                    (job_id, self._job_state(job_id))
                    for job_id in self._job_rows()
                )
            }
            self.assertEqual(expected_states[count_job], "completed")

            self._runtime.server.stop()
            report = self._host.last_shutdown_report
            entry = report.entry(EXTENSION_ID)
            self.assertIsNotNone(entry)
            self.assertEqual(entry.outcome, SHUTDOWN_QUIESCED)
            self.assertEqual(entry.child, CHILD_REAPED)
            self.assertIsNone(entry.failure_code)

            for pid in plugin_pids:
                self.assertFalse(
                    self._process_is_alive(pid),
                    f"plugin child {pid} survived shutdown",
                )
            self.assertTrue(
                self._process_is_alive(self._sentinel.pid),
                "shutdown reaped a process it did not own",
            )
            self.assertIsNone(self._sentinel.poll())
            self._sentinel.send_signal(signal.SIGKILL)
            self._sentinel.wait(timeout=5)

            self._runtime = None
            self._start_engine()
            self._host = self._runtime.external_extension_host
            for job_id, state in expected_states.items():
                reopened = self._job_state(job_id)
                self.assertEqual(
                    reopened["state"],
                    state,
                    f"job {job_id} changed state across the restart",
                )
                self.assertNotIn(reopened["state"], ACTIVE_JOB_STATES)
            self.assertEqual(self._job_state(count_job)["resume_count"], 1)
            self.assertEqual(
                self._job_state(count_job)["output"],
                {"steps_performed_by_this_activation": 3, "final": 6},
            )
            durable = self._job_rows()
            self.assertFalse(
                [row for row in durable.values() if row["state"] in ACTIVE_JOB_STATES],
                "a job still reads as active after the engine reopened",
            )

    def _cycle_extension(self, key: str) -> None:
        """Disable then enable, which is the operator's supervision reset."""

        record = self._result(
            "engine.v1.extensions.get", {"extension_id": EXTENSION_ID}
        )
        self.assertEqual(
            self._result(
                "engine.v1.extensions.disable",
                {
                    "extension_id": EXTENSION_ID,
                    "expected_revision": record["revision"],
                    "idempotency_key": f"{key}-disable",
                },
            ),
            {"enabled": False},
        )
        disabled = self._result(
            "engine.v1.extensions.get", {"extension_id": EXTENSION_ID}
        )
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(
            self._result(
                "engine.v1.extensions.enable",
                {
                    "extension_id": EXTENSION_ID,
                    "expected_revision": disabled["revision"],
                    "idempotency_key": f"{key}-enable",
                },
            ),
            {"enabled": True},
        )
        self._wait_for_serving(timeout_s=20.0)


if __name__ == "__main__":
    unittest.main()
