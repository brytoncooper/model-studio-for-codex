#!/usr/bin/env python3
"""Isolated Job Recovery worker: a deterministic fixture for jobs.resume.

Gives the resume convention (a "<operation>.resume" invoke carrying
job_id/checkpoint_revision/checkpoint_schema_id/checkpoint) something real
to run against, with no engine involved: tests/test_job_recovery_fixture.py
drives this file directly over stdin/stdout, playing the host's part
(hello/activate/invoke/heartbeat) and answering plugin.v1.broker.jobs.*
with a small fake broker. See README.md for the four operations and the
crash/hang control semantics. Wire mechanics (framing, duplicate-key/
non-finite rejection, ProtocolChannel) are copied from the canonical
stdlib-only worker model, examples/session-notebook/plugin.py.
"""
from __future__ import annotations

import importlib.util
import json
import os
import queue
import sys
import threading
import time
import uuid
from typing import Any


PLUGIN_ID = "org.example.job-recovery"
PLUGIN_VERSION = "1.0.0"
FRAME_MAX_BYTES = 1_048_576

START_COUNT = "org.example.job-recovery.count.start"
RESUME_COUNT = "org.example.job-recovery.count.start.resume"
START_STREAM = "org.example.job-recovery.stream.start"
CONTROL = "org.example.job-recovery.control"
_JOB_ECHO_OPERATIONS = frozenset({START_COUNT, RESUME_COUNT, START_STREAM})

CHECKPOINT_SCHEMA_ID = "schemas/count.checkpoint.schema.json"
_STEP_SLEEP_SECONDS = 0.01
_CONTROL_MODES = frozenset({"normal", "crash", "hang"})

JOBS_CREATE = "plugin.v1.broker.jobs.create"
JOBS_CHECKPOINT = "plugin.v1.broker.jobs.checkpoint"
JOBS_COMPLETE = "plugin.v1.broker.jobs.complete"
JOBS_FAIL = "plugin.v1.broker.jobs.fail"
_JOBS_METHODS = frozenset({JOBS_CREATE, JOBS_CHECKPOINT, JOBS_COMPLETE, JOBS_FAIL})

INVENTORY_HELLO = "plugin.v1.hello"
INVENTORY_ACTIVATE = "plugin.v1.activate"
INVENTORY_INVOKE = "plugin.v1.invoke"
INVENTORY_CANCEL = "plugin.v1.cancel"
INVENTORY_DRAIN = "plugin.v1.drain"
INVENTORY_DEACTIVATE = "plugin.v1.deactivate"
INVENTORY_HEARTBEAT = "plugin.v1.heartbeat"

RUNTIME_ALIASES = {
    "plugin.v1.lifecycle.hello": INVENTORY_HELLO,
    "plugin.v1.lifecycle.activate": INVENTORY_ACTIVATE,
    "plugin.v1.lifecycle.drain": INVENTORY_DRAIN,
}


class ProtocolFailure(RuntimeError):
    """The stdio channel can no longer exchange valid frames."""


class JobRecoveryInputError(ValueError):
    """A request from the host does not match this plugin's wire contract."""


class BrokerCallError(RuntimeError):
    """A broker request failed, or the broker's own response was malformed."""


def _reject_non_finite(_value: str) -> None:
    raise ProtocolFailure("invalid JSON frame")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, value in pairs:
        if key in decoded:
            raise ProtocolFailure("invalid JSON frame")
        decoded[key] = value
    return decoded


def _validate_json_value(
    value: Any,
    *,
    depth: int = 0,
    nodes: list[int] | None = None,
) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if depth > 64 or nodes[0] > 200_000:
        raise JobRecoveryInputError()
    if value is None or type(value) is bool or type(value) is int:
        return
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise JobRecoveryInputError()
        return
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise JobRecoveryInputError() from None
        return
    if type(value) is list:
        for child in value:
            _validate_json_value(child, depth=depth + 1, nodes=nodes)
        return
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                raise JobRecoveryInputError()
            try:
                key.encode("utf-8")
            except UnicodeEncodeError:
                raise JobRecoveryInputError() from None
            _validate_json_value(child, depth=depth + 1, nodes=nodes)
        return
    raise JobRecoveryInputError()


