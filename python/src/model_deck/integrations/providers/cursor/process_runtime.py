"""Thin adapter for an injected legacy Cursor process; no SDK or root imports."""
from __future__ import annotations

import copy
import queue
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from model_deck.engine.runs.ports import (
    CancelProviderRunResult, ProviderCancelTerminationStatus,
    SubmitToolResultProviderOutcome, SubmitToolResultProviderResult,
)
from .coordinator import CursorSdkEvent, CursorStartRequest


class CursorProcessPort(Protocol):
    events: queue.Queue
    def tool_result(self, call_id: str, output: Any) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True, repr=False)
class PreparedCursorRun:
    payload: Mapping[str, Any]
    tool_aliases: Mapping[str, str]

    def __repr__(self) -> str:
        return "PreparedCursorRun()"


class CursorProcessRuntime:
    def __init__(self, *, process_factory: Callable[[Mapping[str, Any]], CursorProcessPort],
                 prepare_payload: Callable[[CursorStartRequest], PreparedCursorRun],
                 normalize_usage: Callable[[CursorStartRequest, tuple[dict, ...], dict | None], Iterable[Mapping]]) -> None:
        self._process_factory = process_factory
        self._prepare_payload = prepare_payload
        self._normalize_usage = normalize_usage

    def start(self, request: CursorStartRequest, on_event: Callable[[CursorSdkEvent], None]):
        process = None
        try:
            if request.continuation_handle is not None:
                raise ValueError("continuation unavailable")
            prepared = self._prepare_payload(request)
            payload = copy.deepcopy(dict(prepared.payload))
            aliases = dict(prepared.tool_aliases)
            authorized = {tool.name for tool in request.tools if tool.host_execution_required}
            if any(not isinstance(wire, str) or not wire or host not in authorized
                   for wire, host in aliases.items()):
                raise ValueError("invalid tool aliases")
            process = self._process_factory(payload)
            session = _CursorProcessSession(process, request, aliases, on_event, self._normalize_usage)
            session.start()
            return session
        except Exception:
            if process is not None:
                threading.Thread(target=_close_process, args=(process,), daemon=True).start()
            raise RuntimeError("Cursor process could not start") from None


def _close_process(process):
    try:
        process.close()
    except Exception:
        pass


class _CursorProcessSession:
    def __init__(self, process, request, aliases, on_event, normalize_usage):
        self._process = process
        self._request = request
        self._aliases = aliases
        self._on_event = on_event
        self._normalize_usage = normalize_usage
        self._lock = threading.Lock()
        self._closing = False
        self._terminal = False
        self._started = False
        self._pending_call = None
        self._submitting = False
        self._submission_done = threading.Event()
        self._submission_done.set()
        self._usage = []
        self._cleanup_done = threading.Event()

    def __repr__(self):
        return "CursorProcessSession()"

    def start(self):
        threading.Thread(target=self._pump, daemon=True).start()

    def close(self):
        self._begin_close()

    def _begin_close(self, *, require_nonterminal=False):
        with self._lock:
            if self._closing or (require_nonterminal and self._terminal):
                return False
            self._closing = True
        threading.Thread(target=self._cleanup, daemon=True).start()
        return True

    def _cleanup(self):
        try:
            _close_process(self._process)
        finally:
            self._cleanup_done.set()

    def request_cancel(self, *, deadline: str):
        accepted = self._begin_close(require_nonterminal=True)
        return CancelProviderRunResult(accepted, ProviderCancelTerminationStatus.UNKNOWN)

    def submit_tool_result(self, call_id, result):
        rejected = SubmitToolResultProviderResult(SubmitToolResultProviderOutcome.REJECTED)
        with self._lock:
            if self._terminal or self._closing or self._submitting or call_id != self._pending_call:
                return rejected
            self._submitting = True
            self._submission_done.clear()
        try:
            self._process.tool_result(call_id, copy.deepcopy(result))
        except Exception:
            return rejected
        else:
            with self._lock:
                self._pending_call = None
            return SubmitToolResultProviderResult(SubmitToolResultProviderOutcome.ACCEPTED)
        finally:
            with self._lock:
                self._submitting = False
                self._submission_done.set()

    def _emit(self, kind, payload=None):
        self._on_event(CursorSdkEvent(kind, payload))

    def _finish(self, kind, final=None):
        # Usage normalization is application-injected, preserving the existing
        # intermediate-sum/final-replacement rule without copying legacy logic.
        if self._started:
            records = self._normalize_usage(self._request, tuple(self._usage), final)
            for index, record in enumerate(records):
                if index >= 64:
                    raise ValueError("usage record limit")
                self._emit("usage.observed", {"usage": copy.deepcopy(dict(record))})
        with self._lock:
            if self._terminal:
                return
            if self._closing:
                kind = "run.interrupted"
            self._terminal = True
        try:
            self._emit(kind, {"terminal_result": {"outcome": kind.split(".", 1)[1]}})
        finally:
            self.close()

    def _pump(self):
        try:
            while True:
                with self._lock:
                    closing = self._closing
                if closing:
                    if self._cleanup_done.wait(0.05):
                        self._finish("run.interrupted")
                        return
                    continue
                try:
                    event = self._process.events.get(timeout=0.05)
                except queue.Empty:
                    continue
                # tool_result may synchronously make the next SDK event ready.
                # Settle this adapter's forwarding outcome before interpreting it.
                while not self._submission_done.wait(0.05):
                    with self._lock:
                        if self._closing:
                            break
                with self._lock:
                    if self._closing:
                        continue
                if not isinstance(event, dict):
                    raise ValueError("invalid process event")
                kind = event.get("type")
                if kind == "error":
                    self._finish("run.failed", event)
                    return
                if kind == "started":
                    if self._started:
                        raise ValueError("duplicate started")
                    self._started = True
                    self._emit("run.started")
                elif not self._started:
                    raise ValueError("event before SDK started")
                elif kind in ("text", "thinking"):
                    text = event.get("text")
                    if not isinstance(text, str):
                        raise ValueError("invalid text")
                    for offset in range(0, len(text), 65536):
                        self._emit("content.delta", {"channel": "text" if kind == "text" else "reasoning",
                                                    "delta": text[offset:offset + 65536]})
                elif kind == "tool_call":
                    call_id, name = event.get("call_id"), event.get("name")
                    if not isinstance(call_id, str) or not call_id or len(call_id) > 128 or name not in self._aliases:
                        raise ValueError("invalid tool call")
                    if "arguments" not in event:
                        raise ValueError("tool arguments are missing")
                    with self._lock:
                        if self._pending_call is not None:
                            raise ValueError("tool call already outstanding")
                        self._pending_call = call_id
                    self._emit("tool.requested", {"tool_call": {"call_id": call_id,
                               "tool_name": self._aliases[name], "arguments": copy.deepcopy(event["arguments"])}})
                elif kind == "usage":
                    if len(self._usage) >= 1024:
                        raise ValueError("usage observation limit")
                    self._usage.append(copy.deepcopy(event))
                elif kind == "done":
                    with self._lock:
                        pending = self._pending_call is not None
                    self._finish("run.completed" if event.get("status") == "finished" and not pending else "run.interrupted", event)
                    return
                else:
                    raise ValueError("unknown process event")
        except Exception:
            # Do not propagate child/vendor/callback exception text into events.
            with self._lock:
                terminal = self._terminal
                self._terminal = True
            try:
                if not terminal:
                    self._emit("run.failed", {"terminal_result": {"outcome": "failed"}})
            except Exception:
                pass
            finally:
                self.close()
