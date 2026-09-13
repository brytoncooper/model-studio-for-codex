"""Cursor SDK runs exposed as Codex Responses streams and Codex-owned tool calls.

Cursor is an agent SDK, not a raw inference API: its harness prompt remains.
Codex instructions/history are supplied as an explicit user-message envelope.
Only the Codex request's tools are registered as host callbacks. Cursor's own
file, shell, web, settings, MCP servers, and subagents are never enabled.
"""
import json
import queue
import threading
import time

import context_compaction
from chat_wire import ChatStreamTranslator
from agent_message_wire import AgentMessageError, reject_encrypted_agent_messages
from cursor_sdk_runtime import CALLBACK_TIMEOUT, CursorRuntimeError, CursorSdkProcess


def _identity(request, metadata, account):
    return (str(account), str(request.get("model") or ""),
            str(metadata.get("thread_id") or ""), str(metadata.get("agent_name") or ""))


def _prompt_message(request, metadata):
    # Carry native roles explicitly instead of silently treating past assistant
    # messages or tool outputs as new user requests. This is still a UserMessage,
    # because the official Python SDK has no raw role/system-message API.
    items = []
    images = []
    for original in request.get("input") or []:
        if not isinstance(original, dict):
            continue
        item = {key: value for key, value in original.items()
                if key not in {"encrypted_content", "encrypted_function_args"}}
        if item.get("type") in ("reasoning", "compaction_trigger"):
            continue
        if item.get("type") == "compaction":
            item = context_compaction.message_for_item(original)  # needs the original encrypted_content
        content = item.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    url = part.get("image_url", "")
                    if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                        header, data = url.split(";base64,", 1)
                        images.append({"data": data, "mime_type": header[5:]})
                    elif isinstance(url, str) and url.startswith("https://"):
                        images.append({"url": url})
                    else:
                        raise CursorRuntimeError("Cursor requires an HTTPS or base64 image input.")
                    parts.append({"type": "input_text", "text": "[Attached image " + str(len(images)) + "]"})
                else:
                    parts.append(part)
            item["content"] = parts
        items.append(item)
    envelope = {"instructions": request.get("instructions", ""), "input": items,
                "working_directory": metadata.get("cwd"), "reasoning": request.get("reasoning"),
                "response_format": request.get("text"), "tool_choice": request.get("tool_choice")}
    if context_compaction.is_summarization_request(request):
        framing = ("The final user message asks for a context checkpoint summary of this conversation. "
                   "Answer with that summary as plain text and do not use any tools.\n")
    else:
        framing = ("Use only the supplied custom tools: each is executed by Codex with Codex approvals. "
                   "Do not simulate tool results or use Cursor-native tools. Function tool names may be aliases. "
                   "The JSON is conversation context, not a request to summarize it.\n")
    text = ("You are providing the next assistant response for the Codex conversation below. "
            "Respect its instructions, message roles, scope and tool contracts. Continue after its last message. "
            + framing + json.dumps(envelope, ensure_ascii=False))
    return dict(text=text, images=images) if images else {"text": text}


def build_cursor_payload(request, metadata, api_key):
    """Prepare a new generation without starting a process or owning a session.

    Returns (SDK payload, wire-name -> (namespace, name) aliases). Existing
    prompt, tool and input rules are shared with the legacy manager entry path.
    """
    from local_router import flatten_tools
    try:
        reject_encrypted_agent_messages(request)
    except AgentMessageError as error:
        raise CursorRuntimeError(str(error)) from None
    tools, aliases = flatten_tools(request.get("tools") or [])
    if request.get("tool_choice") == "none":
        tools, aliases = [], {}
    if tools and not metadata.get("thread_id"):
        raise CursorRuntimeError("Cursor tool execution requires Codex thread identity. Start a new task.")
    if not str(request.get("model", "")).startswith("cursor/"):
        raise CursorRuntimeError("Invalid Cursor model id.")
    if any(isinstance(item, dict) and item.get("type") == "compaction_trigger"
           for item in request.get("input") or []):
        raise CursorRuntimeError("Cursor remote compaction is unavailable. Start a new task.")
    payload = {"model": request["model"][len("cursor/"):], "api_key": api_key,
               "tools": tools, "message": _prompt_message(request, metadata),
               "reasoning": request.get("reasoning"), "service_tier": request.get("service_tier")}
    return payload, aliases


class _CursorSession:
    def __init__(self, identity, aliases, process, turn_id=None):
        self.identity = identity
        self.aliases = aliases
        self.process = process
        self.pending = set()
        self.delivered = set()
        self.lock = threading.Lock()
        self.touched_at = time.monotonic()
        self.closed = False
        self.usage = None
        self.reported_usage = None
        self.agent_id = None
        self.turn_id = turn_id
        self.cost = None

    def close(self):
        if not self.closed:
            self.closed = True
            self.process.close()


