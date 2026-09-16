"""External acceptance tests for the isolated Job Recovery fixture.

No engine is involved anywhere in this file. `_Session` spawns plugin.py as
a raw subprocess and speaks its stdin/stdout wire directly, playing the
host's part (hello/activate/invoke/heartbeat/drain/deactivate) itself.
`_FakeJobsBroker` answers the plugin's own plugin.v1.broker.jobs.* calls
the way a real engine would, and records every call so tests can assert on
exactly what was checkpointed, completed, or failed.
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from model_deck.plugins.authoring import pack_project_archive, validate_project_archive
from model_deck_contracts.validator import validate_schema_ref


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = EXAMPLE_ROOT / "plugin.py"
MANIFEST_PATH = EXAMPLE_ROOT / "manifest.json"
PLUGIN_ID = "org.example.job-recovery"
PLUGIN_VERSION = "1.0.0"
FRAME_LIMIT = 1 * 1024 * 1024

START_COUNT = "org.example.job-recovery.count.start"
RESUME_COUNT = "org.example.job-recovery.count.start.resume"
START_STREAM = "org.example.job-recovery.stream.start"
CONTROL = "org.example.job-recovery.control"
CHECKPOINT_SCHEMA_ID = "schemas/count.checkpoint.schema.json"

JOBS_CREATE = "plugin.v1.broker.jobs.create"
JOBS_CHECKPOINT = "plugin.v1.broker.jobs.checkpoint"
JOBS_COMPLETE = "plugin.v1.broker.jobs.complete"
JOBS_FAIL = "plugin.v1.broker.jobs.fail"
ALL_JOBS_METHODS = (JOBS_CREATE, JOBS_CHECKPOINT, JOBS_COMPLETE, JOBS_FAIL)

LIFECYCLE_KINDS = frozenset(
    {"hello", "activate", "heartbeat", "invoke", "cancel", "drain", "deactivate"}
)
CANONICAL_LIFECYCLE_METHODS = frozenset(f"plugin.v1.{kind}" for kind in LIFECYCLE_KINDS)

# Schemas for the three operations the manifest actually contributes (and
# therefore that a real host would see through `engine.v1.operations.list`).
# `count.start.resume` is deliberately absent from this map: per the resume
# convention (`engine/jobs/ports.py`'s `RESUME_OPERATION_SUFFIX` docstring),
# it is never a contributed operation, so it must never appear in
# `manifest["contributes"]["operations"]` -- only a supervisor invokes it
# directly, never through the public operations.invoke channel. See
# RESUME_SCHEMA_FILES below for its (still bundled, just not declared)
# input/output schemas.
OPERATION_SCHEMA_FILES = {
    START_COUNT: ("schemas/count.start.input.schema.json", "schemas/count.start.output.schema.json"),
    START_STREAM: ("schemas/stream.start.input.schema.json", "schemas/stream.start.output.schema.json"),
    CONTROL: ("schemas/control.input.schema.json", "schemas/control.output.schema.json"),
}

# Bundled alongside the plugin and validated by this file's own tests (see
# _schema_registry and test_schemas_accept_representative_values), but never
# referenced from manifest.json -- resume has no public input/output schema
# because it has no public operation entry.
RESUME_SCHEMA_FILES = (
    "schemas/count.start.resume.input.schema.json",
    "schemas/count.start.resume.output.schema.json",
)


def _schema_registry() -> tuple[Registry, dict[str, dict[str, Any]]]:
    schemas: dict[str, dict[str, Any]] = {}
    resources: dict[str, Resource] = {}
    for schema_path in sorted((EXAMPLE_ROOT / "schemas").glob("*.json")):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        schemas[str(schema_path.relative_to(EXAMPLE_ROOT))] = schema
        resources[schema["$id"]] = Resource.from_contents(schema)
    return Registry().with_resources(resources.items()), schemas


class PackageContractTests(unittest.TestCase):
    def test_manifest_schemas_and_isolated_worker_are_valid(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(sorted(manifest["permissions"]), sorted(["storage.own", "jobs.own"]))

        declared_operations = {op["id"]: op for op in manifest["contributes"]["operations"]}
        self.assertEqual(set(declared_operations), set(OPERATION_SCHEMA_FILES))
        for operation_id, (input_schema, output_schema) in OPERATION_SCHEMA_FILES.items():
            self.assertEqual(declared_operations[operation_id]["input_schema"], input_schema)
            self.assertEqual(declared_operations[operation_id]["output_schema"], output_schema)
            self.assertTrue((EXAMPLE_ROOT / input_schema).is_file())
            self.assertTrue((EXAMPLE_ROOT / output_schema).is_file())
        self.assertIs(declared_operations[START_COUNT].get("resumable"), True)
        self.assertNotIn("resumable", declared_operations[START_STREAM])
        self.assertNotIn("resumable", declared_operations[CONTROL])

        # The resume convention (engine/jobs/ports.py's RESUME_OPERATION_SUFFIX
        # docstring): a "<operation>.resume" invoke target is never a
        # contributed operation -- it must never appear in
        # engine.v1.operations.list, and only the supervisor ever calls it.
        # This fixture is the worked example of that convention, so it must
        # not itself list one; a plugin author copying it should not inherit
        # a public entry point that bypasses the resume ladder's ownership
        # and INTERRUPTED-state checks.
        self.assertNotIn(RESUME_COUNT, declared_operations)
        for operation_id in declared_operations:
            self.assertFalse(
                operation_id.endswith(".resume"),
                f"{operation_id!r} must not be a contributed operation "
                "-- resume is invoked directly by the supervisor, never "
                "listed publicly",
            )
        # count.start.resume's input/output schemas still ship in the
        # archive (this fixture's own tests validate them directly below),
        # they are simply not wired to a manifest operation entry.
        for schema_path in RESUME_SCHEMA_FILES:
            self.assertTrue((EXAMPLE_ROOT / schema_path).is_file())

        registry, schemas = _schema_registry()
        for schema in schemas.values():
            Draft202012Validator(
                schema, registry=registry, format_checker=Draft202012Validator.FORMAT_CHECKER
            )

        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import model_deck", source)
        self.assertNotIn("from model_deck", source)

        with tempfile.TemporaryDirectory(prefix="job-recovery-package-", dir="/tmp") as temporary_directory:
            archive_path = Path(temporary_directory) / "job-recovery.zip"
            packed = pack_project_archive(EXAMPLE_ROOT, output_path=archive_path)
            report = validate_project_archive(archive_path.read_bytes())
            self.assertTrue(report.ok)
            self.assertEqual(packed.output_path, archive_path)
            self.assertEqual(report.manifest.identity.manifest_id, PLUGIN_ID)

            # Same check against the packed-and-validated archive, not just
            # the source manifest.json above: this is the data a real
            # engine.v1.operations.list would be built from, so this is
            # where "no operation id ending in .resume appears in
            # operations.list" actually gets exercised end to end.
            packaged_operation_ids = {
                operation.operation_id for operation in report.manifest.contributions.operations
            }
            self.assertEqual(packaged_operation_ids, set(OPERATION_SCHEMA_FILES))
            self.assertNotIn(RESUME_COUNT, packaged_operation_ids)
            self.assertFalse(any(op_id.endswith(".resume") for op_id in packaged_operation_ids))

    def test_schemas_accept_representative_values(self) -> None:
        registry, schemas = _schema_registry()
        job_id = "11111111-2222-4333-8444-555555555555"
        representative_values = {
            "schemas/count.start.input.schema.json": {"steps": 5},
            "schemas/count.start.output.schema.json": {"job_id": job_id, "state": "running"},
            "schemas/count.start.resume.input.schema.json": {
                "job_id": job_id,
                "checkpoint_revision": 2,
                "checkpoint_schema_id": CHECKPOINT_SCHEMA_ID,
                "checkpoint": {"next": 3, "target": 5},
            },
            "schemas/count.start.resume.output.schema.json": {"job_id": job_id, "state": "running"},
            "schemas/stream.start.input.schema.json": {"steps": 3},
            "schemas/stream.start.output.schema.json": {"job_id": job_id, "state": "running"},
            "schemas/control.input.schema.json": {"mode": "crash", "after_step": 2},
            "schemas/control.output.schema.json": {"applied": True},
            "schemas/count.checkpoint.schema.json": {"next": 3, "target": 5},
        }
        self.assertEqual(set(schemas), set(representative_values))
        for schema_name, value in representative_values.items():
            with self.subTest(schema=schema_name):
                Draft202012Validator(
                    schemas[schema_name],
                    registry=registry,
                    format_checker=Draft202012Validator.FORMAT_CHECKER,
                ).validate(value)


class _FakeJobsBroker:
    """Answers plugin.v1.broker.jobs.* like a real engine and records every call.

    Checkpoint bookkeeping is a real compare-and-swap ledger keyed by
    job_id: a mismatched expected_revision fails loudly (an
    AssertionError from the reader thread, surfaced through the pending
    ProtocolFailure the next time the test reads a response), which is
    exactly what should happen if this fixture's own revision tracking
    ever regresses.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: list[tuple[str, dict[str, Any]]] = []
        self._revisions: dict[str, int] = {}

    def calls_for(self, method: str, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(params)
                for name, params in self._calls
                if name == method and params.get("job_id") == job_id
            ]

    def all_calls(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return list(self._calls)

    def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._calls.append((method, dict(params)))
        if method == JOBS_CREATE:
            job_id = str(uuid.uuid4())
            self._revisions[job_id] = 0
            return {"job_id": job_id}
        if method == JOBS_CHECKPOINT:
            job_id = params["job_id"]
            expected = params["expected_revision"] if params["expected_revision"] is not None else 0
            current = self._revisions.setdefault(job_id, expected)
            if expected != current:
                raise AssertionError(
                    f"checkpoint conflict for {job_id}: plugin expected revision {expected}, broker has {current}"
                )
            current += 1
            self._revisions[job_id] = current
            return {"revision": current}
        if method == JOBS_COMPLETE:
            return {"completed": True}
        if method == JOBS_FAIL:
            return {"failed": True}
        raise AssertionError(f"fake broker received an unexpected method: {method!r}")


class _Session:
    """Drives plugin.py over its raw stdin/stdout wire; no engine involved."""

    def __init__(self, broker: _FakeJobsBroker) -> None:
        self._broker = broker
        self._proc = subprocess.Popen(
            [sys.executable, "-I", "-B", str(PLUGIN_PATH)],
            cwd=str(EXAMPLE_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._stdout_fd = self._proc.stdout.fileno()
        self._stdin_fd = self._proc.stdin.fileno()
        self._write_lock = threading.Lock()
        self._buffer = bytearray()
        self._next_id = 0
        self._pending: dict[int, dict[str, Any]] = {}
        self._condition = threading.Condition()
        self._reader_done = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _write(self, frame: dict[str, Any]) -> None:
        line = (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")
        if len(line) > FRAME_LIMIT:
            raise ValueError("test frame exceeds the 1 MiB codec budget")
        with self._write_lock:
            os.write(self._stdin_fd, line)

    def _next_line(self) -> bytes | None:
        while True:
            newline_index = self._buffer.find(b"\n")
            if newline_index >= 0:
                line = bytes(self._buffer[:newline_index])
                del self._buffer[: newline_index + 1]
                return line
            readable, _, _ = select.select([self._stdout_fd], [], [], 5.0)
            if not readable:
                return None
            chunk = os.read(self._stdout_fd, FRAME_LIMIT + 1)
            if not chunk:
                return None
            self._buffer.extend(chunk)

    def _read_loop(self) -> None:
        try:
            while True:
                line = self._next_line()
                if line is None:
                    return
                frame = json.loads(line.decode("utf-8"))
                if "method" in frame:
                    result = self._broker.handle(frame["method"], frame.get("params", {}))
                    self._write({"jsonrpc": "2.0", "id": frame["id"], "result": result})
                    continue
                with self._condition:
                    self._pending[frame["id"]] = frame
                    self._condition.notify_all()
        finally:
            self._reader_done.set()
            with self._condition:
                self._condition.notify_all()

    def request(self, method: str, params: dict[str, Any], *, timeout_s: float = 5.0) -> dict[str, Any]:
        if method in CANONICAL_LIFECYCLE_METHODS:
            kind = method.rsplit(".", 1)[-1]
            validate_schema_ref(f"contracts/plugin.v1/lifecycle/{kind}.params.schema.json", params)
        with self._condition:
            self._next_id += 1
            request_id = self._next_id
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while request_id not in self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._reader_done.is_set():
                    raise AssertionError(
                        f"timed out waiting for a response to {method}; stderr: {self._drain_stderr()!r}"
                    )
                self._condition.wait(timeout=remaining)
            response = self._pending.pop(request_id)
        if method in CANONICAL_LIFECYCLE_METHODS and "result" in response:
            kind = method.rsplit(".", 1)[-1]
            validate_schema_ref(f"contracts/plugin.v1/lifecycle/{kind}.result.schema.json", response["result"])
        return response

    def expect_no_response(self, method: str, params: dict[str, Any], *, timeout_s: float) -> None:
        """Send a request and assert nothing answers it within timeout_s (hang mode)."""
        with self._condition:
            self._next_id += 1
            request_id = self._next_id
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while request_id not in self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._condition.wait(timeout=remaining)
        raise AssertionError(f"expected no response to {method}, but one arrived")

    def _drain_stderr(self) -> str:
        """Best-effort, non-blocking read of whatever the child has written to stderr."""
        stderr_fd = self._proc.stderr.fileno()
        chunks: list[bytes] = []
        while True:
            readable, _, _ = select.select([stderr_fd], [], [], 0)
            if not readable:
                break
            chunk = os.read(stderr_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def is_alive(self) -> bool:
        return self._proc.poll() is None

    def wait_exit(self, timeout_s: float) -> int:
        return self._proc.wait(timeout=timeout_s)

    def close(self) -> None:
        """Terminate-and-reap, like a real host's ProcessRuntime._stop.

        A worker that already answered plugin.v1.deactivate is not left to
        exit on its own: its daemon stdin-reader thread can still be
        blocked in a read when the interpreter starts finalizing, which
        CPython treats as fatal (SIGABRT) rather than deadlocking. Real
        hosts never wait out that race -- they terminate unconditionally.
        """
        if self._proc.poll() is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            try:
                self._proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


class _ActivatedWorker:
    """One job-recovery subprocess, already through hello and activate."""

    def __init__(self) -> None:
        self.broker = _FakeJobsBroker()
        self.session = _Session(self.broker)
        hello = self.session.request(
            "plugin.v1.hello", {"offered_api": {"major": 1, "minor": 0}, "nonce": "job-recovery-test"}
        )["result"]
        if "fixture.isolated-imports" not in hello["capabilities"]:
            raise AssertionError("worker did not confirm isolated imports")
        activated = self.session.request(
            "plugin.v1.activate",
            {"activation_token": "job-recovery-test-token", "allowed_broker_methods": list(ALL_JOBS_METHODS)},
        )["result"]
        self.activation_id = activated["activation_id"]
        self._handle_counter = 0

    def _next_handle(self) -> str:
        self._handle_counter += 1
        return f"job-recovery-handle-{self._handle_counter}"

    def invoke(self, operation_id: str, operation_input: dict[str, Any], *, timeout_s: float = 5.0) -> dict[str, Any]:
        context = {
            "activation_id": self.activation_id,
            "plugin_id": PLUGIN_ID,
            "invocation_handle": self._next_handle(),
            "revocation_generation": 0,
        }
        response = self.session.request(
            "plugin.v1.invoke",
            {"operation_id": operation_id, "input": operation_input, "broker_context": context},
            timeout_s=timeout_s,
        )
        if "result" not in response:
            raise AssertionError(f"invoke {operation_id} failed: {response}")
        return response["result"]

    def set_control(self, mode: str, after_step: int | None = None) -> None:
        control_input: dict[str, Any] = {"mode": mode}
        if after_step is not None:
            control_input["after_step"] = after_step
        result = self.invoke(CONTROL, control_input)
        if result["output"] != {"applied": True}:
            raise AssertionError(f"control was not applied: {result}")

    def heartbeat(self) -> dict[str, Any]:
        response = self.session.request("plugin.v1.heartbeat", {"activation_id": self.activation_id})
        if "result" not in response:
            raise AssertionError(f"heartbeat failed: {response}")
        return response["result"]

    def expect_hung(self, *, timeout_s: float = 1.0) -> None:
        self.session.expect_no_response(
            "plugin.v1.heartbeat", {"activation_id": self.activation_id}, timeout_s=timeout_s
        )

    def wait_for_job_output(self, job_id: str, *, timeout_s: float = 2.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            completions = self.broker.calls_for(JOBS_COMPLETE, job_id)
            if completions:
                return completions[-1]["output"]
            time.sleep(0.01)
        raise AssertionError(f"job {job_id} never reached jobs.complete within {timeout_s}s")

    def close(self) -> None:
        self.session.close()


class JobRecoveryFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = _ActivatedWorker()
        self.addCleanup(self.worker.close)

    def test_count_start_checkpoints_every_step_and_completes(self) -> None:
        result = self.worker.invoke(START_COUNT, {"steps": 5})
        self.assertEqual(result["output"]["state"], "running")
        job_id = result["job_id"]
        self.assertEqual(result["output"]["job_id"], job_id)
        uuid.UUID(job_id)  # a real uuid, not just any string

        all_creates = [p for m, p in self.worker.broker.all_calls() if m == JOBS_CREATE]
        self.assertEqual(all_creates[-1]["operation_id"], START_COUNT)
        self.assertEqual(all_creates[-1]["checkpoint_schema_id"], CHECKPOINT_SCHEMA_ID)

        output = self.worker.wait_for_job_output(job_id)
        self.assertEqual(output, {"steps_performed_by_this_activation": 5, "final": 5})

        checkpoints = self.worker.broker.calls_for(JOBS_CHECKPOINT, job_id)
        self.assertEqual(len(checkpoints), 5)
        self.assertIsNone(checkpoints[0]["expected_revision"])
        self.assertEqual(checkpoints[0]["checkpoint"], {"next": 2, "target": 5})
        self.assertEqual(checkpoints[-1]["checkpoint"], {"next": 6, "target": 5})
        for earlier, later in zip(checkpoints, checkpoints[1:]):
            self.assertEqual(later["expected_revision"], earlier["checkpoint"]["next"] - 1)

    def test_stream_start_never_checkpoints_and_declares_no_schema(self) -> None:
        result = self.worker.invoke(START_STREAM, {"steps": 3})
        job_id = result["job_id"]
        output = self.worker.wait_for_job_output(job_id)
        self.assertEqual(output, {"steps_performed_by_this_activation": 3, "final": 3})
        self.assertEqual(self.worker.broker.calls_for(JOBS_CHECKPOINT, job_id), [])
        stream_creates = [p for m, p in self.worker.broker.all_calls() if m == JOBS_CREATE and p["operation_id"] == START_STREAM]
        self.assertNotIn("checkpoint_schema_id", stream_creates[-1])

    def test_control_crash_exits_the_process_with_status_one(self) -> None:
        self.worker.set_control("crash", after_step=2)
        result = self.worker.invoke(START_COUNT, {"steps": 5})
        job_id = result["job_id"]
        returncode = self.worker.session.wait_exit(timeout_s=3.0)
        self.assertEqual(returncode, 1)
        # The crash fires after step 2's checkpoint is durably recorded, so
        # a resume would pick up at next=3 with no gap and no re-do.
        checkpoints = self.worker.broker.calls_for(JOBS_CHECKPOINT, job_id)
        self.assertEqual(len(checkpoints), 2)
        self.assertEqual(checkpoints[-1]["checkpoint"], {"next": 3, "target": 5})
        self.assertEqual(self.worker.broker.calls_for(JOBS_COMPLETE, job_id), [])

    def test_control_hang_stops_answering_including_heartbeats(self) -> None:
        self.worker.set_control("hang", after_step=2)
        result = self.worker.invoke(START_COUNT, {"steps": 5})
        job_id = result["job_id"]
        deadline = time.monotonic() + 2.0
        while len(self.worker.broker.calls_for(JOBS_CHECKPOINT, job_id)) < 2:
            if time.monotonic() > deadline:
                raise AssertionError("hang fixture never reached step 2")
            time.sleep(0.01)
        time.sleep(0.05)  # let the counting thread finish triggering hang
        self.assertTrue(self.worker.session.is_alive())
        self.worker.expect_hung(timeout_s=0.5)
        self.assertTrue(self.worker.session.is_alive())
        self.assertEqual(self.worker.broker.calls_for(JOBS_COMPLETE, job_id), [])

    def test_resume_continues_from_checkpoint_with_the_same_output_shape(self) -> None:
        """The resume convention: params carry job_id/checkpoint_revision/schema_id/checkpoint.

        No jobs.create call precedes this -- resume never creates a new job.
        count.start.resume shares count.start's completion shape by
        construction (both finish through JobRecoveryWorker._complete_
        counting), so summing steps_performed_by_this_activation across a
        crashed run's checkpoints and a resumed run's output is how a real
        host would notice duplicate or missing effects.
        """
        job_id = str(uuid.uuid4())
        checkpoint_revision = 2
        checkpoint = {"next": 3, "target": 5}
        result = self.worker.invoke(
            RESUME_COUNT,
            {
                "job_id": job_id,
                "checkpoint_revision": checkpoint_revision,
                "checkpoint_schema_id": CHECKPOINT_SCHEMA_ID,
                "checkpoint": checkpoint,
            },
        )
        self.assertEqual(result["job_id"], job_id)
        self.assertEqual(result["output"], {"job_id": job_id, "state": "running"})
        self.assertEqual(self.worker.broker.calls_for(JOBS_CREATE, job_id), [])

        output = self.worker.wait_for_job_output(job_id)
        self.assertEqual(output, {"steps_performed_by_this_activation": 3, "final": 5})

        checkpoints = self.worker.broker.calls_for(JOBS_CHECKPOINT, job_id)
        self.assertEqual(len(checkpoints), 3)
        self.assertEqual(checkpoints[0]["expected_revision"], checkpoint_revision)
        self.assertEqual(checkpoints[0]["checkpoint"], {"next": 4, "target": 5})
        self.assertEqual(checkpoints[-1]["checkpoint"], {"next": 6, "target": 5})

    def test_resume_rejects_a_mismatched_checkpoint_schema_id(self) -> None:
        response = self.worker.session.request(
            "plugin.v1.invoke",
            {
                "operation_id": RESUME_COUNT,
                "input": {
                    "job_id": str(uuid.uuid4()),
                    "checkpoint_revision": 1,
                    "checkpoint_schema_id": "schemas/some.other.schema.json",
                    "checkpoint": {"next": 1, "target": 1},
                },
                "broker_context": {
                    "activation_id": self.worker.activation_id,
                    "plugin_id": PLUGIN_ID,
                    "invocation_handle": "job-recovery-handle-error-test",
                    "revocation_generation": 0,
                },
            },
        )
        self.assertEqual(response["error"]["code"], -32602)

    def test_heartbeat_stays_answered_while_a_normal_job_runs(self) -> None:
        result = self.worker.invoke(START_COUNT, {"steps": 20})
        job_id = result["job_id"]
        self.assertEqual(self.worker.heartbeat(), {"alive": True})
        self.worker.wait_for_job_output(job_id, timeout_s=3.0)

    def test_unknown_lifecycle_method_is_rejected_not_fatal(self) -> None:
        response = self.worker.session.request("plugin.v1.not_a_real_method", {})
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(self.worker.heartbeat(), {"alive": True})  # process is still fine

    def test_drain_then_deactivate(self) -> None:
        drained = self.worker.session.request("plugin.v1.drain", {"deadline_ms": 1000})
        self.assertEqual(drained["result"], {"drained": True})
        deactivated = self.worker.session.request("plugin.v1.deactivate", {})
        self.assertEqual(deactivated["result"], {"deactivated": True})
        # See _Session.close: a real host terminates here rather than
        # waiting for the worker to exit on its own.
        self.worker.close()
        self.assertFalse(self.worker.session.is_alive())


if __name__ == "__main__":
    unittest.main()
