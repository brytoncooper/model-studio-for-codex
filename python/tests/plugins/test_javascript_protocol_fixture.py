"""Bounded integration tests for the JSON-lines JavaScript protocol fixture.

Drives ``examples/protocol-fixture/plugin.js`` as a real subprocess (no
in-process shim or host runtime) through the inventory's canonical short
method names and the ``contracts/plugin.v1/lifecycle/*`` schemas.

The tests exercise the full JSON-RPC wire discipline (newline-delimited
frames, 1 MiB frame limit, duplicate-key / non-finite / lone-surrogate /
batch rejection) and the lifecycle state machine
(created -> hello_verified -> active -> draining -> inactive ->
deactivated) end to end. They also confirm the canonical method names are
primary and the documented ``plugin.v1.lifecycle.*`` legacy aliases are
accepted with identical responses.

These tests do not edit production code, do not run a live network call,
do not spawn any host engine, and do not require ``npm install``. The
fixture is stdlib-only.
"""
from __future__ import annotations

import io
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from uuid import UUID, uuid4

from model_deck.plugins.archive_inspection import inspect_archive
from model_deck.plugins.manifest_inspection import inspect_manifest
from model_deck_contracts.validator import validate_schema_ref

PLUGIN_ID = "org.example.protocol-fixture"
PLUGIN_VERSION = "1.0.0"
FIXTURE_DIR = Path(__file__).resolve().parents[3] / "examples" / "protocol-fixture"
def _discover_node_binary() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/node", "/usr/local/bin/node"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


NODE_BINARY = _discover_node_binary()
PLUGIN_ENTRYPOINT = FIXTURE_DIR / "plugin.js"
PLUGIN_MANIFEST = FIXTURE_DIR / "manifest.json"
FRAME_LIMIT = 1 * 1024 * 1024
HARD_EXIT_DEADLINE_S = 2.0
LIFECYCLE_KINDS = frozenset({
    "hello", "activate", "heartbeat", "invoke", "cancel", "drain", "deactivate",
})
CANONICAL_METHODS = frozenset(f"plugin.v1.{kind}" for kind in LIFECYCLE_KINDS)


def _node_available() -> bool:
    return NODE_BINARY is not None


