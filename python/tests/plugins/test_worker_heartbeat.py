"""Worker liveness tests for the bounded process runtime (U12).

Every child here is a synthetic owned fixture (``sys.executable -c``) that
speaks ``plugin.v1`` lifecycle JSON-RPC over stdio. No shell, no downloads, no
discovery, no live app process. The runtime under test is asked to tell a dead
worker apart from a live but unresponsive one, and to report each loss exactly
once.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import tempfile
import threading
import time
import unittest

from model_deck.plugins.lifecycle_session import LifecycleSession
from model_deck.plugins.process_runtime import (
    ProcessRuntime,
    ProcessRuntimeConfig,
    ProcessRuntimeError,
)
from model_deck.plugins.process_runtime.health import (
    WorkerHealthState,
    WorkerLossCode,
)

PLUGIN_ID = "org.example.notebook"
PLUGIN_VERSION = "1.0.0"
TOKEN = "activation-token-opaque-value"
BROKER_METHODS = ("plugin.v1.broker.storage.get",)
ACTIVATION_ID = "11111111-2222-4333-8444-555555555555"
OUTER_TIMEOUT_S = 30.0
LOSS_WAIT_S = 15.0

_CHILD_SOURCE = """
import json
import sys
import time

ACTIVATION_ID = %(activation_id)r
PLUGIN_ID = %(plugin_id)r
PLUGIN_VERSION = %(plugin_version)r
BEAT_LOG_PATH = %(beat_log_path)r
HEARTBEAT_MODE = %(heartbeat_mode)r
HEARTBEAT_DELAY_S = %(heartbeat_delay_s)r
AFTER_ACTIVATE = %(after_activate)r


def send(payload):
    sys.stdout.write(json.dumps(payload) + chr(10))
    sys.stdout.flush()


def note_beat(params):
    if not BEAT_LOG_PATH:
        return
    with open(BEAT_LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(params) + chr(10))


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    request = json.loads(line)
    method = request.get("method", "")
    request_id = request.get("id")
    if method.endswith("hello"):
        send({"jsonrpc": "2.0", "id": request_id,
              "result": {"plugin_id": PLUGIN_ID,
                         "plugin_version": PLUGIN_VERSION,
                         "capabilities": []}})
        continue
    if method.endswith("activate"):
        send({"jsonrpc": "2.0", "id": request_id,
              "result": {"activation_id": ACTIVATION_ID,
                         "invocation_handle_prefix": "act-7:"}})
        if AFTER_ACTIVATE == "exit_zero":
            time.sleep(0.5)
            sys.exit(0)
        if AFTER_ACTIVATE == "stop_reading":
            time.sleep(30)
            sys.exit(0)
        continue
    if method == "plugin.v1.heartbeat":
        note_beat(request.get("params"))
        if HEARTBEAT_MODE == "ignore":
            continue
        if HEARTBEAT_MODE == "method_not_found":
            send({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32601, "message": "method not found"}})
            continue
        if HEARTBEAT_DELAY_S > 0:
            time.sleep(HEARTBEAT_DELAY_S)
        send({"jsonrpc": "2.0", "id": request_id, "result": {"alive": True}})
        continue
    send({"jsonrpc": "2.0", "id": request_id, "result": {}})
