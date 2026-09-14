"""Authenticated loopback Responses API facade backed by the public engine API."""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import uuid
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.engine.runs.input_codec import normalized_messages_to_wire

from .input_normalization import CodexInputNormalizationError, normalize_codex_input
from .state import BridgeState
from .tool_conversion import ToolConversionError, convert_tools, restore_tool_identity


_CLIENT_NAME = "model-deck-v2-codex-bridge"
_TERMINAL_KINDS = frozenset({"run.completed", "run.failed", "run.cancelled", "run.interrupted"})


class EngineRPC:
    """Small authenticated client for the engine's public Unix JSON-RPC API."""

    def __init__(self, rendezvous_path: Path, credential_path: Path) -> None:
        self._rendezvous_path = Path(rendezvous_path)
        self._credential_path = Path(credential_path)

    def _authenticate(self, session: Any) -> None:
        descriptor = load_rendezvous_file(self._rendezvous_path)
        credential = self._credential_path.read_text(encoding="utf-8").strip()
        response = session.call({
            "jsonrpc": "2.0", "id": "authenticate", "method": "engine.v1.hello",
            "params": {
                "client_name": _CLIENT_NAME,
                "offered_api": {"major": 1, "minor": 0},
                "authentication": {
                    "engine_instance_id": descriptor.engine_instance_id,
                    "instance_nonce": descriptor.instance_nonce,
                    "credential": credential,
                },
            },
        })
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict) or result.get("authenticated") is not True:
            raise RuntimeError("engine authentication failed")

    def call(self, method: str, params: dict[str, Any]) -> Any:
        descriptor = load_rendezvous_file(self._rendezvous_path)
        client = UnixSocketEngineClient(descriptor.socket_path, timeout_seconds=30)
        with client.session() as session:
            self._authenticate(session)
            response = session.call({"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params})
        if "error" in response:
            raise RuntimeError("engine request failed")
        return response.get("result")

    def subscribe_events(self, run_id: str) -> Iterator[dict[str, Any]]:
        descriptor = load_rendezvous_file(self._rendezvous_path)
        client = UnixSocketEngineClient(descriptor.socket_path, timeout_seconds=600)
        with client.session() as session:
            self._authenticate(session)
            response = session.call({
                "jsonrpc": "2.0", "id": "subscribe", "method": "engine.v1.events.subscribe",
                "params": {"topics": [f"run:{run_id}"], "initial_credit": 256},
            })
            if "error" in response:
                raise RuntimeError("engine event subscription failed")
            while True:
                notification = session.read_notification()
                params = notification.get("params")
                event = params.get("event") if isinstance(params, dict) else None
                if isinstance(event, dict):
                    yield event


class _ResponsesStream:
    """Translate one engine run segment to Codex-compatible Responses events."""

    def __init__(self, run_id: str, model: str, aliases: Mapping[str, tuple[str | None, str]]) -> None:
        suffix = run_id.replace("-", "")
        self._response_id = f"resp_{suffix}"
        self._message_id = f"msg_{suffix}"
        self._model = model
        self._aliases = aliases
        self._message_started = False
        self._text_parts: list[str] = []
        self._output: list[dict[str, Any]] = []
        self._usage: dict[str, int] = {}

    def created(self) -> dict[str, Any]:
        return {"type": "response.created", "response": self._response("in_progress")}

    def translate(self, event: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        kind = event.get("kind")
        if kind == "content.delta":
            if event.get("channel") == "reasoning":
                return [], False
            delta = event.get("delta")
            if not isinstance(delta, str):
                return [], False
            result: list[dict[str, Any]] = []
            if not self._message_started:
                self._message_started = True
                message = {"id": self._message_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []}
                result.append({
                    "type": "response.output_item.added", "output_index": 0,
                    "item": message,
                })
                result.append({
                    "type": "response.content_part.added", "item_id": self._message_id,
                    "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                })
            self._text_parts.append(delta)
            result.append({
                "type": "response.output_text.delta", "item_id": self._message_id,
                "output_index": 0, "content_index": 0, "delta": delta,
            })
            return result, False
        if kind == "usage.observed":
            usage = event.get("usage")
            unit_kind = usage.get("unit_kind") if isinstance(usage, dict) else None
            units = usage.get("units") if isinstance(usage, dict) else None
            if isinstance(unit_kind, str) and isinstance(units, (int, float)):
                self._usage[unit_kind] = int(units)
            return [], False
        if kind == "tool.requested":
            tool_call = event.get("tool_call")
            if not isinstance(tool_call, dict):
                raise ValueError("engine tool event is invalid")
            call_id = tool_call.get("call_id")
            alias = tool_call.get("tool_name")
            arguments = tool_call.get("arguments")
            if not isinstance(call_id, str) or not isinstance(alias, str) or not isinstance(arguments, dict):
                raise ValueError("engine tool event is invalid")
            namespace, name = restore_tool_identity(alias, self._aliases)
            item: dict[str, Any] = {
                "id": f"fc_{call_id}", "type": "function_call", "call_id": call_id,
                "name": name, "arguments": json.dumps(arguments, separators=(",", ":"), allow_nan=False), "status": "completed",
            }
            if namespace is not None:
                item["namespace"] = namespace
            self._output = [item]
            return [
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                self.completed(),
            ], True
        if kind in _TERMINAL_KINDS:
            if kind == "run.completed":
                result: list[dict[str, Any]] = []
                if self._message_started:
                    text = "".join(self._text_parts)
                    message = {
                        "id": self._message_id, "type": "message", "role": "assistant",
                        "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                    self._output = [message]
                    result.extend([
                        {"type": "response.output_text.done", "item_id": self._message_id, "output_index": 0, "content_index": 0, "text": text},
                        {"type": "response.output_item.done", "output_index": 0, "item": message},
                    ])
                result.append(self.completed())
                return result, True
            return [{
                "type": "response.failed",
                "response": {**self._response("failed"), "error": {"code": str(kind), "message": "Model Deck run did not complete."}},
            }], True
        return [], False

    def completed(self) -> dict[str, Any]:
        return {"type": "response.completed", "response": self._response("completed")}

    def _response(self, status: str) -> dict[str, Any]:
        input_tokens = self._usage.get("input_tokens", 0)
        output_tokens = self._usage.get("output_tokens", 0)
        cached_tokens = self._usage.get("cached_tokens", 0)
        response = {
            "id": self._response_id, "object": "response", "status": status,
            "model": self._model, "output": list(self._output),
        }
        if self._usage:
            usage: dict[str, Any] = {
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
            if "cached_tokens" in self._usage:
                usage["input_tokens_details"] = {"cached_tokens": cached_tokens}
            response["usage"] = usage
        else:
            response["usage"] = None
        return response


class CodexResponsesBridge:
    """Own the loopback HTTP server and map Codex threads to engine sessions."""

    def __init__(self, *, rendezvous_path: Path, credential_path: Path, profile: Any, state_path: Path, token_path: Path, descriptor_path: Path) -> None:
        self.engine = EngineRPC(Path(rendezvous_path), Path(credential_path))
        self.profile = profile
        self.state = BridgeState(Path(state_path))
        self.token_path = Path(token_path)
        self.descriptor_path = Path(descriptor_path)
        self.token = secrets.token_urlsafe(32)
        self.registration_id: str | None = None
        self._server: ThreadingHTTPServer | None = None
        self._state_lock = threading.Lock()

    def _provision(self) -> str:
        connections = self.engine.call("engine.v1.connections.list", {}).get("connections", [])
        if not any(item.get("connection_id") == self.profile.connection_id for item in connections):
            self.engine.call("engine.v1.connections.save", {
                "expected_revision": 0,
                "idempotency_key": f"codex-connection-{self.profile.connection_id}",
                "connection": {
                    "connection_id": self.profile.connection_id, "provider_id": self.profile.provider_id,
                    "endpoint_config_ref": self.profile.endpoint_config_ref, "credential_ref": self.profile.credential_ref,
                },
            })
        models = self.engine.call("engine.v1.models.list", {"collection": "registered"}).get("items", [])
        for model in models:
            if model.get("connection_id") == self.profile.connection_id and model.get("provider_model_id") == self.profile.provider_model_id:
                return str(model["registration_id"])
        registered = self.engine.call("engine.v1.models.register", {
            "connection_id": self.profile.connection_id, "provider_model_id": self.profile.provider_model_id,
            "display_name": self.profile.display_name, "expected_revision": 0,
            "idempotency_key": f"codex-model-{self.profile.connection_id}",
        })
        return str(registered["model"]["registration_id"])

    def start(self) -> str:
        self.registration_id = self._provision()
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(self.token, encoding="utf-8")
        os.chmod(self.token_path, 0o600)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                bridge._handle(self)

            def log_message(self, _format: str, *args: object) -> None:
                del args

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = int(self._server.server_address[1])
        base_url = f"http://127.0.0.1:{port}/v1"
        descriptor = {
            "schema_version": 1, "base_url": base_url, "provider_id": self.profile.provider_id,
            "model": self.profile.provider_model_id, "billing_description": self.profile.billing_description,
            "token_path": str(self.token_path),
        }
        self._write_private_json(self.descriptor_path, descriptor)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        run_id: str | None = None
        resumed_call_id: str | None = None
        response_started = False
        stage = "request-routing"
        if handler.path != "/v1/responses":
            handler.send_error(404)
            return
        if handler.headers.get("Authorization") != f"Bearer {self.token}":
            handler.send_error(401)
            return
        try:
            stage = "request-body"
            body = self._read_body(handler)
            stage = "tool-conversion"
            tools, aliases = convert_tools(body.get("tools"))
            stage = "turn-metadata"
            metadata = json.loads(handler.headers.get("x-codex-turn-metadata", "{}"))
            thread_id = metadata.get("thread_id") if isinstance(metadata, dict) else None
            if not isinstance(thread_id, str) or not thread_id:
                raise ValueError("thread metadata is required")
            stage = "input-normalization"
            source_items = body.get("input", [])
            if not isinstance(source_items, list):
                raise ValueError("Responses input must be a list")
            instructions = body.get("instructions")
            if isinstance(instructions, str) and instructions:
                source_items = [
                    {"type": "message", "role": "developer", "content": instructions},
                    *source_items,
                ]
            normalized = normalize_codex_input(source_items, aliases)
            stage = "session-resolution"
            session_id = self._session_for_thread(thread_id)
            pending = self._matching_tool_result(thread_id, body.get("input", []))
            after_sequence: int | None = None
            if pending is not None:
                call_id, run_id, output, after_sequence = pending
                resumed_call_id = call_id
                self.engine.call("engine.v1.runs.submit_tool_result", {
                    "run_id": run_id, "call_id": call_id, "result": output,
                    "idempotency_key": f"codex-tool-{call_id}",
                })
            else:
                stage = "run-start"
                turn_id = metadata.get("turn_id")
                request_key = turn_id if isinstance(turn_id, str) and turn_id else str(uuid.uuid4())
                started = self.engine.call("engine.v1.runs.start", {
                    "session_id": session_id, "client_request_id": request_key[:64],
                    "idempotency_key": request_key[:128], "registration_id": self.registration_id,
                    "input": {"messages": normalized_messages_to_wire(normalized)}, "tools": tools,
                    "options": {"parallel_tool_calls": False},
                })
                run_id = str(started.get("run", started)["run_id"])
            stage = "event-stream"
            response_started = True
            disconnect_watch_stop = threading.Event()
            disconnect_watcher = threading.Thread(
                target=self._cancel_when_client_disconnects,
                args=(handler, run_id, disconnect_watch_stop),
                daemon=True,
            )
            disconnect_watcher.start()
            try:
                self._stream(
                    handler,
                    run_id,
                    aliases,
                    thread_id=thread_id,
                    after_sequence=after_sequence,
                    disconnect_watch_stop=disconnect_watch_stop,
                )
            finally:
                disconnect_watch_stop.set()
                disconnect_watcher.join(timeout=0.2)
            if resumed_call_id is not None:
                self._forget_pending(resumed_call_id)
        except (CodexInputNormalizationError, ToolConversionError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            print(f"Codex bridge rejected request at {stage}: {type(error).__name__}", file=sys.stderr, flush=True)
            if stage == "tool-conversion" and isinstance(locals().get("body"), dict):
                advertised = body.get("tools")
                if isinstance(advertised, list):
                    shapes = [
                        {
                            "type": item.get("type"),
                            "keys": sorted(item.keys()),
                            "nested_types": [nested.get("type") for nested in item.get("tools", []) if isinstance(nested, dict)],
                        }
                        for item in advertised
                        if isinstance(item, dict)
                    ]
                    print(f"Codex bridge tool shapes: {json.dumps(shapes, separators=(',', ':'))}", file=sys.stderr, flush=True)
            handler.send_error(400, "invalid Responses request")
        except (BrokenPipeError, ConnectionError, TimeoutError):
            if run_id is not None:
                self._cancel_once(run_id)
        except RuntimeError:
            if run_id is not None:
                self._cancel_once(run_id)
            if response_started:
                try:
                    self._write_event(handler, {
                        "type": "error",
                        "code": "model_deck_engine_error",
                        "message": "Model Deck could not continue the response.",
                    })
                except (BrokenPipeError, ConnectionError, OSError):
                    pass
            else:
                handler.send_error(502, "Model Deck engine request failed")

    def _stream(
        self,
        handler: BaseHTTPRequestHandler,
        run_id: str,
        aliases: Mapping[str, tuple[str | None, str]],
        *,
        thread_id: str,
        after_sequence: int | None,
        disconnect_watch_stop: threading.Event,
    ) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        translator = _ResponsesStream(run_id, self.profile.provider_model_id, aliases)
        self._write_event(handler, translator.created())
        segment_done = False
        for engine_event in self.engine.subscribe_events(run_id):
            if self._client_disconnected(handler):
                raise BrokenPipeError("Codex disconnected from the response stream")
            sequence = engine_event.get("sequence")
            if after_sequence is not None and isinstance(sequence, int) and sequence <= after_sequence:
                continue
            if engine_event.get("kind") == "tool.requested":
                call = engine_event.get("tool_call")
                if isinstance(call, dict) and isinstance(call.get("call_id"), str):
                    self._remember_pending(
                        call["call_id"],
                        run_id,
                        int(engine_event.get("sequence", 0)),
                        thread_id=thread_id,
                    )
            response_events, segment_done = translator.translate(engine_event)
            if segment_done:
                disconnect_watch_stop.set()
            for event in response_events:
                self._write_event(handler, event)
            if segment_done:
                break
        if not segment_done:
            self._write_event(handler, translator.completed())

    def _session_for_thread(self, thread_id: str) -> str:
        with self._state_lock:
            session_id = self.state.data["threads"].get(thread_id)
            if isinstance(session_id, str):
                return session_id
            if self.registration_id is None:
                raise RuntimeError("bridge is not started")
            created = self.engine.call("engine.v1.sessions.create", {"registration_id": self.registration_id})
            session_id = str(created["session_id"])
            self.state.data["threads"][thread_id] = session_id
            self.state.save()
            return session_id

    def _matching_tool_result(self, thread_id: str, items: Any) -> tuple[str, str, Any, int] | None:
        if not isinstance(items, list):
            return None
        with self._state_lock:
            for item in items:
                if not isinstance(item, dict) or item.get("type") != "function_call_output":
                    continue
                call_id = item.get("call_id")
                pending = self.state.data["pending"].get(call_id)
                if (
                    isinstance(call_id, str)
                    and isinstance(pending, dict)
                    and pending.get("thread_id") == thread_id
                ):
                    return call_id, str(pending["run_id"]), item.get("output"), int(pending["sequence"])
        return None

    def _forget_pending(self, call_id: str) -> None:
        with self._state_lock:
            self.state.data["pending"].pop(call_id, None)
            self.state.save()

    def _remember_pending(
        self,
        call_id: str,
        run_id: str,
        sequence: int,
        *,
        thread_id: str,
    ) -> None:
        with self._state_lock:
            self.state.data["pending"][call_id] = {
                "run_id": run_id,
                "sequence": sequence,
                "thread_id": thread_id,
            }
            self.state.save()

    def _cancel_once(self, run_id: str) -> None:
        try:
            self.engine.call("engine.v1.runs.cancel", {"run_id": run_id, "idempotency_key": f"codex-cancel-{run_id}"})
        except Exception:
            pass

    def _cancel_when_client_disconnects(
        self,
        handler: BaseHTTPRequestHandler,
        run_id: str,
        stop: threading.Event,
    ) -> None:
        while not stop.wait(0.05):
            if self._client_disconnected(handler):
                self._cancel_once(run_id)
                return

    @staticmethod
    def _read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        length = int(handler.headers.get("Content-Length", "0"))
        if length < 0 or length > 8 * 1024 * 1024:
            raise ValueError("request body is invalid")
        body = json.loads(handler.rfile.read(length))
        if not isinstance(body, dict):
            raise ValueError("request body is invalid")
        return body

    @staticmethod
    def _write_event(handler: BaseHTTPRequestHandler, event: dict[str, Any]) -> None:
        payload = json.dumps(event, separators=(",", ":"), allow_nan=False).encode("utf-8")
        handler.wfile.write(b"data: " + payload + b"\n\n")
        handler.wfile.flush()

    @staticmethod
    def _client_disconnected(handler: BaseHTTPRequestHandler) -> bool:
        try:
            return handler.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    @staticmethod
    def _write_private_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)


__all__ = ["CodexResponsesBridge", "EngineRPC"]
