"""Context compaction for models the router serves itself.

Codex compacts a long thread by asking its backend for a `compaction` item: it sends the whole
history plus a `compaction_trigger` item (or posts the history to `/responses/compact`) and then
keeps only the returned item, whose `encrypted_content` only that backend can read back. OpenRouter,
custom endpoints and Cursor have no such backend, so the router runs Codex's own summarization
prompt on the same model and returns a compaction item that carries the summary itself. Later
requests turn that item back into the summary message Codex uses for local compaction.
"""
import base64
import json
import os
import time

# Codex's own prompt and summary prefix (rust-v0.148.0, codex-rs/prompts/templates/compact/).
SUMMARIZATION_PROMPT = (
    "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM "
    "that will resume the task.\n\nInclude:\n- Current progress and key decisions made\n"
    "- Important context, constraints, or user preferences\n"
    "- What remains to be done (clear next steps)\n"
    "- Any critical data, examples, or references needed to continue\n\n"
    "Be concise, structured, and focused on helping the next LLM seamlessly continue the work.")
SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary of its thinking "
    "process. You also have access to the state of the tools that were used by that language model. "
    "Use this to build on the work that has already been done and avoid duplicating work. Here is the "
    "summary produced by the other language model, use the information in this summary to assist with "
    "your own analysis:")
FOREIGN_NOTE = ("An earlier part of this conversation was compacted by a different provider and that summary "
                "cannot be read here. Ask the user to restate anything essential from before this point.")
MARKER = "mdk-compaction-v1:"
MARKER_BYTES = MARKER.encode("ascii")
MAX_SUMMARY_CHARS = 200_000


def has_trigger(request):
    """True when Codex appended a compaction_trigger item to this turn's input."""
    return isinstance(request, dict) and any(
        isinstance(item, dict) and item.get("type") == "compaction_trigger" for item in request.get("input") or [])


def summarization_request(request):
    """The same conversation as an ordinary turn whose last user message asks for the handoff summary.

    Tools are withheld: the summary must come back as text, and a routed model that called a tool
    here could not have its call executed.
    """
    prepared = {key: value for key, value in request.items()
                if key not in ("tools", "tool_choice", "parallel_tool_calls")}
    prepared["input"] = [item for item in request.get("input") or []
                         if not (isinstance(item, dict) and item.get("type") == "compaction_trigger")]
    prepared["input"].append({"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": SUMMARIZATION_PROMPT}]})
    prepared["stream"] = True
    return prepared


def is_summarization_request(request):
    """True when the last input item is the router's own summarization prompt."""
    items = request.get("input") or [] if isinstance(request, dict) else []
    last = items[-1] if items and isinstance(items[-1], dict) else {}
    content = last.get("content") if last.get("type") == "message" and last.get("role") == "user" else None
    return isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict) \
        and content[0].get("text") == SUMMARIZATION_PROMPT


def encode(summary, model=None):
    """Opaque `encrypted_content` that carries the summary; only this router reads it back."""
    document = {"version": 1, "summary": summary[:MAX_SUMMARY_CHARS], "model": model, "created": int(time.time())}
    raw = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return MARKER + base64.urlsafe_b64encode(raw).decode("ascii")


def decode(encrypted_content):
    """The summary document behind a router-made compaction item, or None for any other content."""
    if not isinstance(encrypted_content, str) or not encrypted_content.startswith(MARKER):
        return None
    try:
        document = json.loads(base64.urlsafe_b64decode(encrypted_content[len(MARKER):].encode("ascii")))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(document, dict) or not isinstance(document.get("summary"), str):
        return None
    return document


def compaction_item(encrypted_content):
    # No underscore in the id: Codex only forwards underscore ids (OpenAI's own) to OpenAI.
    return {"type": "compaction", "id": "cmp-" + os.urandom(8).hex(), "encrypted_content": encrypted_content}


def summary_message(summary):
    return {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": f"{SUMMARY_PREFIX}\n{summary}"}]}


def message_for_item(item):
    """The user message a model should see in place of a compaction item in its history."""
    record = decode(item.get("encrypted_content")) if isinstance(item, dict) else None
    if record is None:
        return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": FOREIGN_NOTE}]}
    return summary_message(record["summary"])


def summary_from_events(events):
    """Assistant text from the summarization turn's Responses events, or '' when it produced none."""
    texts = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "response.output_item.done":
            continue
        item = event.get("item") or {}
        if item.get("type") == "message" and item.get("role", "assistant") == "assistant":
            texts.append("".join(part.get("text", "") for part in item.get("content") or []
                                 if isinstance(part, dict) and part.get("type") == "output_text"))
    if not any(texts):
        texts = [event.get("delta", "") for event in events
                 if isinstance(event, dict) and event.get("type") == "response.output_text.delta"]
    return "".join(text for text in texts if isinstance(text, str)).strip()


def response_events(item, usage=None):
    """The streamed response Codex expects from a compaction turn: exactly one compaction item."""
    response_id = "resp-" + os.urandom(8).hex()
    completed = {"id": response_id, "status": "completed", "output": [item]}
    if isinstance(usage, dict):
        completed["usage"] = usage
    return [{"type": "response.created", "response": {"id": response_id}},
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": completed}]
