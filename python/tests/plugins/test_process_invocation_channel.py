"""Standalone subprocess tests for generic invocation and worker broker requests."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping
from typing import Any

from model_deck.plugins.lifecycle_session import LifecycleSession
from model_deck.plugins.process_runtime.invocation_channel import InvocationChannel
from model_deck.plugins.process_runtime.runtime import ProcessRuntime, ProcessRuntimeConfig
from model_deck.plugins.process_runtime.errors import ProcessRuntimeError


PLUGIN_ID = "org.example.notebook"
PLUGIN_VERSION = "1.0.0"
ACTIVATION_ID = "11111111-2222-4333-8444-555555555555"
STORAGE_GET = "plugin.v1.broker.storage.get"
STORAGE_DELETE = "plugin.v1.broker.storage.delete"
OUTER_TIMEOUT_S = 10.0


def _child_code(
    *,
    broker_methods: tuple[str, ...] = (STORAGE_GET,),
    duplicate_request_id: bool = False,
    invalid_broker_params: bool = False,
    invalid_invoke_result: bool = False,
    unsolicited_broker_requests: int = 0,
) -> str:
    configuration = json.dumps(
        {
            "activation_id": ACTIVATION_ID,
            "plugin_id": PLUGIN_ID,
            "plugin_version": PLUGIN_VERSION,
            "broker_methods": broker_methods,
            "duplicate_request_id": duplicate_request_id,
            "invalid_broker_params": invalid_broker_params,
            "invalid_invoke_result": invalid_invoke_result,
            "unsolicited_broker_requests": unsolicited_broker_requests,
        }
    )
    return f"""
import json
import sys

configuration = json.loads({configuration!r})