@unittest.skipUnless(_node_available(), "node is required for the JavaScript protocol fixture")
class JavaScriptProtocolFixtureTests(unittest.TestCase):
    """Drive ``plugin.js`` as a real subprocess and assert on its wire behaviour."""

    def setUp(self) -> None:
        self.assertTrue(PLUGIN_ENTRYPOINT.is_file(), f"fixture entrypoint missing: {PLUGIN_ENTRYPOINT}")
        self.assertTrue(PLUGIN_MANIFEST.is_file(), f"fixture manifest missing: {PLUGIN_MANIFEST}")
        self._processes: list[_PluginProcess] = []

    def tearDown(self) -> None:
        for process in self._processes:
            process.close(join_timeout_s=0.5)
        self._processes.clear()

    # ------------------------------------------------------------------
    # Static inspection
    # ------------------------------------------------------------------

    def test_manifest_inspection_passes(self) -> None:
        with PLUGIN_MANIFEST.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        result = inspect_manifest(raw, caller_plugin_api_major=1, caller_plugin_api_minor=0)
        self.assertTrue(result.ok, f"manifest inspection failed: {result.errors!r}")
        self.assertEqual(result.identity.manifest_id, PLUGIN_ID)
        self.assertEqual(result.identity.version, PLUGIN_VERSION)
        self.assertEqual(result.entrypoint.runtime, "node")
        self.assertEqual(result.entrypoint.path, "plugin.js")
        operation_ids = sorted(op.operation_id for op in result.contributions.operations)
        self.assertEqual(operation_ids, [
            "org.example.protocol-fixture.echo",
            "org.example.protocol-fixture.pending",
        ])
        effects = {op.operation_id: op.effect for op in result.contributions.operations}
        self.assertEqual(effects, {
            "org.example.protocol-fixture.echo": "read",
            "org.example.protocol-fixture.pending": "read",
        })
        for operation in raw["contributes"]["operations"]:
            self.assertEqual(
                operation["input_schema"],
                "contracts/common/types.schema.json#/definitions/json_value",
            )
            self.assertEqual(
                operation["output_schema"],
                "contracts/common/types.schema.json#/definitions/json_value",
            )

    def test_canonical_methods_match_frozen_inventory(self) -> None:
        inventory = json.loads(
            (FIXTURE_DIR.parents[1] / "contracts" / "operations.inventory.json")
            .read_text(encoding="utf-8")
        )
        inventory_methods = {
            entry["method"] for entry in inventory["plugin_v1_lifecycle"]
        }
        self.assertEqual(inventory_methods, CANONICAL_METHODS)

    def test_archive_inspection_passes_when_packed(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as writer:
            writer.writestr("manifest.json", PLUGIN_MANIFEST.read_text(encoding="utf-8"))
            writer.writestr("plugin.js", PLUGIN_ENTRYPOINT.read_text(encoding="utf-8"))
            writer.writestr("README.md", "Fixture archive for JS protocol fixture inspection.\n")
        blob = buffer.getvalue()
        result = inspect_archive(blob)
        self.assertTrue(result.ok)
        names = sorted(entry.name for entry in result.entries)
        self.assertEqual(names, ["README.md", "manifest.json", "plugin.js"])
        self.assertEqual(result.total_entries, 3)

    # ------------------------------------------------------------------
    # Wire negotiation
    # ------------------------------------------------------------------

    def test_negotiation_hello_activate_heartbeat_drain_deactivate(self) -> None:
        process = self._spawn()
        hello = process.request("plugin.v1.hello", {
            "offered_api": {"major": 1, "minor": 0},
            "nonce": "negotiation",
        })
        self.assertEqual(hello["result"]["plugin_id"], PLUGIN_ID)
        self.assertEqual(hello["result"]["plugin_version"], PLUGIN_VERSION)
        self.assertIn("fixture.js.stdlib", hello["result"]["capabilities"])

        activate = process.request("plugin.v1.activate", {
            "activation_token": "fixture-token",
            "allowed_broker_methods": ["plugin.v1.invoke"],
        })
        activation_id = activate["result"]["activation_id"]
        UUID(activation_id)  # raises if not a valid UUID
        self.assertEqual(activate["result"]["invocation_handle_prefix"], "fixture:")

        heartbeat = process.request("plugin.v1.heartbeat", {
            "activation_id": activation_id,
        })
        self.assertEqual(heartbeat["result"], {"alive": True})

        drain = process.request("plugin.v1.drain", {"deadline_ms": 1000})
        self.assertEqual(drain["result"], {"drained": True})
        deactivate = process.request("plugin.v1.deactivate", {})
        self.assertEqual(deactivate["result"], {"deactivated": True})

        process.close()
        self.assertTrue(process.exited_cleanly(deadline_s=HARD_EXIT_DEADLINE_S))

    def test_invoke_echo_round_trips_value_including_falsy(self) -> None:
        process = self._spawn()
        activation_id = self._hello_activate(process)
        structured = {
            "string": "hello",
            "empty_string": "",
            "number": 42,
            "zero": 0,
            "negative": -1,
            "boolean_false": False,
            "boolean_true": True,
            "null_value": None,
            "nested": {"x": [1, 2, {"y": None}], "z": []},
            "list_empty": [],
            "a": "same",
            "b": "same",
            "emoji": "😀",
            "😀": "supplementary key",
        }
        for index, original in enumerate((False, 0, "", None, structured)):
            with self.subTest(original=original):
                response = process.request("plugin.v1.invoke", {
                    "operation_id": "org.example.protocol-fixture.echo",
                    "input": original,
                    "broker_context": {
                        "activation_id": activation_id,
                        "plugin_id": PLUGIN_ID,
                        "invocation_handle": f"fixture-handle-{index}",
                        "revocation_generation": 0,
                    },
                })
                self.assertEqual(response["result"]["output"], {"echo": original})
        process.close(join_timeout_s=0.5)

    def test_pending_invocation_exposes_job_for_real_cancel_without_late_output(self) -> None:
        process = self._spawn()
        activation_id = self._hello_activate(process)
        invoke = process.request(
            "plugin.v1.invoke",
            {
                "operation_id": "org.example.protocol-fixture.pending",
                "input": {"sentinel": "wait-for-cancel"},
                "broker_context": {
                    "activation_id": activation_id,
                    "plugin_id": PLUGIN_ID,
                    "invocation_handle": "fixture-handle-cancel",
                    "revocation_generation": 0,
                },
            },
            id=17,
        )
        self.assertEqual(invoke["id"], 17)
        self.assertEqual(invoke["result"]["output"], {"pending": True})
        job_id = invoke["result"]["job_id"]
        UUID(job_id)

        cancel = process.request("plugin.v1.cancel", {"job_id": job_id}, id=18)
        self.assertEqual(cancel, {
            "jsonrpc": "2.0",
            "id": 18,
            "result": {"accepted": True},
        })
        self.assertIsNone(process.collect_pending_response(timeout_s=0.1))

        drain = process.request("plugin.v1.drain", {"deadline_ms": 500}, id=19)
        self.assertEqual(drain["result"], {"drained": True})
        deact = process.request("plugin.v1.deactivate", {}, id=20)
        self.assertEqual(deact["result"], {"deactivated": True})
        self.assertIsNone(process.collect_pending_response(timeout_s=0.1))
        process.close(join_timeout_s=HARD_EXIT_DEADLINE_S)
        self.assertTrue(process.exited_cleanly(deadline_s=HARD_EXIT_DEADLINE_S))

    def test_deactivate_rejects_subsequent_lifecycle_calls(self) -> None:
        process = self._spawn()
        self._hello_activate(process)
        deact = process.request("plugin.v1.deactivate", {})
        self.assertEqual(deact["result"], {"deactivated": True})
        # Any further request after deactivate must not be answered; the
        # subprocess should exit cleanly within the post-deactivate quiet
        # window.
        process.write_raw(b'{"jsonrpc":"2.0","id":99,"method":"plugin.v1.hello",'
                          b'"params":{"offered_api":{"major":1,"minor":0},"nonce":"after"}}\n')
        leftover = process.drain_pending_responses(timeout_s=0.2)
        self.assertEqual(leftover, [], "subprocess emitted a response after deactivate")
        process.close(join_timeout_s=0.5)
        self.assertTrue(process.exited_cleanly(deadline_s=HARD_EXIT_DEADLINE_S))

    def test_unknown_method_returns_jsonrpc_error(self) -> None:
        process = self._spawn()
        self._hello_activate(process)
        response = process.request("plugin.v1.lifecycle.unknown", {})
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32601)
        # Process must still be alive after a method-not-found error.
        self.assertIsNone(process.poll())
        process.request("plugin.v1.deactivate", {})
        process.close(join_timeout_s=0.5)

    def test_lifecycle_prefixed_legacy_aliases_cover_full_protocol(self) -> None:
        process = self._spawn()
        hello = process.request("plugin.v1.lifecycle.hello", {
            "offered_api": {"major": 1, "minor": 0},
            "nonce": "legacy-hello",
        })
        self.assertEqual(hello["result"]["plugin_id"], PLUGIN_ID)
        self.assertEqual(hello["result"]["plugin_version"], PLUGIN_VERSION)
        self.assertIn("fixture.js.stdlib", hello["result"]["capabilities"])

        activate = process.request("plugin.v1.lifecycle.activate", {
            "activation_token": "fixture-token",
            "allowed_broker_methods": [],
        })
        activation_id = activate["result"]["activation_id"]
        UUID(activation_id)

        heartbeat = process.request(
            "plugin.v1.lifecycle.heartbeat",
            {"activation_id": activation_id},
        )
        self.assertEqual(heartbeat["result"], {"alive": True})
        pending = process.request("plugin.v1.lifecycle.invoke", {
            "operation_id": "org.example.protocol-fixture.pending",
            "input": None,
            "broker_context": {
                "activation_id": activation_id,
                "plugin_id": PLUGIN_ID,
                "invocation_handle": "legacy-handle",
                "revocation_generation": 0,
            },
        })
        cancel = process.request(
            "plugin.v1.lifecycle.cancel",
            {"job_id": pending["result"]["job_id"]},
        )
        self.assertEqual(cancel["result"], {"accepted": True})
        drain = process.request("plugin.v1.lifecycle.drain", {"deadline_ms": 500})
        self.assertEqual(drain["result"], {"drained": True})
        deactivate = process.request("plugin.v1.lifecycle.deactivate", {})
        self.assertEqual(deactivate["result"], {"deactivated": True})
        process.close()
        self.assertTrue(process.exited_cleanly(deadline_s=HARD_EXIT_DEADLINE_S))

    def test_legacy_alias_unknown_still_returns_jsonrpc_error(self) -> None:
        process = self._spawn()
        response = process.request("plugin.v1.invocation.launch", {})
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32601)
        process.close(join_timeout_s=0.5)

    def test_lifecycle_order_violation_rejected(self) -> None:
        process = self._spawn()
        # activate before hello must be rejected
        response = process.request("plugin.v1.activate", {
            "activation_token": "fixture-token",
            "allowed_broker_methods": [],
        })
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32602)
        process.close(join_timeout_s=0.5)

    def test_invalid_input_rejected_at_dispatch(self) -> None:
        process = self._spawn()
        activation_id = self._hello_activate(process)
        # Missing broker_context
        response = process.request("plugin.v1.invoke", {
            "operation_id": "org.example.protocol-fixture.echo",
            "input": {"x": 1},
        }, validate_contract=False)
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32602)
        # Unknown operation_id
        unknown_op = process.request("plugin.v1.invoke", {
            "operation_id": "org.example.protocol-fixture.does-not-exist",
            "input": None,
            "broker_context": {
                "activation_id": activation_id,
                "plugin_id": PLUGIN_ID,
                "invocation_handle": "x",
                "revocation_generation": 0,
            },
        })
        self.assertIn("error", unknown_op)
        self.assertEqual(unknown_op["error"]["code"], -32601)
        process.request("plugin.v1.deactivate", {})
        process.close(join_timeout_s=0.5)

    def test_schema_invalid_lifecycle_params_are_rejected_by_child(self) -> None:
        process = self._spawn()
        fractional_hello = process.request("plugin.v1.hello", {
            "offered_api": {"major": 1, "minor": 0.5},
            "nonce": "bad",
        }, validate_contract=False)
        self.assertEqual(fractional_hello["error"]["code"], -32602)
        extra_hello = process.request("plugin.v1.hello", {
            "offered_api": {"major": 1, "minor": 0},
            "nonce": "bad",
            "extra": True,
        }, validate_contract=False)
        self.assertEqual(extra_hello["error"]["code"], -32602)

        process.request("plugin.v1.hello", {
            "offered_api": {"major": 1, "minor": 0},
            "nonce": "valid",
        })
        too_many_methods = process.request("plugin.v1.activate", {
            "activation_token": "fixture-token",
            "allowed_broker_methods": [f"plugin.v1.broker.test.{index}" for index in range(65)],
        }, validate_contract=False)
        self.assertEqual(too_many_methods["error"]["code"], -32602)

        activation = process.request("plugin.v1.activate", {
            "activation_token": "fixture-token",
            "allowed_broker_methods": [],
            "config_revision": 0,
        })
        UUID(activation["result"]["activation_id"])
        fractional_drain = process.request(
            "plugin.v1.drain",
            {"deadline_ms": 1.5},
            validate_contract=False,
        )
        self.assertEqual(fractional_drain["error"]["code"], -32602)
        process.request("plugin.v1.deactivate", {})
        process.close(join_timeout_s=0.5)

    def test_schema_string_bounds_count_astral_code_points(self) -> None:
        process = self._spawn()
        emoji = "😀"
        hello = process.request("plugin.v1.hello", {
            "offered_api": {"major": 1, "minor": 0},
            "nonce": emoji * 128,
        })
        self.assertEqual(hello["result"]["plugin_id"], PLUGIN_ID)

        activate = process.request("plugin.v1.activate", {
            "activation_token": emoji * 512,
            "allowed_broker_methods": [emoji * 128],
        })
        activation_id = activate["result"]["activation_id"]
        echo = process.request("plugin.v1.invoke", {
            "operation_id": "org.example.protocol-fixture.echo",
            "input": False,
            "broker_context": {
                "activation_id": activation_id,
                "plugin_id": PLUGIN_ID,
                "invocation_handle": emoji * 128,
                "revocation_generation": 0,
            },
        })
        self.assertEqual(echo["result"]["output"], {"echo": False})
        process.request("plugin.v1.deactivate", {})
        process.close(join_timeout_s=0.5)

    def test_malformed_frame_rejected_at_codec(self) -> None:
        process = self._spawn()
        frames = (
            (b'{"jsonrpc":"2.0","id":1,"method":"m","params":{"x":1,"x":2}}\n', "duplicate_key"),
            (b'{"jsonrpc":"2.0","id":2,"method":"m","params":{"a":1,"\\u0061":2}}\n', "duplicate_key"),
            (b'{"jsonrpc":"2.0","id":3,"method":"m","params":{"items":[{"x":1,"x":2}]}}\n', "duplicate_key"),
            (b'{"jsonrpc":"2.0","id":4,"method":"m","params":{"x":1e999}}\n', "non_finite_number"),
            (b'[{"jsonrpc":"2.0","id":5,"method":"m"}]\n', "batch_not_supported"),
            (b'\n', "empty_frame"),
            (b'{"jsonrpc":"2.0","id":6,"method":"m","params":{"bad":"\xff"}}\n', "invalid_utf8"),
            (b'{"jsonrpc":"2.0","id":7,"method":"m","params":{"\\uD800":1}}\n', "invalid_utf8"),
        )
        for frame, expected in frames:
            with self.subTest(expected=expected):
                process.write_raw(frame)
                response = process.read_response(timeout_s=2.0)
                self.assertIsNotNone(response)
                self.assertEqual(response["error"]["code"], -32600)
                self.assertEqual(response["error"]["message"], expected)

        deep_value = b"[" * 66 + b"0" + b"]" * 66
        process.write_raw(
            b'{"jsonrpc":"2.0","id":8,"method":"m","params":{"value":'
            + deep_value
            + b"}}\n"
        )
        response = process.read_response(timeout_s=2.0)
        self.assertEqual(response["error"]["message"], "invalid_json")

        groups = [[0] * 4000 for _ in range(51)]
        node_frame = json.dumps({
            "jsonrpc": "2.0",
            "id": 9,
            "method": "m",
            "params": {"groups": groups},
        }, separators=(",", ":")).encode("utf-8") + b"\n"
        self.assertLessEqual(len(node_frame), FRAME_LIMIT)
        process.write_raw(node_frame)
        response = process.read_response(timeout_s=2.0)
        self.assertEqual(response["error"]["message"], "invalid_json")
        process.close(join_timeout_s=0.5)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _spawn(self) -> "_PluginProcess":
        process = _PluginProcess.spawn(
            argv=(NODE_BINARY, str(PLUGIN_ENTRYPOINT)),
            cwd=str(FIXTURE_DIR),
        )
        self._processes.append(process)
        return process

    def _hello_activate(self, process: "_PluginProcess") -> str:
        process.request("plugin.v1.hello", {
            "offered_api": {"major": 1, "minor": 0},
            "nonce": uuid4().hex,
        })
        activate = process.request("plugin.v1.activate", {
            "activation_token": "fixture-token",
            "allowed_broker_methods": [],
        })
        return activate["result"]["activation_id"]


