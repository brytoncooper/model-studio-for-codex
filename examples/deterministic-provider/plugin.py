"""Standalone deterministic provider fixture. Standard library and stdio only."""
import importlib.util
import json
import sys
import uuid
from datetime import datetime, timezone

PLUGIN_ID = "org.example.deterministic-provider"
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
    if run["mode"] == "foreign-handle":
        params["adapter_run_handle"] = str(uuid.uuid4())
    if run["mode"] == "malformed":
        event["unexpected"] = True
    send({"jsonrpc": "2.0", "method": "plugin.v1.provider.event", "params": params})


def main():
    # The acceptance harness invokes -I from the extracted archive directory.
    # Refuse a host environment exposing the private engine package.
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
            if len(runs) >= 256:
                raise ValueError("fixture run bound")
            captured = params["run_request"]
            mode = params["route_snapshot"]["provider_model_id"]
            handle = str(uuid.uuid4())
            run = {"handle": handle, "run_id": captured["run_id"], "session_id": captured["session_id"],
                   "sequence": 0, "mode": mode, "tool": None}
            runs[handle] = run
            reply(request, {"adapter_run_handle": handle})
            if mode == "crash":
                return
            emit(run, "run.started")
            if mode == "text":
                emit(run, "content.delta", channel="text", delta="external deterministic text")
                emit(run, "run.completed", terminal_result={"outcome": "completed"})
            elif mode == "tool":
                run["tool"] = "fixture-call"
                emit(run, "tool.requested", tool_call={"call_id": run["tool"],
                     "tool_name": captured["tools"][0]["name"], "arguments": {"query": "fixture"}})
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
            confirmed = run["mode"] != "cancel-unconfirmed"
            reply(request, {"accepted": True, "confirmed": confirmed})
            if confirmed:
                emit(run, "run.cancelled", terminal_result={"outcome": "cancelled"})
        elif method == "plugin.v1.provider.ack" and activation:
            reply(request, {"credit": 1})
        else:
            raise ValueError("fixture method unsupported")


if __name__ == "__main__":
    main()