def send(payload):
    sys.stdout.write(json.dumps(payload, separators=(\",\", \":\")) + \"\\n\")
    sys.stdout.flush()

for line in sys.stdin:
    request = json.loads(line)
    method = request.get(\"method\", \"\")
    request_id = request.get(\"id\")
    if method.endswith(\"hello\"):
        result = {{
            \"plugin_id\": configuration[\"plugin_id\"],
            \"plugin_version\": configuration[\"plugin_version\"],
            \"capabilities\": [],
        }}
        send({{\"jsonrpc\": \"2.0\", \"id\": request_id, \"result\": result}})
        continue
    if method.endswith(\"activate\"):
        result = {{
            \"activation_id\": configuration[\"activation_id\"],
            \"invocation_handle_prefix\": \"worker:\",
        }}
        send({{\"jsonrpc\": \"2.0\", \"id\": request_id, \"result\": result}})
        if configuration[\"unsolicited_broker_requests\"]:
            import time
            time.sleep(0.1)
            for index in range(configuration[\"unsolicited_broker_requests\"]):
                send({{
                    \"jsonrpc\": \"2.0\",
                    \"id\": 8001 + index,
                    \"method\": \"plugin.v1.broker.storage.get\",
                    \"params\": {{
                        \"invocation_handle\": \"trusted:unsolicited\",
                        \"namespace\": configuration[\"plugin_id\"],
                        \"key\": \"copied\",
                    }},
                }})
        continue
    if method == \"plugin.v1.invoke\":
        context = request[\"params\"][\"broker_context\"]
        request_ids = []
        for index, broker_method in enumerate(configuration[\"broker_methods\"]):
            broker_request_id = 7001 if configuration[\"duplicate_request_id\"] else 7001 + index
            request_ids.append(broker_request_id)
            broker_params = {{
                \"invocation_handle\": context[\"invocation_handle\"],
                \"namespace\": configuration[\"plugin_id\"],
                \"key\": \"copied\",
            }}
            if broker_method.endswith(\"delete\"):
                broker_params = {{
                    \"invocation_handle\": context[\"invocation_handle\"],
                    \"namespace\": configuration[\"plugin_id\"],
                    \"key\": \"copied\",
                    \"expected_revision\": 1,
                }}
            if configuration[\"invalid_broker_params\"]:
                broker_params.pop(\"key\", None)
            send({{
                \"jsonrpc\": \"2.0\",
                \"id\": broker_request_id,
                \"method\": broker_method,
                \"params\": broker_params,
            }})
        broker_results = []
        for ignored in request_ids:
            broker_results.append(json.loads(sys.stdin.readline()))
        if configuration[\"invalid_invoke_result\"]:
            result = {{\"unexpected\": True}}
        else:
            result = {{
                \"output\": {{
                    \"input\": request[\"params\"][\"input\"],
                    \"broker_results\": broker_results,
                }}
            }}
        send({{\"jsonrpc\": \"2.0\", \"id\": request_id, \"result\": result}})
"""


def _session(allowed_methods: tuple[str, ...]) -> LifecycleSession:
    return LifecycleSession(
        expected_plugin_id=PLUGIN_ID,
        expected_plugin_version=PLUGIN_VERSION,
        offered_api_major=1,
        offered_api_minor=0,
        activation_token="fixture-token",
        allowed_broker_methods=allowed_methods,
    )


def _broker_context(**overrides: Any) -> dict[str, Any]:
    context = {
        "activation_id": ACTIVATION_ID,
        "plugin_id": PLUGIN_ID,
        "invocation_handle": "trusted:00000001",
        "revocation_generation": 3,
    }
    context.update(overrides)
    return context


def _runtime(
    child_code: str,
    *,
    allowed_methods: tuple[str, ...] = (STORAGE_GET,),
    broker_request_handler=None,
    max_pending_requests: int = 16,
    max_frames: int = 16,
    timeout_s: float = 2.0,
) -> ProcessRuntime:
    package_dir = tempfile.mkdtemp(prefix="model-deck-invocation-")
    return ProcessRuntime(
        ProcessRuntimeConfig(
            argv=(sys.executable, "-c", child_code),
            package_dir=package_dir,
            timeout_s=timeout_s,
            max_pending_requests=max_pending_requests,
            max_frames=max_frames,
        ),
        allowed_broker_methods=allowed_methods,
        broker_request_handler=broker_request_handler,
    )


def _activate(runtime: ProcessRuntime, allowed_methods: tuple[str, ...]) -> LifecycleSession:
    runtime.spawn()
    session = _session(allowed_methods)
    runtime.run_hello(session, "nonce")
    runtime.run_activation(session)
    return session


class _Watchdog:
    def __init__(self, runtime: ProcessRuntime) -> None:
        self._timer = threading.Timer(OUTER_TIMEOUT_S, runtime.close)
        self._timer.daemon = True

    def __enter__(self):
        self._timer.start()
        return self

    def __exit__(self, *exc):
        self._timer.cancel()
        return False


class ProcessInvocationChannelTests(unittest.TestCase):
    def test_invoke_round_trip_services_worker_broker_request(self) -> None:
        calls: list[tuple[str, str, dict[str, Any]]] = []
        handler_lock_observations: list[bool] = []
        runtime: ProcessRuntime

        def handle_broker_request(
            activation_id: str, method: str, params: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            write_lock_was_free = runtime._write_lock.acquire(blocking=False)
            handler_lock_observations.append(write_lock_was_free)
            if write_lock_was_free:
                runtime._write_lock.release()
            self.assertEqual(runtime.invocation_channel().activation_id, ACTIVATION_ID)
            calls.append((activation_id, method, copy.deepcopy(dict(params))))
            return {"value": {"copied": True}, "revision": 7}

        runtime = _runtime(
            _child_code(), broker_request_handler=handle_broker_request
        )
        self.addCleanup(runtime.close)
        with _Watchdog(runtime):
            session = _activate(runtime, (STORAGE_GET,))
            channel = runtime.invocation_channel()
            self.assertIsInstance(channel, InvocationChannel)
            self.assertEqual(channel.activation_id, ACTIVATION_ID)
            result = channel.invoke(
                "org.example.notebook.copy",
                {"source": "fixture"},
                _broker_context(),
                timeout_s=2.0,
            )

        self.assertEqual(result["output"]["input"], {"source": "fixture"})
        broker_response = result["output"]["broker_results"][0]
        self.assertEqual(broker_response["result"], {"value": {"copied": True}, "revision": 7})
        self.assertEqual(calls[0][0], ACTIVATION_ID)
        self.assertEqual(calls[0][1], STORAGE_GET)
        self.assertEqual(calls[0][2]["invocation_handle"], "trusted:00000001")
        self.assertEqual(handler_lock_observations, [True])
        self.assertEqual(session.outstanding, ())

    def test_unlisted_or_missing_handler_fails_closed(self) -> None:
        cases = (
            (_child_code(broker_methods=(STORAGE_DELETE,)), (STORAGE_GET,), lambda *_: {}),
            (
                _child_code(broker_methods=("plugin.v1.broker.storage.drop_all",)),
                (STORAGE_GET,),
                lambda *_: {},
            ),
            (_child_code(), (STORAGE_GET,), None),
        )
        for child_code, allowed_methods, handler in cases:
            with self.subTest(handler=handler):
                runtime = _runtime(
                    child_code,
                    allowed_methods=allowed_methods,
                    broker_request_handler=handler,
                )
                self.addCleanup(runtime.close)
                with _Watchdog(runtime):
                    _activate(runtime, allowed_methods)
                    with self.assertRaises(ProcessRuntimeError) as captured:
                        runtime.invocation_channel().invoke(
                            "org.example.notebook.copy",
                            {},
                            _broker_context(),
                            timeout_s=1.0,
                        )
                self.assertEqual(captured.exception.code, "protocol")

    def test_session_advertisement_cannot_widen_runtime_allowlist(self) -> None:
        handler_calls: list[str] = []

        def handler(_activation_id, method, _params):
            handler_calls.append(method)
            return {"value": None, "revision": 0}

        runtime = _runtime(
            _child_code(),
            allowed_methods=(),
            broker_request_handler=handler,
        )
        self.addCleanup(runtime.close)
        with _Watchdog(runtime):
            _activate(runtime, (STORAGE_GET,))
            with self.assertRaises(ProcessRuntimeError) as captured:
                runtime.invocation_channel().invoke(
                    "org.example.notebook.copy", {}, _broker_context()
                )
        self.assertEqual(captured.exception.code, "protocol")
        self.assertEqual(handler_calls, [])

    def test_invoke_without_broker_requests_needs_no_handler(self) -> None:
        runtime = _runtime(
            _child_code(broker_methods=()),
            allowed_methods=(),
            broker_request_handler=None,
        )
        self.addCleanup(runtime.close)
        with _Watchdog(runtime):
            _activate(runtime, ())
            result = runtime.invocation_channel().invoke(
                "org.example.notebook.copy",
                {"source": "fixture"},
                _broker_context(),
            )
        self.assertEqual(result["output"]["input"], {"source": "fixture"})

    def test_wrong_activation_context_is_rejected_without_echo(self) -> None:
        sentinel = "wrong-activation-secret"
        runtime = _runtime(_child_code(), broker_request_handler=lambda *_: {})
        self.addCleanup(runtime.close)
        with _Watchdog(runtime):
            _activate(runtime, (STORAGE_GET,))
            with self.assertRaises(ProcessRuntimeError) as captured:
                runtime.invocation_channel().invoke(
                    "org.example.notebook.copy",
                    {"sentinel": sentinel},
                    _broker_context(
                        activation_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                        invocation_handle=sentinel,
                    ),
                )
        self.assertEqual(captured.exception.code, "protocol")
        self.assertNotIn(sentinel, str(captured.exception))

    def test_broker_params_and_results_are_schema_validated(self) -> None:
        cases = (
            (_child_code(invalid_broker_params=True), lambda *_: {"value": 1, "revision": 1}),
            (_child_code(), lambda *_: {"value": 1}),
        )
        for child_code, handler in cases:
            with self.subTest(child_code=child_code[-40:]):
                runtime = _runtime(child_code, broker_request_handler=handler)
                self.addCleanup(runtime.close)
                with _Watchdog(runtime):
                    _activate(runtime, (STORAGE_GET,))
                    with self.assertRaises(ProcessRuntimeError) as captured:
                        runtime.invocation_channel().invoke(
                            "org.example.notebook.copy", {}, _broker_context()
                        )
                self.assertEqual(captured.exception.code, "protocol")

    def test_handler_error_does_not_echo_request_or_exception_values(self) -> None:
        sentinel = "handler-private-sentinel"

        def rejecting_handler(*_):
            raise RuntimeError(sentinel)

        runtime = _runtime(_child_code(), broker_request_handler=rejecting_handler)
        self.addCleanup(runtime.close)
        with _Watchdog(runtime):
            _activate(runtime, (STORAGE_GET,))
            with self.assertRaises(ProcessRuntimeError) as captured:
                runtime.invocation_channel().invoke(
                    "org.example.notebook.copy",
                    {"private": sentinel},
                    _broker_context(invocation_handle=sentinel),
                )
        self.assertEqual(captured.exception.code, "protocol")
        self.assertNotIn(sentinel, str(captured.exception))

    def test_duplicate_worker_request_ids_fail_closed(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocking_handler(*_):
            entered.set()
            release.wait(OUTER_TIMEOUT_S)
            return {"value": None, "revision": 0}

        runtime = _runtime(
            _child_code(broker_methods=(STORAGE_GET, STORAGE_GET), duplicate_request_id=True),
            broker_request_handler=blocking_handler,
        )
        self.addCleanup(runtime.close)
        try:
            with _Watchdog(runtime):
                _activate(runtime, (STORAGE_GET,))
                with self.assertRaises(ProcessRuntimeError) as captured:
                    runtime.invocation_channel().invoke(
                        "org.example.notebook.copy", {}, _broker_context()
                    )
            self.assertEqual(captured.exception.code, "id_mismatch")
        finally:
            release.set()

    def test_broker_request_limit_fails_closed(self) -> None:
        release = threading.Event()

        def blocking_handler(*_):
            release.wait(OUTER_TIMEOUT_S)
            return {"value": None, "revision": 0}

        runtime = _runtime(
            _child_code(broker_methods=(STORAGE_GET, STORAGE_GET)),
            broker_request_handler=blocking_handler,
            max_pending_requests=1,
        )
        self.addCleanup(runtime.close)
        try:
            with _Watchdog(runtime):
                _activate(runtime, (STORAGE_GET,))
                with self.assertRaises(ProcessRuntimeError) as captured:
                    runtime.invocation_channel().invoke(
                        "org.example.notebook.copy", {}, _broker_context()
                    )
            self.assertEqual(captured.exception.code, "frame_limit")
        finally:
            release.set()

    def test_timeout_revokes_late_callback_write(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocking_handler(*_):
            entered.set()
            release.wait(OUTER_TIMEOUT_S)
            return {"value": None, "revision": 0}

        runtime = _runtime(_child_code(), broker_request_handler=blocking_handler)
        self.addCleanup(runtime.close)
        try:
            with _Watchdog(runtime):
                _activate(runtime, (STORAGE_GET,))
                with self.assertRaises(ProcessRuntimeError) as captured:
                    runtime.invocation_channel().invoke(
                        "org.example.notebook.copy",
                        {},
                        _broker_context(),
                        timeout_s=0.1,
                    )
                self.assertTrue(entered.wait(1.0))
            self.assertEqual(captured.exception.code, "timeout")
            self.assertIsNone(runtime._proc)
        finally:
            release.set()
            time.sleep(0.05)

    def test_unsolicited_hanging_callbacks_expire_and_reap_child(self) -> None:
        entered = threading.Barrier(3)
        release = threading.Event()

        def blocking_handler(*_):
            entered.wait(OUTER_TIMEOUT_S)
            release.wait(OUTER_TIMEOUT_S)
            return {"value": None, "revision": 0}

        runtime = _runtime(
            _child_code(unsolicited_broker_requests=2),
            broker_request_handler=blocking_handler,
            timeout_s=0.5,
        )
        self.addCleanup(runtime.close)
        owned_process = None
        try:
            with _Watchdog(runtime):
                _activate(runtime, (STORAGE_GET,))
                owned_process = runtime._proc
                entered.wait(OUTER_TIMEOUT_S)
                self.assertTrue(runtime._cleanup_done.wait(3.0))
            self.assertIsNone(runtime._proc)
            self.assertIsNotNone(owned_process)
            self.assertIsNotNone(owned_process.poll())
            self.assertEqual(runtime._broker_request_ids, set())
            self.assertEqual(runtime._broker_request_deadlines, {})
        finally:
            release.set()
            for worker in runtime._broker_threads:
                worker.join(1.0)

    def test_oversized_schema_valid_broker_result_fails_closed(self) -> None:
        runtime = _runtime(
            _child_code(),
            broker_request_handler=lambda *_: {
                "value": "x" * 1_048_576,
                "revision": 1,
            },
        )
        self.addCleanup(runtime.close)
        with _Watchdog(runtime):
            _activate(runtime, (STORAGE_GET,))
            with self.assertRaises(ProcessRuntimeError) as captured:
                runtime.invocation_channel().invoke(
                    "org.example.notebook.copy", {}, _broker_context()
                )
        self.assertEqual(captured.exception.code, "frame_limit")
        self.assertIsNone(runtime._proc)
        self.assertEqual(runtime._broker_request_ids, set())
        self.assertEqual(runtime._broker_request_deadlines, {})

    def test_deactivation_invalidates_channel_and_close_reaps_workers(self) -> None:
        runtime = _runtime(
            _child_code(broker_methods=()),
            broker_request_handler=lambda *_: {"value": None, "revision": 0},
        )
        self.addCleanup(runtime.close)
        with _Watchdog(runtime):
            session = _activate(runtime, (STORAGE_GET,))
            channel = runtime.invocation_channel()
            session.deactivate()
            with self.assertRaises(ProcessRuntimeError):
                channel.invoke(
                    "org.example.notebook.copy", {}, _broker_context(), timeout_s=1.0
                )
        runtime.close()
        self.assertIsNone(runtime._proc)
        self.assertTrue(all(not worker.is_alive() for worker in runtime._broker_threads))

    def test_invalid_invoke_result_fails_closed(self) -> None:
        runtime = _runtime(
            _child_code(broker_methods=(), invalid_invoke_result=True),
            broker_request_handler=lambda *_: {},
        )
        self.addCleanup(runtime.close)
        with _Watchdog(runtime):
            _activate(runtime, (STORAGE_GET,))
            with self.assertRaises(ProcessRuntimeError) as captured:
                runtime.invocation_channel().invoke(
                    "org.example.notebook.copy", {}, _broker_context()
                )
        self.assertEqual(captured.exception.code, "protocol")

    def test_invoke_uses_lifecycle_frame_limit(self) -> None:
        runtime = _runtime(
            _child_code(),
            broker_request_handler=lambda *_: {"value": None, "revision": 0},
            max_frames=1,
        )
        self.addCleanup(runtime.close)
        with _Watchdog(runtime):
            _activate(runtime, (STORAGE_GET,))
            with self.assertRaises(ProcessRuntimeError) as captured:
                runtime.invocation_channel().invoke(
                    "org.example.notebook.copy", {}, _broker_context()
                )
        self.assertEqual(captured.exception.code, "frame_limit")


if __name__ == "__main__":
    unittest.main()