class _PluginProcess:
    """Subprocess wrapper that reads/writes one JSON frame per line.

    Mirrors the codec discipline used by the bundled Python plugin codec:
    one JSON object per line, bounded frames, no input echo in errors.
    """

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self._stdout_fd = proc.stdout.fileno()
        self._stdin_fd = proc.stdin.fileno()
        self._stderr_fd = proc.stderr.fileno()
        self._id_counter = 0
        self._pending: dict[int, list[dict]] = {}
        self._late_responses: list[dict] = []
        self._frame_buffer = bytearray()

    @classmethod
    def spawn(cls, *, argv: tuple[str, ...], cwd: str) -> "_PluginProcess":
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        return cls(proc)

    def request(self, method: str, params: dict | None = None, *, id: int | None = None,
                timeout_s: float = 5.0, validate_contract: bool = True) -> dict:
        self._id_counter += 1
        request_id = id if id is not None else self._id_counter
        envelope = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            envelope["params"] = params
        kind = method.rsplit(".", 1)[-1]
        lifecycle_method = (
            method in CANONICAL_METHODS
            or method == f"plugin.v1.lifecycle.{kind}" and kind in LIFECYCLE_KINDS
        )
        if validate_contract and lifecycle_method:
            validate_schema_ref(
                f"contracts/plugin.v1/lifecycle/{kind}.params.schema.json",
                {} if params is None else params,
            )
        self.write_raw((json.dumps(envelope, separators=(",", ":")) + "\n").encode("utf-8"))
        response = self._await_response(request_id, timeout_s=timeout_s)
        if validate_contract and lifecycle_method and "result" in response:
            validate_schema_ref(
                f"contracts/plugin.v1/lifecycle/{kind}.result.schema.json",
                response["result"],
            )
        return response

    def write_raw(self, payload: bytes) -> None:
        if len(payload) > FRAME_LIMIT:
            raise ValueError("test frame exceeds 1 MiB codec budget")
        try:
            os.write(self._stdin_fd, payload)
        except BrokenPipeError as exc:
            raise AssertionError("subprocess stdin closed unexpectedly") from exc

    def read_response(self, *, timeout_s: float) -> dict | None:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            frame = self._next_frame(remaining)
            if frame is None:
                return None
            try:
                return json.loads(frame.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise AssertionError(f"non-JSON frame from subprocess: {frame!r}") from exc

    def collect_pending_response(self, *, timeout_s: float) -> dict | None:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            frame = self._next_frame(remaining)
            if frame is None:
                return None
            try:
                return json.loads(frame.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise AssertionError(f"non-JSON frame from subprocess: {frame!r}") from exc

    def drain_pending_responses(self, *, timeout_s: float) -> list[dict]:
        deadline = time.monotonic() + timeout_s
        out: list[dict] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            frame = self._next_frame(remaining)
            if frame is None:
                break
            try:
                out.append(json.loads(frame.decode("utf-8")))
            except json.JSONDecodeError:
                pass
        return out

    def close(self, *, join_timeout_s: float = 1.0) -> None:
        if self._proc.poll() is None:
            try:
                self._proc.stdin.close()
            except Exception:  # noqa: BLE001 -- best-effort
                pass
            try:
                self._proc.wait(timeout=join_timeout_s)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=join_timeout_s)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            try:
                stream.close()
            except Exception:  # noqa: BLE001 -- best-effort fixture cleanup
                pass

    def exited_cleanly(self, *, deadline_s: float) -> bool:
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            rc = self._proc.poll()
            if rc is not None:
                return rc == 0
            time.sleep(0.02)
        return False

    def poll(self) -> int | None:
        return self._proc.poll()

    def _await_response(self, request_id: int, *, timeout_s: float) -> dict:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out waiting for response to id={request_id}")
            frame = self._next_frame(remaining)
            if frame is None:
                continue
            try:
                payload = json.loads(frame.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise AssertionError(f"non-JSON frame from subprocess: {frame!r}") from exc
            self._pending.setdefault(payload.get("id"), []).append(payload)
            if payload.get("id") == request_id:
                return payload
            # Not our response yet; drain other ids.
            for response in self._pending.pop(request_id, []):
                if response.get("id") == request_id:
                    return response
            continue

    def _next_frame(self, timeout_s: float) -> bytes | None:
        deadline = time.monotonic() + timeout_s
        while True:
            newline_idx = self._frame_buffer.find(b"\n")
            if newline_idx >= 0:
                line = bytes(self._frame_buffer[:newline_idx])
                del self._frame_buffer[: newline_idx + 1]
                if line.endswith(b"\r"):
                    line = line[:-1]
                if not line:
                    return b""  # empty frame
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            readable, _, _ = select.select([self._stdout_fd], [], [], remaining)
            if not readable:
                continue
            try:
                chunk = os.read(self._stdout_fd, FRAME_LIMIT + 1)
            except OSError:
                return None
            if not chunk:
                return None
            if len(chunk) > FRAME_LIMIT:
                raise AssertionError("subprocess emitted an oversized frame")
            self._frame_buffer.extend(chunk)


if __name__ == "__main__":
    unittest.main()