"""


def _child_source(**overrides) -> str:
    values = {
        "activation_id": ACTIVATION_ID,
        "plugin_id": PLUGIN_ID,
        "plugin_version": PLUGIN_VERSION,
        "beat_log_path": "",
        "heartbeat_mode": "answer",
        "heartbeat_delay_s": 0.0,
        "after_activate": "",
    }
    values.update(overrides)
    return _CHILD_SOURCE % values


def _session() -> LifecycleSession:
    return LifecycleSession(
        expected_plugin_id=PLUGIN_ID,
        expected_plugin_version=PLUGIN_VERSION,
        offered_api_major=1,
        offered_api_minor=0,
        activation_token=TOKEN,
        allowed_broker_methods=BROKER_METHODS,
    )


class _RecordingListener:
    """Collects worker-loss events and lets a test wait for the first one."""

    def __init__(self) -> None:
        self.events: list = []
        self.received = threading.Event()

    def on_worker_lost(self, event) -> None:
        self.events.append(event)
        self.received.set()


class _RaisingListener(_RecordingListener):
    """A badly behaved listener: it records, then raises."""

    def on_worker_lost(self, event) -> None:
        super().on_worker_lost(event)
        raise RuntimeError("listener exploded")


class WorkerLivenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._package_dirs: list[str] = []

    def tearDown(self) -> None:
        for path in self._package_dirs:
            shutil.rmtree(path, ignore_errors=True)

    def _package_dir(self) -> str:
        path = tempfile.mkdtemp(prefix="md-u12-hb-")
        self._package_dirs.append(path)
        return path

    def _runtime(self, child_source: str, listener=None, **config_overrides):
        import sys

        settings = {
            "argv": (sys.executable, "-c", child_source),
            "package_dir": self._package_dir(),
            "timeout_s": 5.0,
        }
        settings.update(config_overrides)
        runtime = ProcessRuntime(
            ProcessRuntimeConfig(**settings), worker_loss_listener=listener
        )
        self.addCleanup(runtime.close)
        timer = threading.Timer(OUTER_TIMEOUT_S, runtime.close)
        timer.daemon = True
        timer.start()
        self.addCleanup(timer.cancel)
        return runtime

    def _activate(self, runtime: ProcessRuntime) -> LifecycleSession:
        runtime.spawn()
        session = _session()
        runtime.run_hello(session, "nonce-liveness")
        activation = runtime.run_activation(session)
        self.assertEqual(activation["activation_id"], ACTIVATION_ID)
        return session

    @staticmethod
    def _read_beats(package_dir: str) -> list:
        path = os.path.join(package_dir, "beats.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_health_starts_out_as_starting(self) -> None:
        runtime = self._runtime(_child_source())
        health = runtime.health()
        self.assertIs(health.state, WorkerHealthState.STARTING)
        self.assertIsNone(health.last_heartbeat_monotonic)
        self.assertEqual(health.consecutive_missed, 0)
        self.assertTrue(runtime.heartbeat_supported)

    def test_child_exiting_after_activation_is_reported_as_exited(self) -> None:
        listener = _RecordingListener()
        runtime = self._runtime(
            _child_source(after_activate="exit_zero"), listener=listener
        )
        self._activate(runtime)
        self.assertTrue(listener.received.wait(LOSS_WAIT_S))
        self.assertEqual(len(listener.events), 1)
        event = listener.events[0]
        self.assertIs(event.failure_code, WorkerLossCode.EXITED)
        self.assertEqual(event.exit_code, 0)
        self.assertEqual(event.activation_id, ACTIVATION_ID)
        self.assertIs(runtime.health().state, WorkerHealthState.DEAD)
        self.assertIsNone(runtime._proc)

    def test_sigkilled_child_is_reported_as_exited_with_the_signal(self) -> None:
        listener = _RecordingListener()
        runtime = self._runtime(_child_source(), listener=listener)
        self._activate(runtime)
        proc = runtime._proc
        assert proc is not None
        os.kill(proc.pid, signal.SIGKILL)
        self.assertTrue(listener.received.wait(LOSS_WAIT_S))
        self.assertEqual(len(listener.events), 1)
        event = listener.events[0]
        self.assertIs(event.failure_code, WorkerLossCode.EXITED)
        self.assertEqual(event.exit_code, -signal.SIGKILL)
        self.assertIs(runtime.health().state, WorkerHealthState.DEAD)

    def test_child_ignoring_every_frame_is_declared_unresponsive(self) -> None:
        listener = _RecordingListener()
        runtime = self._runtime(
            _child_source(after_activate="stop_reading"),
            listener=listener,
            heartbeat_interval_s=0.2,
            heartbeat_timeout_s=0.3,
            heartbeat_max_missed=2,
        )
        self._activate(runtime)
        proc = runtime._proc
        assert proc is not None
        self.assertTrue(listener.received.wait(LOSS_WAIT_S))
        self.assertEqual(len(listener.events), 1)
        self.assertIs(listener.events[0].failure_code, WorkerLossCode.UNRESPONSIVE)
        self.assertEqual(listener.events[0].activation_id, ACTIVATION_ID)
        health = runtime.health()
        self.assertIs(health.state, WorkerHealthState.UNRESPONSIVE)
        self.assertGreaterEqual(health.consecutive_missed, 2)
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(runtime._proc)

    def test_child_answering_beats_stays_healthy_across_intervals(self) -> None:
        listener = _RecordingListener()
        package_dir = self._package_dir()
        runtime = self._runtime(
            _child_source(beat_log_path=os.path.join(package_dir, "beats.log")),
            listener=listener,
            package_dir=package_dir,
            heartbeat_interval_s=0.15,
            heartbeat_timeout_s=1.0,
            heartbeat_max_missed=2,
        )
        self._activate(runtime)
        time.sleep(0.9)
        health = runtime.health()
        self.assertIs(health.state, WorkerHealthState.HEALTHY)
        self.assertEqual(health.consecutive_missed, 0)
        self.assertIsNotNone(health.last_heartbeat_monotonic)
        self.assertTrue(runtime.heartbeat_supported)
        self.assertEqual(listener.events, [])
        self.assertIsNotNone(runtime._proc)
        beats = self._read_beats(package_dir)
        self.assertGreaterEqual(len(beats), 3)
        for beat in beats:
            self.assertEqual(beat, {"activation_id": ACTIVATION_ID})

    def test_owner_close_never_reports_a_loss(self) -> None:
        listener = _RecordingListener()
        runtime = self._runtime(_child_source(), listener=listener)
        self._activate(runtime)
        runtime.close()
        self.assertFalse(listener.received.wait(0.5))
        self.assertEqual(listener.events, [])
        self.assertIs(runtime.health().state, WorkerHealthState.DEAD)
        self.assertIsNone(runtime.last_listener_failure)

    def test_listener_that_raises_does_not_break_the_stop_path(self) -> None:
        listener = _RaisingListener()
        runtime = self._runtime(_child_source(), listener=listener)
        self._activate(runtime)
        proc = runtime._proc
        assert proc is not None
        os.kill(proc.pid, signal.SIGKILL)
        self.assertTrue(listener.received.wait(LOSS_WAIT_S))
        time.sleep(0.2)
        self.assertEqual(len(listener.events), 1)
        self.assertIsNone(runtime._proc)
        self.assertIsNotNone(proc.poll())
        failure = runtime.last_listener_failure
        self.assertIsNotNone(failure)
        self.assertEqual(failure.code, "transport")
        self.assertIsNotNone(runtime.last_failure)
        runtime.close()
        self.assertEqual(len(listener.events), 1)

    def test_method_not_found_marks_heartbeat_unsupported_without_loss(self) -> None:
        listener = _RecordingListener()
        package_dir = self._package_dir()
        runtime = self._runtime(
            _child_source(
                heartbeat_mode="method_not_found",
                beat_log_path=os.path.join(package_dir, "beats.log"),
            ),
            listener=listener,
            package_dir=package_dir,
            heartbeat_interval_s=0.15,
            heartbeat_timeout_s=1.0,
            heartbeat_max_missed=2,
        )
        self._activate(runtime)
        time.sleep(0.9)
        self.assertFalse(runtime.heartbeat_supported)
        health = runtime.health()
        self.assertIs(health.state, WorkerHealthState.HEALTHY)
        self.assertEqual(health.consecutive_missed, 0)
        self.assertEqual(listener.events, [])
        self.assertIsNotNone(runtime._proc)
        self.assertEqual(len(self._read_beats(package_dir)), 1)

    def test_late_heartbeat_reply_is_dropped_without_failing_the_runtime(self) -> None:
        listener = _RecordingListener()
        runtime = self._runtime(
            _child_source(heartbeat_delay_s=0.4),
            listener=listener,
            heartbeat_interval_s=0.3,
            heartbeat_timeout_s=0.15,
            heartbeat_max_missed=20,
        )
        self._activate(runtime)
        time.sleep(1.4)
        self.assertEqual(listener.events, [])
        self.assertIsNotNone(runtime._proc)
        self.assertIsNone(runtime.last_failure)
        health = runtime.health()
        self.assertIs(health.state, WorkerHealthState.UNRESPONSIVE)
        self.assertGreaterEqual(health.consecutive_missed, 2)

    def test_zero_interval_disables_heartbeats(self) -> None:
        listener = _RecordingListener()
        package_dir = self._package_dir()
        runtime = self._runtime(
            _child_source(beat_log_path=os.path.join(package_dir, "beats.log")),
            listener=listener,
            package_dir=package_dir,
            heartbeat_interval_s=0.0,
        )
        self._activate(runtime)
        time.sleep(0.5)
        self.assertEqual(self._read_beats(package_dir), [])
        self.assertIs(runtime.health().state, WorkerHealthState.STARTING)
        self.assertEqual(listener.events, [])


class HeartbeatConfigurationTests(unittest.TestCase):
    def _config(self, **overrides) -> ProcessRuntimeConfig:
        import sys

        settings = {"argv": (sys.executable, "-c", "pass"), "package_dir": "/tmp"}
        settings.update(overrides)
        return ProcessRuntimeConfig(**settings)

    def test_heartbeat_fields_are_validated(self) -> None:
        for overrides in (
            {"heartbeat_interval_s": -1.0},
            {"heartbeat_interval_s": 61.0},
            {"heartbeat_interval_s": "5"},
            {"heartbeat_interval_s": True},
            {"heartbeat_timeout_s": 0.0},
            {"heartbeat_timeout_s": 61.0},
            {"heartbeat_max_missed": 0},
            {"heartbeat_max_missed": 2048},
            {"heartbeat_max_missed": 1.5},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ProcessRuntimeError):
                    self._config(**overrides)

    def test_heartbeat_defaults_and_disabled_interval_are_accepted(self) -> None:
        default = self._config()
        self.assertEqual(default.heartbeat_interval_s, 5.0)
        self.assertEqual(default.heartbeat_timeout_s, 2.0)
        self.assertEqual(default.heartbeat_max_missed, 3)
        self.assertEqual(self._config(heartbeat_interval_s=0.0).heartbeat_interval_s, 0.0)

    def test_worker_loss_listener_must_implement_on_worker_lost(self) -> None:
        with self.assertRaises(ProcessRuntimeError):
            ProcessRuntime(self._config(), worker_loss_listener=object())
        runtime = ProcessRuntime(self._config(), worker_loss_listener=None)
        self.assertIsNone(runtime.last_failure)


if __name__ == "__main__":
    unittest.main()