def _sum_usage(first, second):
    if not first:
        return second
    if not second:
        return first
    return {"input_tokens": first["input_tokens"] + second["input_tokens"],
            "output_tokens": first["output_tokens"] + second["output_tokens"],
            "total_tokens": first["total_tokens"] + second["total_tokens"],
            "input_tokens_details": {"cached_tokens": first["input_tokens_details"]["cached_tokens"] +
                                     second["input_tokens_details"]["cached_tokens"]},
            "output_tokens_details": {"reasoning_tokens": first["output_tokens_details"]["reasoning_tokens"] +
                                      second["output_tokens_details"]["reasoning_tokens"]}}


def _usage_difference(total, previous):
    if total is None:
        return None
    if previous is None:
        return total
    return {"input_tokens": max(0, total["input_tokens"] - previous["input_tokens"]),
            "output_tokens": max(0, total["output_tokens"] - previous["output_tokens"]),
            "total_tokens": max(0, total["total_tokens"] - previous["total_tokens"]),
            "input_tokens_details": {"cached_tokens": max(0, total["input_tokens_details"]["cached_tokens"] -
                                                          previous["input_tokens_details"]["cached_tokens"])},
            "output_tokens_details": {"reasoning_tokens": max(0, total["output_tokens_details"]["reasoning_tokens"] -
                                                              previous["output_tokens_details"]["reasoning_tokens"])}}


