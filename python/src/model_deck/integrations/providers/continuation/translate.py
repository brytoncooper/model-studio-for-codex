"""Foreign-safe item translation extracted from local_router.py.

Preserves exact behavior of `local_item_id`, `_make_item_foreign_safe`,
`sanitize_openai_input`, and `heal_rejected_encrypted_item`. This module
mutates the item dicts passed to `_make_item_foreign_safe` in place, exactly
as the legacy router does. Pure provider helpers (`LOCAL_ITEM_PREFIX`,
`is_local_item_id`, `_item_identity`, `ContinuationError`) are extracted from
`provider_continuation.py` so this package has no legacy runtime imports.
"""
import copy
import hashlib
import json
import re

from . import compaction as _compaction

LOCAL_ITEM_PREFIX = "mdkc_"


class ContinuationError(ValueError):
    """Display-safe continuation failure; never includes stored reasoning or signatures."""


def is_local_item_id(item_id):
    return isinstance(item_id, str) and item_id.startswith(LOCAL_ITEM_PREFIX)


def _item_identity(item):
    """Compare the visible content before attaching its original provider metadata."""
    kind = item.get("type")
    if kind == "message":
        content = [{key: value for key, value in part.items() if key in ("type", "text", "image_url", "detail")}
                   for part in item.get("content") or [] if isinstance(part, dict)]
        return {"type": kind, "role": item.get("role"), "content": content}
    if kind == "function_call":
        arguments = item.get("arguments") or "{}"
        try:
            arguments = json.loads(arguments)
        except (ValueError, TypeError):
            pass
        return {"type": kind, "name": item.get("name"), "call_id": item.get("call_id"), "arguments": arguments}
    if kind == "reasoning":
        summary = copy.deepcopy(item.get("summary") or [])
        for part in item.get("content") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                summary.append({"type": "summary_text", "text": part["text"]})
        return {"type": kind, "summary": summary}
    raise ContinuationError("This provider returned an unsupported continuation item.")


def item_identity(item):
    """Return the stable provider-neutral identity used for scoped replay."""
    return _item_identity(item)


def local_item_id(original):
    """An id Codex keeps for streaming but drops before sending history to OpenAI.

    Codex forwards ids that contain an underscore (OpenAI's `rs_…`, `fc_…`, `msg_…`) and nulls the
    rest. Items produced by other providers must never be sent to OpenAI under such ids, because
    OpenAI then looks for reasoning or encrypted state it never produced.
    """
    if is_local_item_id(original):
        return original
    return "mdk-" + hashlib.sha256(str(original).encode("utf-8")).hexdigest()[:20]


def _make_item_foreign_safe(item):
    """Rewrite an OpenRouter output item so a later OpenAI turn cannot choke on it."""
    if not isinstance(item, dict):
        return
    if isinstance(item.get("id"), str) and item["id"]:
        item["id"] = local_item_id(item["id"])
    item.pop("encrypted_content", None)
    item.pop("encrypted_function_args", None)
    if item.get("type") == "reasoning":
        summary = [part for part in item.get("summary") or [] if isinstance(part, dict)]
        for part in item.get("content") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                summary.append({"type": "summary_text", "text": part["text"]})
        item["summary"] = summary
        item.pop("content", None)


def sanitize_openai_input(request_body):
    """Rewrite items OpenAI cannot use before a passthrough request. Returns (body, changed).

    OpenAI only makes use of reasoning items that carry its own `encrypted_content`; Codex always
    asks for it, so a reasoning item without one came from another provider (or is useless) and
    would make OpenAI reject the whole turn. A compaction item this router made for another
    provider's model becomes the plain summary message. The original bytes are forwarded when
    nothing changes.
    """
    if b'"reasoning"' not in request_body and b'mdkc_' not in request_body             and _compaction.MARKER_BYTES not in request_body:
        return request_body, 0
    try:
        request = json.loads(request_body)
    except (ValueError, TypeError):
        return request_body, 0
    if not isinstance(request, dict) or not isinstance(request.get("input"), list):
        return request_body, 0
    kept, dropped = [], 0
    for item in request["input"]:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        local_id = is_local_item_id(item.get("id"))
        if item.get("type") == "reasoning" and (local_id or not item.get("encrypted_content")):
            dropped += 1
            continue
        if item.get("type") == "compaction" and _compaction.decode(item.get("encrypted_content")):
            kept.append(_compaction.message_for_item(item))
            dropped += 1
            continue
        if local_id:
            item.pop("id", None)
            dropped += 1
        kept.append(item)
    if not dropped:
        return request_body, 0
    request["input"] = kept
    return json.dumps(request, separators=(",", ":")).encode("utf-8"), dropped


REJECTED_ITEM_PATTERN = re.compile(r"\b(rs_[A-Za-z0-9_-]+)")
REJECTED_INDEX_PATTERN = re.compile(r"input\[(\d+)\]")


def heal_rejected_encrypted_item(request_body, error_body):
    """Drop the reasoning item OpenAI rejected. Returns the new body, or None if not applicable.

    A task that ran on an OpenRouter model before this router made foreign items safe can carry a
    reasoning item OpenAI cannot decrypt or parse. OpenAI rejects the whole turn because of it, so
    the router removes exactly that item and retries instead of leaving the task stuck. Only
    reasoning items are ever removed; any other rejected item is a real error and is replayed.
    """
    text = error_body.decode("utf-8", "replace") if isinstance(error_body, bytes) else str(error_body)
    mentions_encrypted = "encrypted content" in text.lower()
    rejected_indexes = {int(index) for index in REJECTED_INDEX_PATTERN.findall(text)}
    if not mentions_encrypted and not rejected_indexes:
        return None
    try:
        request = json.loads(request_body)
    except (ValueError, TypeError):
        return None
    if not isinstance(request, dict) or not isinstance(request.get("input"), list):
        return None
    rejected_ids = set(REJECTED_ITEM_PATTERN.findall(text))

    def is_rejected(index, item):
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            return False
        if index in rejected_indexes or item.get("id") in rejected_ids:
            return True
        return mentions_encrypted and not rejected_ids and not rejected_indexes and bool(item.get("encrypted_content"))

    kept = [item for index, item in enumerate(request["input"]) if not is_rejected(index, item)]
    if len(kept) == len(request["input"]):
        return None
    request["input"] = kept
    return json.dumps(request, separators=(",", ":")).encode("utf-8")
