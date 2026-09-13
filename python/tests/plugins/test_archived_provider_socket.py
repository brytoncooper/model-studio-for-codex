"""Archived extracted standard-library provider routed through the authenticated PUBLIC socket.

This is a bounded integration test that proves an archived provider:

1. is staged as a ZIP, extracted to a temp artifact root, and launched as a
   real subprocess (no in-process shim);
2. is composed into the engine via ``provider_execution`` /
   ``provider_route_definitions``;
3. reaches the public Unix socket path through ``UnixSocketEngineClient``;
4. drives public ``engine.v1.sessions.*`` / ``engine.v1.runs.*`` operations;
5. emits ``usage.observed`` events that reconcile into a durable record
   reachable through ``engine.v1.usage.query``.

It does not edit production code, the existing deterministic fixture, the
existing ``test_external_provider_integration.py`` /
``test_provider_bootstrap.py``, or run a live network call. The custom ZIP
below exists so the worker can emit ``usage.observed``; the existing
deterministic fixture only emits ``content.delta`` / ``tool.requested`` /
``run.completed|cancelled``.

It does not assert the rest of B18. Coverage is the four scenarios below.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4
import zipfile

from model_deck.adapters.platform.macos.isolated_roots import validate_isolated_roots
from model_deck.adapters.routing.registered import ProviderRouteDefinition
from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import build_engine_server
from model_deck.engine.routing.ports import CapabilityFeature, CapabilityTriState, ExecutionMode
from model_deck.plugins.archive_inspection import inspect_archive
from model_deck.plugins.artifact_store import stage_archive
from model_deck.plugins.lifecycle_session import LifecycleSession
from model_deck.plugins.process_runtime import ProcessRuntime, ProcessRuntimeConfig
from model_deck.plugins.provider_proxy import ExternalProviderExecution
from model_deck_contracts import validate_schema_ref
from model_deck_contracts.paths import repo_root


PLUGIN_ID = "org.example.socket-fixture"
PLUGIN_VERSION = "1.0.0"
CONNECTION_ID = "550e8400-e29b-41d4-a716-4466554400a1"

MANIFEST = {
    "manifest_version": 1,
    "id": PLUGIN_ID,
    "version": PLUGIN_VERSION,
    "plugin_api": {"major": 1, "minimum_minor": 0},
    "entrypoint": {"runtime": "python", "path": "plugin.py"},
    "permissions": [],
    "contributes": {
        "providers": [{
            "id": PLUGIN_ID,
            "port": "provider.execution/v1",
            "execution_mode": "custom",
            "features": ["tools"],
        }]
    },
}

# Standard-library + stdio only. The -I flag the runtime uses plus this guard
# prevent the fixture from importing the private engine package, matching the
# real child contract enforced by ``ProcessRuntime``.
PLUGIN_PY = r'''"""Standalone standard-library provider fixture exercised through the public socket."""
from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import datetime, timezone

PLUGIN_ID = "org.example.socket-fixture"
VERSION = "1.0.0"
FRAME_LIMIT = 1048576


def send(frame):
    encoded = json.dumps(frame, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > FRAME_LIMIT:
        raise ValueError("fixture frame exceeds bound")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def reply(request, result):
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})


def emit(run, kind, **payload):
    sequence = run["sequence"]
    run["sequence"] += 1
    event = {"kind": kind, "run_id": run["run_id"], "session_id": run["session_id"],
             "sequence": sequence, "event_schema_version": 1,
             "observed_at": datetime.now(timezone.utc).isoformat(), **payload}
    params = {"adapter_run_handle": run["handle"], "sequence": sequence, "event": event}
    send({"jsonrpc": "2.0", "method": "plugin.v1.provider.event", "params": params})


def main():
    if not sys.flags.isolated or importlib.util.find_spec("model_deck") is not None:
        raise RuntimeError("fixture requires isolated imports")
    runs = {}
    activation = None
    while True:
        line = sys.stdin.buffer.readline(FRAME_LIMIT + 1)
        if not line:
            return
        if len(line) > FRAME_LIMIT or not line.endswith(b"\n"):
            raise ValueError("fixture frame rejected")
        request = json.loads(line)
        method, params = request["method"], request["params"]
        if method == "plugin.v1.lifecycle.hello":
            reply(request, {"plugin_id": PLUGIN_ID, "plugin_version": VERSION,
                            "capabilities": ["fixture.isolated-imports"]})
        elif method == "plugin.v1.lifecycle.activate":
            activation = str(uuid.uuid4())
            reply(request, {"activation_id": activation, "invocation_handle_prefix": "fixture:"})
        elif method == "plugin.v1.lifecycle.drain":
            reply(request, {"drained": True})
        elif method == "plugin.v1.provider.start" and activation:
            captured = params["run_request"]
            mode = params["route_snapshot"]["provider_model_id"]
            handle = str(uuid.uuid4())
            run = {"handle": handle, "run_id": captured["run_id"], "session_id": captured["session_id"],
                   "sequence": 0, "mode": mode, "tool": None}
            runs[handle] = run
            reply(request, {"adapter_run_handle": handle})
            if mode == "text":
                emit(run, "run.started")
                emit(run, "content.delta", channel="text", delta="external deterministic text")
                emit(run, "run.completed", terminal_result={"outcome": "completed"})
            elif mode == "tool":
                run["tool"] = "fixture-call"
                emit(run, "run.started")
                emit(run, "tool.requested", tool_call={"call_id": run["tool"],
                     "tool_name": captured["tools"][0]["name"], "arguments": {"query": "fixture"}})
            elif mode == "wait":
                emit(run, "run.started")
            elif mode == "usage":
                emit(run, "run.started")
                emit(run, "usage.observed", usage={
                    "run_id": run["run_id"], "session_id": run["session_id"],
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "units": 7, "unit_kind": "input_tokens"})
                emit(run, "run.completed", terminal_result={"outcome": "completed"})
            else:
                raise ValueError("fixture mode unsupported: " + mode)
        elif method == "plugin.v1.provider.submit_tool_result" and activation:
            run = runs[params["adapter_run_handle"]]
            accepted = params["call_id"] == run["tool"]
            reply(request, {"accepted": accepted})
            if accepted:
                run["tool"] = None
                emit(run, "content.delta", channel="text", delta="tool result received")
                emit(run, "run.completed", terminal_result={"outcome": "completed"})
        elif method == "plugin.v1.provider.cancel" and activation:
            run = runs[params["adapter_run_handle"]]
            reply(request, {"accepted": True, "confirmed": True})
            emit(run, "run.cancelled", terminal_result={"outcome": "cancelled"})
        elif method == "plugin.v1.provider.ack" and activation:
            reply(request, {"credit": 1})
        else:
            raise ValueError("fixture method unsupported")


if __name__ == "__main__":
    main()
'''


def _build_archive_blob() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as writer:
        writer.writestr("manifest.json", json.dumps(MANIFEST, sort_keys=True, separators=(",", ":")))
        writer.writestr("plugin.py", PLUGIN_PY)
        writer.writestr("README.md", "Test fixture for archived socket integration.\n")
    return buffer.getvalue()


class ArchivedProviderSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="md-b18-sock-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state_root = self.root / "state"
        self.artifact_root = self.root / "artifact"
        self.socket_root = self.root / "socket"
        validate_isolated_roots(self.state_root, self.artifact_root, self.socket_root,
                                source_root=repo_root())

        blob = _build_archive_blob()
        validate_schema_ref("contracts/plugin.v1/manifest.schema.json", MANIFEST)
        self.assertEqual(inspect_archive(blob).total_entries, 3)
        self.artifact_root.mkdir()
        staged = stage_archive(blob, store_root=self.artifact_root)
        self.assertFalse(staged.already_present)
        self.assertTrue(stage_archive(blob, store_root=self.artifact_root).already_present)
        self.artifact_path = Path(staged.artifact_path)

        self.runtime = ProcessRuntime(ProcessRuntimeConfig(
            argv=(sys.executable, "-I", str(self.artifact_path / MANIFEST["entrypoint"]["path"])),
            package_dir=str(self.artifact_path), timeout_s=2))
        self.addCleanup(self.runtime.close)
        watchdog = threading.Timer(20, self.runtime.close)
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)

        lifecycle = LifecycleSession(expected_plugin_id=PLUGIN_ID, expected_plugin_version=PLUGIN_VERSION,
                                     offered_api_major=1, offered_api_minor=0,
                                     activation_token="socket-fixture-token", allowed_broker_methods=())
        self.runtime.spawn()
        hello = self.runtime.run_hello(lifecycle, "socket-fixture-nonce")
        self.assertIn("fixture.isolated-imports", hello["capabilities"])
        self.runtime.run_activation(lifecycle)
        self.proxy = ExternalProviderExecution(self.runtime.provider_channel(), provider_id=PLUGIN_ID,
                                                clock=lambda: datetime.now(timezone.utc),
                                                request_timeout_s=2)
        self.addCleanup(self.proxy.close)

        runtime = build_engine_server(state_root=self.state_root, artifact_root=self.artifact_root,
            socket_root=self.socket_root,
            legacy_agents_dir=repo_root() / "python/tests/engine/fixtures/legacy_agent",
            default_connection_id=CONNECTION_ID, source_root=repo_root(), enable_application_state=True,
            provider_execution=self.proxy,
            provider_route_definitions={PLUGIN_ID: ProviderRouteDefinition(
                ExecutionMode.CUSTOM,
                (CapabilityFeature("tools", CapabilityTriState.SUPPORTED),),
                "ref:archived.provider")})
        runtime.server.start()
        self.addCleanup(runtime.server.stop)
        self.runtime_handle = runtime

        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text(encoding="utf-8").strip()
        self._id_counter = 1
        self.client = UnixSocketEngineClient(descriptor.socket_path, timeout_seconds=5.0)
        self.session = self.client.session()
        self.session.__enter__()
        self.addCleanup(self.session.__exit__, None, None, None)
        self._authenticate(descriptor, credential)
        self._register_connection()
        self.registration_ids = {
            "text": self._register_model("text", "Archived Text"),
            "tool": self._register_model("tool", "Archived Tool"),
            "wait": self._register_model("wait", "Archived Wait"),
            "usage": self._register_model("usage", "Archived Usage"),
        }

    def _call(self, method, params):
        self._id_counter += 1
        request = {"jsonrpc": "2.0", "id": self._id_counter, "method": "engine.v1." + method,
                   "params": params}
        return self.session.call(request)

    def _authenticate(self, descriptor, credential):
        response = self.session.call({"jsonrpc": "2.0", "id": self._id_counter,
            "method": "engine.v1.hello", "params": {"client_name": "archived-socket-test",
                "offered_api": {"major": 1, "minor": 0},
                "authentication": {"engine_instance_id": descriptor.engine_instance_id,
                                   "instance_nonce": descriptor.instance_nonce,
                                   "credential": credential}}})
        self._id_counter += 1
        self.assertTrue(response["result"]["authenticated"], response)

    def _register_connection(self):
        response = self._call("connections.save",
            {"expected_revision": 0, "idempotency_key": "archived-connection",
             "connection": {"connection_id": CONNECTION_ID, "provider_id": PLUGIN_ID,
                            "credential_ref": "ref:archived.credential",
                            "endpoint_config_ref": "ref:archived.endpoint"}})
        self.assertIn("connection", response["result"])

    def _register_model(self, provider_model_id, display_name):
        response = self._call("models.register",
            {"connection_id": CONNECTION_ID, "provider_model_id": provider_model_id,
             "display_name": display_name, "expected_revision": 0,
             "idempotency_key": "archived-" + provider_model_id})
        return response["result"]["model"]["registration_id"]

    def _start_run(self, registration_id, *, model, tools=None):
        session_id = self._call("sessions.create", {"registration_id": registration_id})["result"]["session_id"]
        params = {"session_id": session_id, "registration_id": registration_id,
                  "client_request_id": "archived-" + model, "idempotency_key": session_id}
        if tools is not None:
            params["tools"] = tools
        return self._call("runs.start", params)["result"]["run"]["run_id"]

    def _read_until(self, run_id, kinds, *, max_seconds=15):
        """Read pushed notifications and acknowledge only consumed events."""
        response = self._call("events.subscribe",
            {"topics": ["run:" + run_id], "initial_credit": 1})
        subscription_id = response["result"]["subscription_id"]
        collected = []
        deadline = time.monotonic() + max_seconds
        try:
            while time.monotonic() < deadline:
                try:
                    notification = self.session.read_notification()
                except TimeoutError:
                    continue
                self.assertEqual(
                    notification["params"]["subscription_id"],
                    subscription_id,
                )
                event = notification["params"]["event"]
                collected.append(event)
                if event.get("kind") in kinds:
                    return collected
                acknowledged = self._call("events.ack", {
                    "subscription_id": subscription_id,
                    "sequence": event["sequence"],
                })
                self.assertIn("result", acknowledged, acknowledged)
            self.fail("did not observe any of " + repr(kinds)
                      + "; saw " + repr([e.get("kind") for e in collected]))
        finally:
            unsubscribed = self._call(
                "events.unsubscribe",
                {"subscription_id": subscription_id},
            )
            self.assertTrue(unsubscribed["result"]["unsubscribed"], unsubscribed)

    def test_archived_text_completion_routes_through_public_socket(self):
        run_id = self._start_run(self.registration_ids["text"], model="text")
        events = self._read_until(run_id, ("run.completed",))
        kinds = [event.get("kind") for event in events]
        self.assertIn("run.started", kinds)
        self.assertIn("content.delta", kinds)
        self.assertIn("run.completed", kinds)
        delta = next(event for event in events if event.get("kind") == "content.delta")
        self.assertEqual(delta.get("delta"), "external deterministic text")
        run = self._call("runs.get", {"run_id": run_id})["result"]["run"]
        self.assertEqual(run["state"], "completed")

    def test_archived_tool_roundtrip_resolves_through_public_socket(self):
        tools = [{"name": "lookup", "input_schema": {"type": "object"}, "host_execution_required": True}]
        run_id = self._start_run(self.registration_ids["tool"], model="tool", tools=tools)
        events = self._read_until(run_id, ("tool.requested",))
        requested = next(event for event in events if event.get("kind") == "tool.requested")
        self.assertEqual(requested["tool_call"]["call_id"], "fixture-call")
        self.assertEqual(requested["tool_call"]["arguments"], {"query": "fixture"})
        run = self._call("runs.get", {"run_id": run_id})["result"]["run"]
        self.assertEqual(run["state"], "waiting_for_tool")
        self._call("runs.submit_tool_result",
            {"run_id": run_id, "call_id": "fixture-call", "result": {"answer": 42},
             "idempotency_key": str(uuid4())})
        completed = self._read_until(run_id, ("run.completed",))
        completion_kinds = [event.get("kind") for event in completed]
        self.assertIn("content.delta", completion_kinds)
        self.assertIn("run.completed", completion_kinds)
        run = self._call("runs.get", {"run_id": run_id})["result"]["run"]
        self.assertEqual(run["state"], "completed")

    def test_archived_confirmed_cancel_completes_through_public_socket(self):
        run_id = self._start_run(self.registration_ids["wait"], model="wait")
        started = self._read_until(run_id, ("run.started",))
        self.assertTrue(any(event.get("kind") == "run.started" for event in started))
        cancel = self._call("runs.cancel", {"run_id": run_id, "idempotency_key": str(uuid4())})
        self.assertTrue(cancel["result"]["accepted"])
        cancelled = self._read_until(run_id, ("run.cancelled",))
        self.assertTrue(any(event.get("kind") == "run.cancelled" for event in cancelled))
        run = self._call("runs.get", {"run_id": run_id})["result"]["run"]
        self.assertEqual(run["state"], "cancelled")

    def test_archived_usage_observation_reconciles_into_durable_query(self):
        run_id = self._start_run(self.registration_ids["usage"], model="usage")
        events = self._read_until(run_id, ("run.completed",))
        kinds = [event.get("kind") for event in events]
        self.assertIn("usage.observed", kinds)
        self.assertIn("run.completed", kinds)
        records = self._call("usage.query", {})["result"]["records"]
        matched = [record for record in records if record["run_id"] == run_id]
        self.assertEqual(len(matched), 1)
        record = matched[0]
        self.assertEqual(record["session_id"], events[0]["session_id"])
        self.assertEqual(record["units"], 7)
        self.assertEqual(record["unit_kind"], "input_tokens")
        validate_schema_ref("contracts/engine.v1/methods/usage.query.result.schema.json",
                            {"records": records})


if __name__ == "__main__":
    unittest.main()
