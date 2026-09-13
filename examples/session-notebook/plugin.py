#!/usr/bin/env python3
"""Isolated Session Notebook worker using only the frozen plugin wire."""

from __future__ import annotations

import copy
import importlib.util
import json
import queue
import sys
import threading
import uuid
from typing import Any


PLUGIN_ID = "org.example.notebook"
PLUGIN_VERSION = "1.0.0"
FRAME_MAX_BYTES = 1_048_576
EXPORT_MAX_BYTES = 900_000
NOTE_KEY_PREFIX = "notes/"

CREATE_NOTE = "org.example.notebook.notes.create"
GET_NOTE = "org.example.notebook.notes.get"
LIST_NOTES = "org.example.notebook.notes.list"
UPDATE_NOTE = "org.example.notebook.notes.update"
DELETE_NOTE = "org.example.notebook.notes.delete"
PREVIEW_EXPORT = "org.example.notebook.export.preview"

STORAGE_GET = "plugin.v1.broker.storage.get"
STORAGE_LIST = "plugin.v1.broker.storage.list"
STORAGE_PUT = "plugin.v1.broker.storage.put"
STORAGE_DELETE = "plugin.v1.broker.storage.delete"

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


class NotebookInputError(ValueError):
    """An invocation does not match the notebook operation contract."""


class BrokerCallError(RuntimeError):
    """A broker request failed without exposing its private response."""


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
        raise NotebookInputError()
    if value is None or type(value) is bool or type(value) is int:
        return
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise NotebookInputError()
        return
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise NotebookInputError() from None
        return
    if type(value) is list:
        for child in value:
            _validate_json_value(child, depth=depth + 1, nodes=nodes)
        return
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                raise NotebookInputError()
            try:
                key.encode("utf-8")
            except UnicodeEncodeError:
                raise NotebookInputError() from None
            _validate_json_value(child, depth=depth + 1, nodes=nodes)
        return
    raise NotebookInputError()


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
    except NotebookInputError:
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
    except (NotebookInputError, TypeError, ValueError, UnicodeEncodeError, RecursionError):
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
        raise NotebookInputError()
    if not required.issubset(value) or set(value) - required - optional:
        raise NotebookInputError()
    return value


