"""Acceptance tests for the B18 bounded process runtime adapter.

Spawns only synthetic owned fixture children (``sys.executable -c``)
that speak ``plugin.v1`` lifecycle JSON-RPC over stdio. No shell,
no downloads, no global discovery, no live app processes.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import uuid

from model_deck.plugins.lifecycle_session import LifecycleSession
from model_deck.plugins.process_runtime import (
    ProcessRuntime,
    ProcessRuntimeConfig,
    ProcessRuntimeError,
)

PLUGIN_ID = "org.example.notebook"
PLUGIN_VERSION = "1.0.0"
TOKEN = "activation-token-opaque-value"
METHODS = ("plugin.v1.broker.storage.get",)
ACTIVATION_ID = "11111111-2222-4333-8444-555555555555"
OUTER_TIMEOUT_S = 30.0

GOOD_CHILD_LINES = [
    "import json,sys",
    "aid=%r" % ACTIVATION_ID,
    "pid=%r" % PLUGIN_ID,
    "pver=%r" % PLUGIN_VERSION,
    "for line in sys.stdin:",
    "    line=line.strip()",
    "    if not line: continue",
    "    req=json.loads(line)",
    "    m=req.get('method','')",
    "    i=req.get('id')",
    "    r={}",
    "    if m.endswith('hello'): r={'plugin_id':pid,'plugin_version':pver,'capabilities':[]}",
    "    elif m.endswith('activate'): r={'activation_id':aid,'invocation_handle_prefix':'act-7:'}",
    "    elif m.endswith('drain'): r={'drained':True}",
    "    sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':i,'result':r})+chr(10))",
    "    sys.stdout.flush()",
]
GOOD_CHILD = chr(10).join(GOOD_CHILD_LINES) + chr(10)

GARBAGE_CHILD = "import sys" + chr(10) + "sys.stdout.write('not json'+chr(10))" + chr(10) + "sys.stdout.flush()" + chr(10)

WRONG_ID_CHILD_LINES = [
    "import json,sys",
    "line=sys.stdin.readline()",
    "sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':9999,'result':{}})+chr(10))",
    "sys.stdout.flush()",
]
WRONG_ID_CHILD = chr(10).join(WRONG_ID_CHILD_LINES) + chr(10)

NEVER_READS_CHILD = "import time; time.sleep(30)"

STDERR_FLOOD_CHILD_LINES = GOOD_CHILD_LINES[:4] + [
    "import sys as _s",
    "_s.stderr.write('x'*200000)",
    "_s.stderr.flush()",
] + GOOD_CHILD_LINES[4:]
STDERR_FLOOD_CHILD = chr(10).join(STDERR_FLOOD_CHILD_LINES) + chr(10)


def _raw_child(payload: dict) -> str:
    body = json.dumps(payload)
    return (
        "import sys\n"
        "sys.stdin.readline()\n"
        f"sys.stdout.write({body!r}+chr(10))\n"
        "sys.stdout.flush()\n"
        "sys.stdin.read()\n"
    )

BAD_SHAPES = {
    "missing_jsonrpc": {"id": 1, "result": {}},
    "missing_id": {"jsonrpc": "2.0", "result": {}},
    "string_id": {"jsonrpc": "2.0", "id": "1", "result": {}},
    "bool_id": {"jsonrpc": "2.0", "id": True, "result": {}},
    "both_result_error": {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": -1}},
    "neither_result_error": {"jsonrpc": "2.0", "id": 1},
    "non_object_result": {"jsonrpc": "2.0", "id": 1, "result": 42},
    "error_only": {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603}},
}


def _session(**overrides):
    params = {
        "expected_plugin_id": PLUGIN_ID,
        "expected_plugin_version": PLUGIN_VERSION,
        "offered_api_major": 1,
        "offered_api_minor": 0,
        "activation_token": TOKEN,
        "allowed_broker_methods": METHODS,
    }
    params.update(overrides)
    return LifecycleSession(**params)


def _runtime(code: str, **cfg_overrides):
    tmp = tempfile.mkdtemp(prefix="md-b18-procrt-")
    cfg = {
        "argv": (sys.executable, "-c", code),
        "package_dir": tmp,
        "timeout_s": 5.0,
        "max_frames": 16,
        "max_stderr_bytes": 65536,
    }
    cfg.update(cfg_overrides)
    rt = ProcessRuntime(ProcessRuntimeConfig(**cfg))
    return rt


class _Watchdog:
    def __init__(self, rt: ProcessRuntime, timeout: float = OUTER_TIMEOUT_S) -> None:
        self._timer = threading.Timer(timeout, rt.close)
        self._timer.daemon = True

    def __enter__(self):
        self._timer.start()
        return self

    def __exit__(self, *exc):
        self._timer.cancel()
        return False


class HandshakeTests(unittest.TestCase):
    def test_successful_handshake_and_drain(self) -> None:
        rt = _runtime(GOOD_CHILD)
        self.addCleanup(rt.close)
        with _Watchdog(rt):
            rt.spawn()
            session = _session()
            hello = rt.run_hello(session, "nonce-001")
            self.assertEqual(hello["plugin_id"], PLUGIN_ID)
            activation = rt.run_activation(session)
            self.assertEqual(activation["activation_id"], ACTIVATION_ID)
            self.assertEqual(session.state, "active")
            drain = rt.run_drain(session, 10000)
            self.assertEqual(drain, {"drained": True})
            session.deactivate()
            self.assertEqual(session.state, "inactive")
        rt.close()
        self.assertIsNone(rt._proc)
        self.assertLessEqual(rt.stderr_bytes_drained, 65536)

    def test_malformed_eof_child_cleaned_up(self) -> None:
        rt = _runtime(GARBAGE_CHILD)
        self.addCleanup(rt.close)
        with _Watchdog(rt):
            rt.spawn()
            proc = rt._proc
            session = _session()
            with self.assertRaises(ProcessRuntimeError) as ctx:
                rt.run_hello(session, "nonce-002")
            self.assertNotIn(TOKEN, str(ctx.exception))
        assert proc is not None
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(rt._proc)

    def test_mismatched_response_id_rejected(self) -> None:
        rt = _runtime(WRONG_ID_CHILD)
        self.addCleanup(rt.close)
        with _Watchdog(rt):
            rt.spawn()
            proc = rt._proc
            session = _session()
            with self.assertRaises(ProcessRuntimeError) as ctx:
                rt.run_hello(session, "nonce-003")
            self.assertEqual(ctx.exception.code, "id_mismatch")
            self.assertNotIn(TOKEN, str(ctx.exception))
        assert proc is not None
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(rt._proc)

    def test_no_token_or_stderr_leak_on_failure(self) -> None:
        rt = _runtime(GARBAGE_CHILD)
        self.addCleanup(rt.close)
        with _Watchdog(rt):
            rt.spawn()
            session = _session()
            try:
                rt.run_hello(session, "nonce-004")
            except ProcessRuntimeError as exc:
                text = f"{exc.code} {exc.detail} {exc!r}"
                self.assertNotIn(TOKEN, text)
            else:
                self.fail("expected ProcessRuntimeError")
        self.assertLessEqual(rt.stderr_bytes_drained, 65536)

    def test_large_write_to_never_reading_child_times_out_and_cleans_up(self) -> None:
        big_params = {"nonce": "n", "pad": "p" * 900000}
        rt = _runtime(NEVER_READS_CHILD, timeout_s=2.0)
        self.addCleanup(rt.close)
        import time as _t
        with _Watchdog(rt):
            rt.spawn()
            proc = rt._proc
            start = _t.monotonic()
            with self.assertRaises(ProcessRuntimeError) as ctx:
                rt._exchange_guarded("plugin.v1.lifecycle.hello", big_params)
            elapsed = _t.monotonic() - start
            self.assertIn(ctx.exception.code, ("timeout", "malformed_eof", "transport"))
            self.assertLess(elapsed, OUTER_TIMEOUT_S)
            self.assertNotIn(TOKEN, str(ctx.exception))
        assert proc is not None
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(rt._proc)

    def test_stderr_flood_before_hello_does_not_deadlock(self) -> None:
        rt = _runtime(STDERR_FLOOD_CHILD, timeout_s=8.0)
        self.addCleanup(rt.close)
        with _Watchdog(rt):
            rt.spawn()
            session = _session()
            hello = rt.run_hello(session, "nonce-flood")
            self.assertEqual(hello["plugin_id"], PLUGIN_ID)
        rt.close()
        self.assertLessEqual(rt.stderr_bytes_drained, 65536)
        self.assertGreater(rt.stderr_bytes_drained, 0)

    def test_strict_response_shape_rejected_and_cleaned_up(self) -> None:
        for name, payload in BAD_SHAPES.items():
            with self.subTest(shape=name):
                rt = _runtime(_raw_child(payload), timeout_s=5.0)
                self.addCleanup(rt.close)
                with _Watchdog(rt):
                    rt.spawn()
                    proc = rt._proc
                    session = _session()
                    with self.assertRaises(ProcessRuntimeError) as ctx:
                        rt.run_hello(session, f"nonce-{name}")
                    self.assertEqual(ctx.exception.code, "protocol", name)
                    self.assertNotIn(TOKEN, str(ctx.exception))
                assert proc is not None
                self.assertIsNotNone(proc.poll())
                rt.close()

    def test_invalid_hello_result_fail_closes_child(self) -> None:
        rt = _runtime(GOOD_CHILD)
        self.addCleanup(rt.close)
        with _Watchdog(rt):
            rt.spawn()
            proc = rt._proc
            session = _session(expected_plugin_id="org.other.plugin")
            with self.assertRaises(ProcessRuntimeError) as ctx:
                rt.run_hello(session, "nonce-identity")
            self.assertEqual(ctx.exception.code, "protocol")
            self.assertNotIn(TOKEN, str(ctx.exception))
        assert proc is not None
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(rt._proc)

    def test_strict_config_and_one_use_lifecycle(self) -> None:
        with self.assertRaises(ProcessRuntimeError):
            ProcessRuntimeConfig(argv=(), package_dir="/tmp", timeout_s=0.0)
        with self.assertRaises(ProcessRuntimeError):
            ProcessRuntimeConfig(
                argv=(sys.executable,), package_dir="/tmp", max_frames=0
            )
        rt = _runtime(GOOD_CHILD)
        self.addCleanup(rt.close)
        with _Watchdog(rt):
            rt.spawn()
            with self.assertRaises(ProcessRuntimeError):
                rt.spawn()
            session = _session()
            rt.run_hello(session, "nonce-once")
        rt.close()
        with self.assertRaises(ProcessRuntimeError):
            rt.spawn()



    def test_bundled_unsolicited_frame_rejected_not_dropped(self) -> None:
        lines = [
            "import json,sys",
            "pid=%r" % PLUGIN_ID,
            "pver=%r" % PLUGIN_VERSION,
            "line=sys.stdin.readline()",
            "req=json.loads(line)",
            "i=req.get('id')",
            "r={'plugin_id':pid,'plugin_version':pver,'capabilities':[]}",
            "sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':i,'result':r})+chr(10)" "+json.dumps({'jsonrpc':'2.0','id':9999,'result':{}})+chr(10))",
            "sys.stdout.flush()",
            "sys.stdin.read()",
        ]
        rt = _runtime(chr(10).join(lines) + chr(10))
        self.addCleanup(rt.close)
        with _Watchdog(rt):
            rt.spawn()
            proc = rt._proc
            session = _session()
            with self.assertRaises(ProcessRuntimeError) as ctx:
                rt.run_hello(session, "nonce-bundled")
            self.assertEqual(ctx.exception.code, "id_mismatch")
            self.assertNotIn(TOKEN, str(ctx.exception))
        assert proc is not None
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(rt._proc)

    def test_exited_child_before_hello_cleans_up_proc_handle(self) -> None:
        rt = _runtime("import sys; sys.exit(0)")
        self.addCleanup(rt.close)
        with _Watchdog(rt):
            rt.spawn()
            proc = rt._proc
            assert proc is not None
            import time as _t
            deadline = _t.monotonic() + 5.0
            while proc.poll() is None and _t.monotonic() < deadline:
                _t.sleep(0.02)
            self.assertIsNotNone(proc.poll())
            session = _session()
            with self.assertRaises(ProcessRuntimeError) as ctx:
                rt.run_hello(session, "nonce-exited")
            self.assertEqual(ctx.exception.code, "malformed_eof")
            self.assertNotIn(TOKEN, str(ctx.exception))
            self.assertIsNotNone(proc.poll())
            self.assertIsNone(rt._proc)

if __name__ == "__main__":
    unittest.main()