def _parse_frame(line: bytes) -> dict[str, Any]:
    if not line.endswith(b"\n") or len(line) > FRAME_MAX_BYTES + 1:
        raise ProtocolFailure("invalid frame")
    payload = line[:-1]
    if payload.endswith(b"\r"):
        payload = payload[:-1]
    if not payload:
        raise ProtocolFailure("invalid frame")
    try:
        text = payload.decode("utf-8")
        decoded = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except ProtocolFailure:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ProtocolFailure("invalid JSON frame") from None
    if type(decoded) is not dict:
        raise ProtocolFailure("invalid JSON frame")
    try:
        _validate_json_value(decoded)
    except JobRecoveryInputError:
        raise ProtocolFailure("invalid JSON frame") from None
    return decoded


def _encode_frame(payload: dict[str, Any]) -> bytes:
    try:
        _validate_json_value(payload)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    except (JobRecoveryInputError, TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise ProtocolFailure("response could not be encoded") from None
    if len(encoded) > FRAME_MAX_BYTES + 1:
        raise ProtocolFailure("response exceeds frame limit")
    return encoded


def _require_exact_object(
    value: Any,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if type(value) is not dict:
        raise JobRecoveryInputError()
    if not required.issubset(value) or set(value) - required - optional:
        raise JobRecoveryInputError()
    return value


def _require_text(value: Any, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise JobRecoveryInputError()
    return value


def _require_uuid(value: Any, on_error: type[Exception]) -> str:
    """Validate a UUID string, raising on_error() (caller picks whose fault it is)."""
    if type(value) is not str:
        raise on_error()
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise on_error() from None
    return value


def _require_positive_int(value: Any, *, minimum: int = 1) -> int:
    if type(value) is not int or type(value) is bool or value < minimum:
        raise JobRecoveryInputError()
    return value


def _require_step_count(value: Any) -> int:
    if type(value) is not int or type(value) is bool or not 1 <= value <= 100_000:
        raise JobRecoveryInputError()
    return value


def _require_checkpoint(value: Any) -> dict[str, int]:
    checkpoint = _require_exact_object(value, required=frozenset({"next", "target"}))
    return {
        "next": _require_positive_int(checkpoint["next"]),
        "target": _require_positive_int(checkpoint["target"]),
    }


class ProtocolChannel:
    """Demultiplex stdin into host requests (queued) and broker responses (id-matched)."""

    def __init__(self) -> None:
        self._requests: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._responses: dict[str, dict[str, Any]] = {}
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._failure: ProtocolFailure | None = None
        self._next_broker_request = 0

    def start(self) -> None:
        reader = threading.Thread(
            target=self._read_frames,
            name="job-recovery-stdio",
            daemon=True,
        )
        reader.start()

    def _read_frames(self) -> None:
        try:
            while True:
                line = sys.stdin.buffer.readline(FRAME_MAX_BYTES + 2)
                if not line:
                    raise ProtocolFailure("host channel closed")
                frame = _parse_frame(line)
                if "method" in frame:
                    self._requests.put(frame)
                    continue
                request_id = frame.get("id")
                if type(request_id) is not str or not request_id.startswith("job-recovery-broker-"):
                    raise ProtocolFailure("unexpected response")
                with self._condition:
                    if request_id in self._responses:
                        raise ProtocolFailure("duplicate response")
                    self._responses[request_id] = frame
                    self._condition.notify_all()
        except ProtocolFailure as failure:
            with self._condition:
                self._failure = failure
                self._condition.notify_all()
            self._requests.put(None)

    def receive_request(self) -> dict[str, Any]:
        request = self._requests.get()
        if request is None:
            raise self._failure or ProtocolFailure("host channel closed")
        return request

    def send(self, payload: dict[str, Any]) -> None:
        encoded = _encode_frame(payload)
        with self._write_lock:
            try:
                sys.stdout.buffer.write(encoded)
                sys.stdout.buffer.flush()
            except (BrokenPipeError, OSError, ValueError):
                raise ProtocolFailure("host channel closed") from None

    def call_broker(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._condition:
            self._next_broker_request += 1
            request_id = f"job-recovery-broker-{self._next_broker_request}"
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        with self._condition:
            while request_id not in self._responses:
                if self._failure is not None:
                    raise self._failure
                self._condition.wait()
            response = self._responses.pop(request_id)
        if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
            raise ProtocolFailure("broker response mismatch")
        if ("result" in response) == ("error" in response):
            raise ProtocolFailure("broker response invalid")
        if "error" in response:
            raise BrokerCallError()
        result = response["result"]
        if type(result) is not dict:
            raise ProtocolFailure("broker response invalid")
        return result


class JobRecoveryWorker:
    """Serial operation dispatcher for one activated job-recovery process."""

    def __init__(self, channel: ProtocolChannel) -> None:
        self._channel = channel
        self._state = "created"
        self._activation_id: str | None = None
        self._allowed_broker_methods: frozenset[str] = frozenset()
        self._stopped = False
        self._control_lock = threading.Lock()
        self._control: dict[str, Any] = {"mode": "normal", "after_step": None}
        self._hang_triggered = threading.Event()
        self._counting_threads: dict[str, threading.Thread] = {}

    def serve(self) -> None:
        while not self._stopped:
            request = self._channel.receive_request()
            response = self._response_for(request)
            self._channel.send(response)

    def _response_for(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._hang_triggered.is_set():
            threading.Event().wait()  # hang mode: never answer again; process stays alive
        request_id = request.get("id")
        try:
            if request.get("jsonrpc") != "2.0" or "id" not in request:
                raise JobRecoveryInputError()
            method = request.get("method")
            if type(method) is not str:
                raise JobRecoveryInputError()
            params = request.get("params", {})
            if type(params) is not dict:
                raise JobRecoveryInputError()
            canonical_method = RUNTIME_ALIASES.get(method, method)
            result = self._dispatch(canonical_method, params)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except JobRecoveryInputError:
            return self._error(request_id, -32602, "invalid job-recovery request")
        except BrokerCallError:
            return self._error(request_id, -32000, "job-recovery broker unavailable")
        except Exception:
            return self._error(request_id, -32603, "job-recovery operation failed")

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == INVENTORY_HELLO:
            return self._hello(params)
        if method == INVENTORY_ACTIVATE:
            return self._activate(params)
        if method == INVENTORY_INVOKE:
            return self._invoke(params)
        if method == INVENTORY_HEARTBEAT:
            return self._heartbeat(params)
        if method == INVENTORY_CANCEL:
            return self._cancel(params)
        if method == INVENTORY_DRAIN:
            return self._drain(params)
        if method == INVENTORY_DEACTIVATE:
            return self._deactivate(params)
        raise JobRecoveryInputError()  # unknown lifecycle method: rejected, never fatal

    def _hello(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state != "created":
            raise JobRecoveryInputError()
        hello = _require_exact_object(params, required=frozenset({"offered_api", "nonce"}))
        api = _require_exact_object(hello["offered_api"], required=frozenset({"major", "minor"}))
        if (
            type(api["major"]) is not int
            or api["major"] != 1
            or type(api["minor"]) is not int
            or api["minor"] < 0
        ):
            raise JobRecoveryInputError()
        _require_text(hello["nonce"], 128)
        self._state = "hello_verified"
        return {
            "plugin_id": PLUGIN_ID,
            "plugin_version": PLUGIN_VERSION,
            "capabilities": ["job-recovery.checkpoint-resume", "fixture.isolated-imports"],
        }

    def _activate(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state != "hello_verified":
            raise JobRecoveryInputError()
        activation = _require_exact_object(
            params,
            required=frozenset({"activation_token", "allowed_broker_methods"}),
            optional=frozenset({"config_revision"}),
        )
        token = activation["activation_token"]
        methods = activation["allowed_broker_methods"]
        if type(token) is not str or not token or len(token) > 512:
            raise JobRecoveryInputError()
        if type(methods) is not list or len(methods) > 64:
            raise JobRecoveryInputError()
        if any(type(method) is not str or not method or len(method) > 128 for method in methods):
            raise JobRecoveryInputError()
        if len(methods) != len(set(methods)):
            raise JobRecoveryInputError()
        if "config_revision" in activation and (
            type(activation["config_revision"]) is not int or activation["config_revision"] < 0
        ):
            raise JobRecoveryInputError()
        self._allowed_broker_methods = frozenset(methods)
        self._activation_id = str(uuid.uuid4())
        self._state = "active"
        with self._control_lock:  # control is test-only, in-memory: every activation starts clean
            self._control = {"mode": "normal", "after_step": None}
        self._hang_triggered = threading.Event()
        return {"activation_id": self._activation_id, "invocation_handle_prefix": "job-recovery:"}

    def _invoke(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state != "active" or self._activation_id is None:
            raise JobRecoveryInputError()
        invocation = _require_exact_object(
            params,
            required=frozenset({"operation_id", "input", "broker_context"}),
        )
        operation_id = _require_text(invocation["operation_id"], 256)
        invocation_input = invocation["input"]
        _validate_json_value(invocation_input)
        broker_context = _require_exact_object(
            invocation["broker_context"],
            required=frozenset(
                {"activation_id", "plugin_id", "invocation_handle", "revocation_generation"}
            ),
        )
        if (
            broker_context["activation_id"] != self._activation_id
            or broker_context["plugin_id"] != PLUGIN_ID
            or type(broker_context["invocation_handle"]) is not str
            or not broker_context["invocation_handle"]
            or type(broker_context["revocation_generation"]) is not int
            or broker_context["revocation_generation"] < 0
        ):
            raise JobRecoveryInputError()
        output = self._invoke_operation(
            operation_id, invocation_input, broker_context["invocation_handle"]
        )
        # Job-creating operations echo job_id at the top level so the host
        # can read it generically, without knowing this plugin's output shape.
        result: dict[str, Any] = {"output": output}
        if operation_id in _JOB_ECHO_OPERATIONS:
            job_id = output.get("job_id")
            if type(job_id) is str and job_id:
                result["job_id"] = job_id
        return result

    def _invoke_operation(
        self, operation_id: str, invocation_input: Any, invocation_handle: str
    ) -> dict[str, Any]:
        if operation_id in (START_COUNT, START_STREAM):
            return self._start_job(
                operation_id, invocation_input, invocation_handle, use_checkpoint=operation_id == START_COUNT
            )
        if operation_id == RESUME_COUNT:
            return self._resume_count(invocation_input)
        if operation_id == CONTROL:
            return self._set_control(invocation_input)
        raise JobRecoveryInputError()

    def _call_jobs(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method not in _JOBS_METHODS:
            raise JobRecoveryInputError()
        if method not in self._allowed_broker_methods:
            raise BrokerCallError()
        return self._channel.call_broker(method, params)

    def _start_job(
        self, operation_id: str, invocation_input: Any, invocation_handle: str, *, use_checkpoint: bool
    ) -> dict[str, Any]:
        """Back count.start (use_checkpoint) and stream.start (not): same shape, jobs.create then a background counting thread."""
        values = _require_exact_object(invocation_input, required=frozenset({"steps"}))
        steps = _require_step_count(values["steps"])
        create_params = {"invocation_handle": invocation_handle, "operation_id": operation_id}
        if use_checkpoint:
            create_params["checkpoint_schema_id"] = CHECKPOINT_SCHEMA_ID
        created = self._call_jobs(JOBS_CREATE, create_params)
        job_id = _require_uuid(created.get("job_id"), BrokerCallError)
        self._spawn_counting(job_id, start_at=1, target=steps, use_checkpoint=use_checkpoint, expected_revision=None)
        return {"job_id": job_id, "state": "running"}

    def _resume_count(self, invocation_input: Any) -> dict[str, Any]:
        """The resume convention: continue an existing job from its checkpoint (no jobs.create)."""
        values = _require_exact_object(
            invocation_input,
            required=frozenset({"job_id", "checkpoint_revision", "checkpoint_schema_id", "checkpoint"}),
        )
        job_id = _require_uuid(values["job_id"], JobRecoveryInputError)
        checkpoint_revision = _require_positive_int(values["checkpoint_revision"])
        if values["checkpoint_schema_id"] != CHECKPOINT_SCHEMA_ID:
            raise JobRecoveryInputError()
        checkpoint = _require_checkpoint(values["checkpoint"])
        self._spawn_counting(
            job_id,
            start_at=checkpoint["next"],
            target=checkpoint["target"],
            use_checkpoint=True,
            expected_revision=checkpoint_revision,
        )
        return {"job_id": job_id, "state": "running"}

    def _set_control(self, invocation_input: Any) -> dict[str, Any]:
        values = _require_exact_object(
            invocation_input, required=frozenset({"mode"}), optional=frozenset({"after_step"})
        )
        mode = values["mode"]
        if mode not in _CONTROL_MODES:
            raise JobRecoveryInputError()
        after_step = values.get("after_step")
        if after_step is not None:
            after_step = _require_positive_int(after_step)
        if mode in ("crash", "hang") and after_step is None:
            raise JobRecoveryInputError()
        with self._control_lock:
            self._control = {"mode": mode, "after_step": after_step}
        return {"applied": True}

    def _spawn_counting(
        self,
        job_id: str,
        *,
        start_at: int,
        target: int,
        use_checkpoint: bool,
        expected_revision: int | None,
    ) -> None:
        thread = threading.Thread(
            target=self._run_counting,
            args=(job_id, start_at, target, use_checkpoint, expected_revision),
            name=f"job-recovery-count-{job_id}",
            daemon=True,
        )
        self._counting_threads[job_id] = thread
        thread.start()

    def _run_counting(
        self,
        job_id: str,
        start_at: int,
        target: int,
        use_checkpoint: bool,
        expected_revision: int | None,
    ) -> None:
        """Perform steps start_at..target, checkpointing after every step when asked."""
        try:
            performed = 0
            for step in range(start_at, target + 1):
                time.sleep(_STEP_SLEEP_SECONDS)
                performed += 1
                if use_checkpoint:
                    result = self._call_jobs(
                        JOBS_CHECKPOINT,
                        {
                            "job_id": job_id,
                            "checkpoint": {"next": step + 1, "target": target},
                            "expected_revision": expected_revision,
                            # A job created with a declared checkpoint schema
                            # must name it on every save; the engine refuses an
                            # unlabelled checkpoint it would have to store
                            # unvalidated.
                            "schema_id": CHECKPOINT_SCHEMA_ID,
                        },
                    )
                    revision = result.get("revision")
                    if type(revision) is not int or type(revision) is bool or revision < 0:
                        raise BrokerCallError()
                    expected_revision = revision
                if self._maybe_trigger_control(step):
                    return  # hang mode: stop quietly, leave the job running
            self._complete_counting(job_id, performed, target)
        except JobRecoveryInputError:
            self._fail_counting(job_id, "invalid_argument")
        except BrokerCallError:
            self._fail_counting(job_id, "plugin_unavailable")
        except Exception:
            self._fail_counting(job_id, "internal")
        finally:
            self._counting_threads.pop(job_id, None)

    def _maybe_trigger_control(self, step: int) -> bool:
        """Test-only fault injection. True means hang triggered (stop quietly); crash never returns."""
        with self._control_lock:
            control = dict(self._control)
        if control["after_step"] != step:
            return False
        if control["mode"] == "crash":
            os._exit(1)
        if control["mode"] == "hang":
            self._hang_triggered.set()
            return True
        return False

    def _complete_counting(self, job_id: str, performed: int, target: int) -> None:
        self._call_jobs(
            JOBS_COMPLETE,
            {
                "job_id": job_id,
                "output": {"steps_performed_by_this_activation": performed, "final": target},
            },
        )

    def _fail_counting(self, job_id: str, code: str) -> None:
        try:
            self._call_jobs(JOBS_FAIL, {"job_id": job_id, "error": {"code": code, "retryable": False}})
        except BrokerCallError:
            pass

    def _heartbeat(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state != "active" or self._activation_id is None:
            raise JobRecoveryInputError()
        heartbeat = _require_exact_object(params, required=frozenset({"activation_id"}))
        if heartbeat["activation_id"] != self._activation_id:
            raise JobRecoveryInputError()
        return {"alive": True}

    def _cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state != "active":
            raise JobRecoveryInputError()
        _require_exact_object(params, required=frozenset({"job_id"}))
        _require_uuid(params["job_id"], JobRecoveryInputError)
        return {"accepted": False}

    def _drain(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state != "active":
            raise JobRecoveryInputError()
        drain = _require_exact_object(params, required=frozenset({"deadline_ms"}))
        deadline_ms = drain["deadline_ms"]
        if type(deadline_ms) is not int or not 1 <= deadline_ms <= 60_000:
            raise JobRecoveryInputError()
        self._state = "inactive"
        return {"drained": True}

    def _deactivate(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state not in {"active", "inactive"}:
            raise JobRecoveryInputError()
        _require_exact_object(params, required=frozenset())
        self._state = "deactivated"
        self._stopped = True
        return {"deactivated": True}


def main() -> None:
    if not sys.flags.isolated or importlib.util.find_spec("model_deck") is not None:
        raise RuntimeError("Job Recovery fixture requires isolated standard-library imports")
    channel = ProtocolChannel()
    channel.start()
    JobRecoveryWorker(channel).serve()


if __name__ == "__main__":
    main()