def _require_text(value: Any, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum:
        raise NotebookInputError()
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise NotebookInputError() from None
    return value


def _require_note_id(value: Any) -> str:
    text = _require_text(value, 64)
    try:
        uuid.UUID(text)
    except (ValueError, AttributeError):
        raise NotebookInputError() from None
    return text


def _require_revision(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise NotebookInputError()
    return value


def _note_key(note_id: str) -> str:
    return NOTE_KEY_PREFIX + note_id


def _note_for_output(value: Any, revision: Any) -> dict[str, Any]:
    note = _require_exact_object(
        value,
        required=frozenset({"note_id", "title", "body", "metadata"}),
    )
    note_id = _require_note_id(note["note_id"])
    title = _require_text(note["title"], 256)
    body = _require_text(note["body"], 65_536)
    _validate_json_value(note["metadata"])
    stored_revision = _require_revision(revision)
    return {
        "note_id": note_id,
        "title": title,
        "body": body,
        "metadata": copy.deepcopy(note["metadata"]),
        "revision": stored_revision,
    }


def format_notes_as_markdown(notes: list[dict[str, Any]]) -> str:
    """Return stable Markdown ordered by note identity."""

    ordered_notes = sorted(notes, key=lambda note: note["note_id"])
    lines = ["# Session Notebook"]
    for note in ordered_notes:
        title = note["title"] if note["title"] else "Untitled"
        lines.extend(("", f"## {title}", "", note["body"]))
    markdown = "\n".join(lines) + "\n"
    if len(markdown.encode("utf-8")) > EXPORT_MAX_BYTES:
        raise NotebookInputError()
    return markdown


class ProtocolChannel:
    """Read host requests while correlating nested broker responses."""

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
            name="session-notebook-stdio",
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
                if type(request_id) is not str or not request_id.startswith("notebook-broker-"):
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
            request_id = f"notebook-broker-{self._next_broker_request}"
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


class SessionNotebookWorker:
    """Serial operation dispatcher for one activated notebook process."""

    def __init__(self, channel: ProtocolChannel) -> None:
        self._channel = channel
        self._state = "created"
        self._activation_id: str | None = None
        self._allowed_broker_methods: frozenset[str] = frozenset()
        self._stopped = False

    def serve(self) -> None:
        while not self._stopped:
            request = self._channel.receive_request()
            response = self._response_for(request)
            self._channel.send(response)

    def _response_for(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        try:
            if request.get("jsonrpc") != "2.0" or "id" not in request:
                raise NotebookInputError()
            method = request.get("method")
            if type(method) is not str:
                raise NotebookInputError()
            params = request.get("params", {})
            if type(params) is not dict:
                raise NotebookInputError()
            canonical_method = RUNTIME_ALIASES.get(method, method)
            result = self._dispatch(canonical_method, params)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except NotebookInputError:
            return self._error(request_id, -32602, "invalid notebook request")
        except BrokerCallError:
            return self._error(request_id, -32000, "notebook storage unavailable")
        except Exception:
            return self._error(request_id, -32603, "notebook operation failed")

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
        raise NotebookInputError()

    def _hello(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state != "created":
            raise NotebookInputError()
        hello = _require_exact_object(
            params,
            required=frozenset({"offered_api", "nonce"}),
        )
        api = _require_exact_object(
            hello["offered_api"],
            required=frozenset({"major", "minor"}),
        )
        if (
            type(api["major"]) is not int
            or api["major"] != 1
            or type(api["minor"]) is not int
            or api["minor"] < 0
        ):
            raise NotebookInputError()
        _require_text(hello["nonce"], 128)
        self._state = "hello_verified"
        return {
            "plugin_id": PLUGIN_ID,
            "plugin_version": PLUGIN_VERSION,
            "capabilities": [
                "notebook.manual-crud",
                "fixture.isolated-imports",
            ],
        }

    def _activate(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state != "hello_verified":
            raise NotebookInputError()
        activation = _require_exact_object(
            params,
            required=frozenset({"activation_token", "allowed_broker_methods"}),
            optional=frozenset({"config_revision"}),
        )
        token = activation["activation_token"]
        methods = activation["allowed_broker_methods"]
        if type(token) is not str or not token or len(token) > 512:
            raise NotebookInputError()
        if type(methods) is not list or len(methods) > 64:
            raise NotebookInputError()
        if any(type(method) is not str or not method or len(method) > 128 for method in methods):
            raise NotebookInputError()
        if len(methods) != len(set(methods)):
            raise NotebookInputError()
        if "config_revision" in activation and (
            type(activation["config_revision"]) is not int
            or activation["config_revision"] < 0
        ):
            raise NotebookInputError()
        self._allowed_broker_methods = frozenset(methods)
        self._activation_id = str(uuid.uuid4())
        self._state = "active"
        return {
            "activation_id": self._activation_id,
            "invocation_handle_prefix": "notebook:",
        }

    def _invoke(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state != "active" or self._activation_id is None:
            raise NotebookInputError()
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
                {
                    "activation_id",
                    "plugin_id",
                    "invocation_handle",
                    "revocation_generation",
                }
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
            raise NotebookInputError()
        output = self._invoke_operation(
            operation_id,
            invocation_input,
            broker_context["invocation_handle"],
        )
        return {"output": output}

    def _invoke_operation(
        self,
        operation_id: str,
        invocation_input: Any,
        invocation_handle: str,
    ) -> dict[str, Any]:
        if operation_id == CREATE_NOTE:
            return self._create_note(invocation_input, invocation_handle)
        if operation_id == GET_NOTE:
            return self._get_note(invocation_input, invocation_handle)
        if operation_id == LIST_NOTES:
            _require_exact_object(invocation_input, required=frozenset())
            return {"notes": self._list_notes(invocation_handle)}
        if operation_id == UPDATE_NOTE:
            return self._update_note(invocation_input, invocation_handle)
        if operation_id == DELETE_NOTE:
            return self._delete_note(invocation_input, invocation_handle)
        if operation_id == PREVIEW_EXPORT:
            _require_exact_object(invocation_input, required=frozenset())
            notes = self._list_notes(invocation_handle)
            return {
                "markdown": format_notes_as_markdown(notes),
                "note_count": len(notes),
            }
        raise NotebookInputError()

    def _call_storage(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if method not in self._allowed_broker_methods:
            raise BrokerCallError()
        return self._channel.call_broker(method, params)

    @staticmethod
    def _storage_params(invocation_handle: str) -> dict[str, Any]:
        return {
            "invocation_handle": invocation_handle,
            "namespace": PLUGIN_ID,
        }

    def _create_note(
        self,
        invocation_input: Any,
        invocation_handle: str,
    ) -> dict[str, Any]:
        values = _require_exact_object(
            invocation_input,
            required=frozenset({"title", "body"}),
            optional=frozenset({"metadata"}),
        )
        note_id = str(uuid.uuid4())
        note = {
            "note_id": note_id,
            "title": _require_text(values["title"], 256),
            "body": _require_text(values["body"], 65_536),
            "metadata": copy.deepcopy(values.get("metadata", {})),
        }
        _validate_json_value(note["metadata"])
        params = self._storage_params(invocation_handle)
        params.update(
            {
                "key": _note_key(note_id),
                "value": note,
                "expected_revision": 0,
            }
        )
        stored = self._call_storage(STORAGE_PUT, params)
        return _note_for_output(note, stored.get("revision"))

    def _load_note(self, note_id: str, invocation_handle: str) -> dict[str, Any]:
        params = self._storage_params(invocation_handle)
        params["key"] = _note_key(note_id)
        stored = self._call_storage(STORAGE_GET, params)
        if set(stored) != {"value", "revision"}:
            raise BrokerCallError()
        note = _note_for_output(stored["value"], stored["revision"])
        if note["note_id"] != note_id:
            raise BrokerCallError()
        return note

    def _get_note(
        self,
        invocation_input: Any,
        invocation_handle: str,
    ) -> dict[str, Any]:
        values = _require_exact_object(
            invocation_input,
            required=frozenset({"note_id"}),
        )
        return self._load_note(_require_note_id(values["note_id"]), invocation_handle)

    def _list_notes(self, invocation_handle: str) -> list[dict[str, Any]]:
        params = self._storage_params(invocation_handle)
        params.update({"prefix": NOTE_KEY_PREFIX, "limit": 200})
        listed = self._call_storage(STORAGE_LIST, params)
        if set(listed) != {"items"} or type(listed["items"]) is not list:
            raise BrokerCallError()
        notes: list[dict[str, Any]] = []
        for item in listed["items"]:
            entry = _require_exact_object(
                item,
                required=frozenset({"key", "revision"}),
            )
            key = entry["key"]
            if type(key) is not str or not key.startswith(NOTE_KEY_PREFIX):
                raise BrokerCallError()
            note_id = key.removeprefix(NOTE_KEY_PREFIX)
            note = self._load_note(_require_note_id(note_id), invocation_handle)
            if note["revision"] != entry["revision"]:
                raise BrokerCallError()
            notes.append(note)
        return notes

    def _update_note(
        self,
        invocation_input: Any,
        invocation_handle: str,
    ) -> dict[str, Any]:
        values = _require_exact_object(
            invocation_input,
            required=frozenset(
                {"note_id", "expected_revision", "title", "body"}
            ),
            optional=frozenset({"metadata"}),
        )
        note_id = _require_note_id(values["note_id"])
        expected_revision = _require_revision(values["expected_revision"])
        current = self._load_note(note_id, invocation_handle)
        note = {
            "note_id": note_id,
            "title": _require_text(values["title"], 256),
            "body": _require_text(values["body"], 65_536),
            "metadata": copy.deepcopy(
                values["metadata"] if "metadata" in values else current["metadata"]
            ),
        }
        _validate_json_value(note["metadata"])
        params = self._storage_params(invocation_handle)
        params.update(
            {
                "key": _note_key(note_id),
                "value": note,
                "expected_revision": expected_revision,
            }
        )
        stored = self._call_storage(STORAGE_PUT, params)
        return _note_for_output(note, stored.get("revision"))

    def _delete_note(
        self,
        invocation_input: Any,
        invocation_handle: str,
    ) -> dict[str, Any]:
        values = _require_exact_object(
            invocation_input,
            required=frozenset({"note_id", "expected_revision"}),
        )
        note_id = _require_note_id(values["note_id"])
        expected_revision = _require_revision(values["expected_revision"])
        self._load_note(note_id, invocation_handle)
        params = self._storage_params(invocation_handle)
        params.update(
            {
                "key": _note_key(note_id),
                "expected_revision": expected_revision,
            }
        )
        deleted = self._call_storage(STORAGE_DELETE, params)
        if set(deleted) != {"deleted"} or type(deleted["deleted"]) is not bool:
            raise BrokerCallError()
        return {"deleted": deleted["deleted"]}

    def _heartbeat(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state != "active" or self._activation_id is None:
            raise NotebookInputError()
        heartbeat = _require_exact_object(
            params,
            required=frozenset({"activation_id"}),
        )
        if heartbeat["activation_id"] != self._activation_id:
            raise NotebookInputError()
        return {"alive": True}

    def _cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state != "active":
            raise NotebookInputError()
        _require_exact_object(params, required=frozenset({"job_id"}))
        _require_note_id(params["job_id"])
        return {"accepted": False}

    def _drain(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state != "active":
            raise NotebookInputError()
        drain = _require_exact_object(
            params,
            required=frozenset({"deadline_ms"}),
        )
        deadline_ms = drain["deadline_ms"]
        if type(deadline_ms) is not int or not 1 <= deadline_ms <= 60_000:
            raise NotebookInputError()
        self._state = "inactive"
        return {"drained": True}

    def _deactivate(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state not in {"active", "inactive"}:
            raise NotebookInputError()
        _require_exact_object(params, required=frozenset())
        self._state = "deactivated"
        self._stopped = True
        return {"deactivated": True}


def main() -> None:
    if not sys.flags.isolated or importlib.util.find_spec("model_deck") is not None:
        raise RuntimeError("Session Notebook requires isolated private imports")
    channel = ProtocolChannel()
    channel.start()
    SessionNotebookWorker(channel).serve()


if __name__ == "__main__":
    main()