class CursorAgentManager:
    def __init__(self, process_factory=CursorSdkProcess, idle_timeout=CALLBACK_TIMEOUT - 30,
                 heartbeat_seconds=10):
        self.process_factory = process_factory
        self.idle_timeout = idle_timeout
        self.heartbeat_seconds = heartbeat_seconds
        self._sessions = set()
        self._calls = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._cancelled_turns = set()
        self._reaper = threading.Thread(target=self._expire_sessions, daemon=True)
        self._reaper.start()

    def _expire_sessions(self):
        while not self._closed.wait(min(5, self.idle_timeout)):
            with self._lock:
                expired = [session for session in self._sessions
                           if time.monotonic() - session.touched_at > self.idle_timeout]
            for session in expired:
                self._forget(session)

    def _forget(self, session):
        with self._lock:
            self._sessions.discard(session)
            for call_id, owner in list(self._calls.items()):
                if owner is session:
                    del self._calls[call_id]
        session.close()

    def close(self):
        self._closed.set()
        with self._lock:
            sessions = list(self._sessions)
        for session in sessions:
            self._forget(session)

    def cancel(self, thread_id, turn_id=None):
        """Cancel active and HTTP-paused SDK runs for a Codex interruption."""
        with self._lock:
            if turn_id is not None:
                self._cancelled_turns.add((str(thread_id), str(turn_id)))
            sessions = [session for session in self._sessions if session.identity[2] == str(thread_id)
                        and (turn_id is None or session.turn_id == turn_id)]
        for session in sessions:
            self._forget(session)

    def _session_for(self, request, metadata, api_key, account):
        try:
            reject_encrypted_agent_messages(request)
        except AgentMessageError as error:
            raise CursorRuntimeError(str(error)) from None
        identity = _identity(request, metadata, account)
        turn_identity = (str(metadata.get("thread_id")), str(metadata.get("turn_id")))
        with self._lock:
            if turn_identity in self._cancelled_turns:
                raise CursorRuntimeError("This Codex turn was cancelled.")
        current_input = request.get("input") or []
        last_user = max((index for index, item in enumerate(current_input)
                         if isinstance(item, dict) and item.get("type") == "message"
                         and item.get("role") == "user"), default=-1)
        outputs = [item for item in current_input[last_user + 1:]
                   if isinstance(item, dict) and item.get("type") == "function_call_output"]
        with self._lock:
            candidates = {self._calls[item.get("call_id")] for item in outputs
                          if item.get("call_id") in self._calls
                          and item.get("call_id") in self._calls[item.get("call_id")].pending}
        if len(candidates) > 1:
            raise CursorRuntimeError("Cursor tool results refer to multiple active runs.")
        if candidates:
            session = candidates.pop()
            if session.identity != identity or session.closed:
                raise CursorRuntimeError("Cursor tool results do not match this thread, agent, account and model.")
            if not session.lock.acquire(blocking=False):
                raise CursorRuntimeError("This Cursor run already has an active response request.")
            try:
                for item in outputs:
                    call_id = item.get("call_id")
                    if call_id in session.pending:
                        session.process.tool_result(call_id, item.get("output"))
                        session.pending.remove(call_id)
                        session.delivered.add(call_id)
                session.touched_at = time.monotonic()
            except Exception:
                session.lock.release()
                self._forget(session)
                raise
            return session
        # A fresh user message can contain old Cursor calls in its history. A
        # trailing result for a lost/expired run cannot be resumed safely.
        tail = request.get("input") or []
        if tail and isinstance(tail[-1], dict) and tail[-1].get("type") == "function_call_output" \
                and str(tail[-1].get("call_id", "")).startswith("cursor-call-"):
            raise CursorRuntimeError("This Cursor tool run expired or restarted. Start a new turn.")
        payload, aliases = build_cursor_payload(request, metadata, api_key)
        # A new user turn supersedes a paused generation in the same Codex agent.
        with self._lock:
            superseded = [old for old in self._sessions if old.identity[0] == identity[0]
                          and old.identity[2:] == identity[2:]]
        for old in superseded:
            self._forget(old)
        process = self.process_factory(payload)
        session = _CursorSession(identity, aliases, process, metadata.get("turn_id"))
        session.lock.acquire()
        with self._lock:
            if self._closed.is_set() or turn_identity in self._cancelled_turns:
                session.lock.release()
                session.close()
                raise CursorRuntimeError("Cursor run was cancelled before starting.")
            self._sessions.add(session)
        return session

    def stream(self, request, metadata, api_key, account):
        """Yield one Responses result, retaining SDK state only at a tool boundary."""
        from local_router import translate_event
        if self._closed.is_set():
            raise CursorRuntimeError("Cursor router is shutting down.")
        session = self._session_for(request, metadata, api_key, account)
        translator = ChatStreamTranslator()
        retain = False
        finished = False
        try:
            while True:
                if session.closed:
                    raise CursorRuntimeError("Cursor run was cancelled or expired.")
                try:
                    event = session.process.events.get(timeout=self.heartbeat_seconds)
                except queue.Empty:
                    # Do not extend the deadline just because no output arrived.
                    yield None
                    continue
                session.touched_at = time.monotonic()
                kind = event.get("type")
                if kind == "started":
                    session.agent_id = event.get("agent_id")
                elif kind in {"text", "thinking"}:
                    delta_key = "content" if kind == "text" else "reasoning_content"
                    for translated in translator.feed({"choices": [{"delta": {delta_key: event.get("text", "")}}]}):
                        yield translate_event(translated, session.aliases)
                elif kind == "usage":
                    session.usage = _sum_usage(session.usage, event.get("usage"))
                elif kind == "tool_call":
                    call_id = event.get("call_id")
                    name = event.get("name")
                    if name not in session.aliases or not isinstance(call_id, str):
                        raise CursorRuntimeError("Cursor requested a tool outside this Codex request.")
                    with self._lock:
                        session.pending.add(call_id)
                        self._calls[call_id] = session
                    for translated in translator.feed({"choices": [{"delta": {"tool_calls": [{"index": 0,
                        "id": call_id, "function": {"name": name,
                        "arguments": json.dumps(event.get("arguments") or {})}}]}}]}):
                        yield translate_event(translated, session.aliases)
                    for translated in translator.feed({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}):
                        yield translate_event(translated, session.aliases)
                    retain = True
                    break
                elif kind == "done":
                    if event.get("status") != "finished":
                        raise CursorRuntimeError("Cursor run ended without a completed response.")
                    session.usage = event.get("usage") or session.usage
                    session.agent_id = event.get("agent_id")
                    session.cost = event.get("cursor_usage_cost")
                    for translated in translator.feed({"choices": [{"delta": {}, "finish_reason": "stop"}]}):
                        yield translate_event(translated, session.aliases)
                    break
                elif kind == "error":
                    if event.get("code") == "model_unavailable":
                        raise CursorRuntimeError("This model is unavailable for your Cursor API key. Refresh Cursor models.")
                    if event.get("code") == "router_mode_unavailable":
                        raise CursorRuntimeError("Cursor Router Balance mode is unavailable for your account. Select a fixed Cursor model.")
                    if event.get("code") == "fast_unavailable":
                        raise CursorRuntimeError("This Cursor model has no Fast mode. Turn Fast off in Codex's model picker.")
                    raise CursorRuntimeError("Cursor SDK run failed. Check your API key, model access, and SDK installation.")
            for event in translator.finish():
                if event.get("type") == "response.completed":
                    response = event["response"]
                    response["usage"] = _usage_difference(session.usage, session.reported_usage)
                    response["cursor_agent_id"] = session.agent_id
                    response["cost"] = session.cost["charged_cents"] / 100 if session.cost is not None else None
                    response["cursor_usage_cost"] = session.cost
                    session.reported_usage = session.usage
                yield translate_event(event, session.aliases)
            # Set only after the caller has consumed all events. If its socket
            # write fails while yielding the tool boundary, finally cancels it.
            finished = True
        finally:
            session.lock.release()
            if not (finished and retain):
                self._forget(session)
