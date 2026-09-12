"""Loopback model router for Codex. Every request is routed by its model name.

OpenAI models keep chatgpt.com authentication, so the ChatGPT subscription pays.
Collaboration tool messages use a plain-text handoff for mixed-provider agents.
Models registered in Model Deck are re-authenticated with the OpenRouter key and translated
to OpenRouter's stateless Responses API, so OpenRouter credits pay. Unregistered models fail
with a clear error instead of a silent billing choice. Credentials are never logged.
"""
import gzip
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.parse

try:
    from compression import zstd
except ImportError:  # Python before 3.14: request compression is disabled by the bridge instead.
    zstd = None

import context_compaction
from chat_wire import ChatStreamTranslator, chat_request_from_responses
from provider_continuation import (ContinuationError, ProviderContinuationStore, continuation_scope,
                                   is_local_item_id, response_item_id)
from provider_connections import provider_for_base_url
from agent_message_wire import (AgentMessageError, mark_plaintext_agent_call,
                                prepare_request, reject_encrypted_agent_messages, restore_event)
import pricing
from model_benchmarks import BenchmarkStore
from spawn_benchmarks import annotate_spawn_tools, load_benchmark_lines, model_choice_lines
from routing_registry import RoutingRegistry, endpoint_description, friendly_model_name, support_directory

CODEX_BACKEND_PREFIX = "/backend-api/codex"
CHATGPT_UPSTREAM = ("chatgpt.com", 443, True)
OPENROUTER_UPSTREAM = ("openrouter.ai", 443, True)
OPENROUTER_RESPONSES_PATH = "/api/v1/responses"
HOP_BY_HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "keep-alive",
                      "proxy-authorization", "proxy-connection", "te", "trailer", "upgrade"}
SENSITIVE_HEADERS = {"authorization", "chatgpt-account-id", "cookie", "x-oai-attestation", "proxy-authorization"}
# Only tools supplied by Codex are advertised. Namespace flattening changes their wire names,
# while Codex continues to own execution, approvals, and each tool's availability.
DEFAULT_FUNCTION_NAMESPACE = "functions"
MAX_TOOL_NAME_LENGTH = 64
EFFORT_MAP = {"none": "minimal", "minimal": "minimal", "low": "low", "medium": "medium",
              "high": "high", "xhigh": "high", "ultra": "high", "max": "high"}
# Codex's speed tiers mapped to OpenRouter's documented model-variant shortcuts:
# ":nitro" sorts providers by throughput and allows priority endpoints; ":floor" sorts by price.
SERVICE_TIER_VARIANTS = {"priority": "nitro", "fast": "nitro", "flex": "floor"}
CURSOR_SERVICE_TIERS = [
    {"id": "priority", "name": "Fast", "description": "Cursor's fast mode for this model. Uses more of your Cursor allowance."},
]
CURSOR_CATALOG_MAX_AGE = 24 * 3600  # seconds before the router re-lists Cursor's models at startup
OPENROUTER_SERVICE_TIERS = [
    {"id": "priority", "name": "Fast", "description": "Fastest available OpenRouter provider (nitro routing). Can cost more."},
    {"id": "flex", "name": "Flex", "description": "Cheapest available OpenRouter provider (floor routing). Can be slower."},
]
CATALOG_TEMPLATE_SLUG = "gpt-5.5"
ROUTER_ERROR_RESPONSE_ID = "resp_model_deck_router"


class RouterError(Exception):
    """A routing decision that must surface to the user as a clear error."""


# ---------------------------------------------------------------------------
# Routing decisions


def route_for_model(model, registered_models):
    if not model:
        return "openai"
    if model in registered_models:
        return "endpoint"
    if "/" in model:
        return "unregistered"
    return "openai"


def endpoint_for(entry):
    """The endpoint summary for a registry entry; derived from its provider table when absent."""
    if isinstance(entry.get("endpoint"), dict):
        return entry["endpoint"]
    provider = entry["config"]["model_providers"]["openrouter-settings"]
    return RoutingRegistry.endpoint_summary(provider, {})


# ---------------------------------------------------------------------------
# Request translation for OpenRouter


def _normalized_namespace(namespace):
    if namespace in (None, "", DEFAULT_FUNCTION_NAMESPACE):
        return None
    return namespace


def _function_spec(tool):
    spec = {"type": "function", "name": tool.get("name", ""), "description": tool.get("description", ""),
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}}}
    if isinstance(tool.get("strict"), bool):
        spec["strict"] = tool["strict"]
    return spec


def _mangled_tool_name(namespace, name, attempt=0):
    identity = json.dumps([namespace, name, attempt], separators=(",", ":"))
    suffix = "__" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return name[:MAX_TOOL_NAME_LENGTH - len(suffix)] + suffix


def flatten_tools(tools):
    """Return OpenRouter-compatible function tools and a map of alias -> (namespace, name)."""
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        nested = tool.get("tools") or [] if tool.get("type") == "namespace" else [tool]
        if any(isinstance(spec, dict) and spec.get("type") in ("custom", "custom_tool") for spec in nested):
            raise RouterError("This endpoint cannot use Codex's freeform tools yet. Select a model profile with function tools.")
    plain = [tool for tool in tools if isinstance(tool, dict) and tool.get("type") == "function"]
    namespaces = [tool for tool in tools if isinstance(tool, dict) and tool.get("type") == "namespace"
                  and isinstance(tool.get("name"), str) and tool["name"]]
    flat, alias_map = [], {}
    for tool in plain:
        spec = _function_spec(tool)
        if spec["name"] and spec["name"] not in alias_map:
            alias_map[spec["name"]] = (None, spec["name"])
            flat.append(spec)
    for namespace in namespaces:
        for nested in namespace.get("tools") or []:
            if not isinstance(nested, dict) or nested.get("type") != "function" or not nested.get("name"):
                continue
            spec = _function_spec(nested)
            alias = spec["name"]
            identity = (_normalized_namespace(namespace["name"]), nested["name"])
            if identity in alias_map.values():
                continue
            if alias in alias_map:
                alias = _mangled_tool_name(namespace["name"], spec["name"])
            attempt = 0
            while alias in alias_map:
                attempt += 1
                alias = _mangled_tool_name(namespace["name"], spec["name"], attempt)
            spec["name"] = alias
            alias_map[alias] = identity
            flat.append(spec)
    return flat, alias_map


def _content_parts(content):
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}] if content else []
    parts = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("input_text", "output_text") and isinstance(part.get("text"), str):
            parts.append({"type": kind, "text": part["text"]})
        elif kind == "input_image" and part.get("image_url"):
            image = {"type": "input_image", "image_url": part["image_url"]}
            if part.get("detail"):
                image["detail"] = part["detail"]
            parts.append(image)
    return parts


def _function_output(output):
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts = _content_parts(output)
        if all(part["type"] == "input_text" for part in parts):
            return "\n".join(part["text"] for part in parts)
        return parts
    if isinstance(output, dict) and isinstance(output.get("content"), (str, list)):
        return _function_output(output["content"])
    return json.dumps(output) if output is not None else ""


def translate_input(items, alias_map, preserve_continuation=False):
    """Keep only items the stateless OpenRouter Responses API understands."""
    reverse = {value: alias for alias, value in alias_map.items()}
    translated = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        start = len(translated)
        if kind == "message":
            parts = _content_parts(item.get("content"))
            if parts:
                translated.append({"type": "message", "role": item.get("role", "user"), "content": parts})
        elif kind == "function_call":
            key = (_normalized_namespace(item.get("namespace")), item.get("name", ""))
            translated.append({"type": "function_call", "name": reverse.get(key, item.get("name", "")),
                               "arguments": item.get("arguments") or "{}", "call_id": item.get("call_id", "")})
        elif kind == "function_call_output":
            translated.append({"type": "function_call_output", "call_id": item.get("call_id", ""),
                               "output": _function_output(item.get("output"))})
        elif kind == "agent_message":
            text = "".join(part.get("text", "") for part in item.get("content") or []
                           if isinstance(part, dict) and part.get("type") == "input_text")
            if text:
                translated.append({"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": f"Message from agent {item.get('author', 'unknown')}:\n{text}"}]})
        elif kind == "reasoning" and preserve_continuation and is_local_item_id(item.get("id")):
            translated.append({"type": "reasoning", "summary": item.get("summary") or []})
        elif kind == "compaction":
            # Our own compaction item carries the summary; another provider's cannot be read here.
            translated.append(context_compaction.message_for_item(item))
            start = len(translated)
        elif kind in ("custom_tool_call", "custom_tool_call_output"):
            raise RouterError("This task contains freeform tool history that this endpoint cannot replay yet.")
        if preserve_continuation and len(translated) > start and is_local_item_id(item.get("id")):
            translated[-1]["id"] = item["id"]
        # Reasoning, compaction, additional_tools, custom tool calls, and other Codex-only
        # items are dropped: OpenRouter is stateless and cannot use them.
    return translated


def openrouter_model_for_request(model, service_tier):
    """Apply Codex's Fast/Flex choice as an OpenRouter routing variant on the model id."""
    variant = SERVICE_TIER_VARIANTS.get(str(service_tier or "").lower())
    if not variant or ":" in str(model):
        return model
    return f"{model}:{variant}"


def translate_request(body, openrouter=True, preserve_continuation=False):
    """Build the endpoint request from a Codex Responses request. Returns (request, alias_map)."""
    model = body.get("model")
    if openrouter:
        model = openrouter_model_for_request(model, body.get("service_tier"))
    request = {"model": model, "stream": True}
    if isinstance(body.get("instructions"), str) and body["instructions"]:
        request["instructions"] = body["instructions"]
    tools, alias_map = flatten_tools(body.get("tools") or [])
    if tools:
        request["tools"] = tools
        choice = body.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") == "function":
            identity = (_normalized_namespace(choice.get("namespace")), choice.get("name", ""))
            name = next((alias for alias, target in alias_map.items() if target == identity), choice.get("name", ""))
            choice = {"type": "function", "name": name}
        request["tool_choice"] = choice if isinstance(choice, (str, dict)) else "auto"
        request["parallel_tool_calls"] = bool(body.get("parallel_tool_calls", True))
    reasoning = body.get("reasoning") if isinstance(body.get("reasoning"), dict) else {}
    effort = EFFORT_MAP.get(reasoning.get("effort")) if openrouter else reasoning.get("effort")
    if effort:
        request["reasoning"] = {"effort": effort}
    text = body.get("text") if isinstance(body.get("text"), dict) else {}
    if text.get("format"):
        request["text"] = {"format": text["format"]}
    if "max_output_tokens" in body:
        limit = body["max_output_tokens"]
        if type(limit) is not int or limit <= 0:
            raise RouterError("The requested output token limit must be a positive integer.")
        request["max_output_tokens"] = limit
    request["input"] = translate_input(body.get("input"), alias_map, preserve_continuation)
    return request, alias_map


# ---------------------------------------------------------------------------
# Stream translation from OpenRouter


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
    # Only OpenAI can decrypt OpenAI reasoning blobs, and OpenAI rejects reasoning items whose
    # `content` list is non-empty. Keep the visible summary only.
    item.pop("encrypted_content", None)
    item.pop("encrypted_function_args", None)
    if item.get("type") == "reasoning":
        summary = [part for part in item.get("summary") or [] if isinstance(part, dict)]
        for part in item.get("content") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                summary.append({"type": "summary_text", "text": part["text"]})
        item["summary"] = summary
        item.pop("content", None)


def translate_event(event, alias_map, continuation=None, scope=None, response_nonce=""):
    """Map flattened tool names back to Codex namespaces and make items foreign-safe.

    Returns None for events to drop.
    """
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        return None
    if isinstance(event.get("item_id"), str) and event["item_id"]:
        event["item_id"] = (response_item_id(scope, event["item_id"], response_nonce) if continuation is not None
                            and not is_local_item_id(event["item_id"]) else local_item_id(event["item_id"]))

    def preserve_provider_item(item, completed):
        if continuation is None or not isinstance(item, dict) or is_local_item_id(item.get("id")):
            return
        if item.get("type") not in ("message", "reasoning", "function_call"):
            raise ContinuationError("The provider returned a tool or output type that this native adapter does not support.")
        item["id"] = (continuation.capture_response_item(scope, item, response_nonce) if completed else
                      response_item_id(scope, item.get("id"), response_nonce))
    if event["type"] in ("response.output_item.added", "response.output_item.done"):
        item = event.get("item")
        preserve_provider_item(item, event["type"] == "response.output_item.done")
        _make_item_foreign_safe(item)
        if isinstance(item, dict) and item.get("type") == "function_call":
            namespace, name = alias_map.get(item.get("name"), (None, item.get("name")))
            item["name"] = name
            if namespace:
                item["namespace"] = namespace
            mark_plaintext_agent_call(item)
    response = event.get("response")
    if isinstance(response, dict) and isinstance(response.get("output"), list):
        for item in response["output"]:
            preserve_provider_item(item, event["type"] == "response.completed")
            _make_item_foreign_safe(item)
            if isinstance(item, dict) and item.get("type") == "function_call":
                namespace, name = alias_map.get(item.get("name"), (None, item.get("name")))
                item["name"] = name
                if namespace:
                    item["namespace"] = namespace
                mark_plaintext_agent_call(item)
    return event


def sanitize_openai_input(request_body):
    """Rewrite items OpenAI cannot use before a passthrough request. Returns (body, changed).

    OpenAI only makes use of reasoning items that carry its own `encrypted_content`; Codex always
    asks for it, so a reasoning item without one came from another provider (or is useless) and
    would make OpenAI reject the whole turn. A compaction item this router made for another
    provider's model becomes the plain summary message. The original bytes are forwarded when
    nothing changes.
    """
    if b'"reasoning"' not in request_body and b'mdkc_' not in request_body \
            and context_compaction.MARKER_BYTES not in request_body:
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
        if item.get("type") == "compaction" and context_compaction.decode(item.get("encrypted_content")):
            # OpenAI cannot decrypt a summary this router made; give it the plain summary instead.
            kept.append(context_compaction.message_for_item(item))
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


def failed_event(message, code="invalid_prompt"):
    return {"type": "response.failed", "response": {"id": ROUTER_ERROR_RESPONSE_ID,
                                                     "error": {"code": code, "message": message}}}


def encode_event(event):
    return f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode("utf-8")


def openrouter_error_message(status, body, endpoint="OpenRouter"):
    detail = ""
    try:
        parsed = json.loads(body)
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        elif isinstance(error, str):
            detail = error
    except (ValueError, TypeError, AttributeError):
        detail = body.decode("utf-8", "replace")[:300] if isinstance(body, bytes) else ""
    if status in (401, 403):
        return f"{endpoint} rejected the API key (HTTP {status}). Check the key in Model Deck. {detail}".strip(), "invalid_prompt"
    if status == 402:
        return f"{endpoint} reports insufficient credits (HTTP 402). {detail}".strip(), "invalid_prompt"
    if status == 404:
        return f"{endpoint} does not know this model or route (HTTP 404). {detail}".strip(), "invalid_prompt"
    if status == 429:
        return f"{endpoint} rate limit reached (HTTP 429). {detail}".strip(), "rate_limit_exceeded"
    if status >= 500:
        return f"{endpoint} server error (HTTP {status}). {detail}".strip(), "server_error"
    return f"{endpoint} rejected the request (HTTP {status}). {detail}".strip(), "invalid_prompt"


def split_base_url(base_url):
    """('host', port, secure, '/path/prefix') for an endpoint base URL."""
    parts = urllib.parse.urlsplit(base_url)
    secure = parts.scheme == "https"
    return parts.hostname, parts.port or (443 if secure else 80), secure, parts.path.rstrip("/")


class SseParser:
    """Incremental server-sent-events parser that yields (comment_line | event_json)."""

    def __init__(self):
        self.buffer = b""

    def feed(self, chunk):
        self.buffer += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in self.buffer:
            block, self.buffer = self.buffer.split(b"\n\n", 1)
            yield from self._parse_block(block)

    def flush(self):
        block, self.buffer = self.buffer, b""
        yield from self._parse_block(block)

    @staticmethod
    def _parse_block(block):
        data_lines = []
        for line in block.split(b"\n"):
            if line.startswith(b":"):
                yield ("comment", line)
            elif line.startswith(b"data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            return
        data = b"\n".join(data_lines)
        if data == b"[DONE]":
            return
        try:
            yield ("event", json.loads(data))
        except ValueError:
            return


# ---------------------------------------------------------------------------
# Catalog injection: registered OpenRouter models become native picker entries


def cursor_offers_fast(model, cursor_fast_models):
    """Whether a Cursor model's picker entry gets a Fast toggle.

    `cursor_fast_models` is the set of cursor/<id> whose SDK catalog exposes the `fast` parameter,
    or None before any catalog has been cached: then every Cursor model offers Fast and the SDK
    broker refuses it explicitly for a model that lacks it.
    """
    return cursor_fast_models is None or model in cursor_fast_models


def inject_catalog(raw_json, registered_models, display_names=None, price_table=None, cursor_fast_models=None):
    """Append registered models to the Codex /models catalog, with OpenRouter list prices. Returns bytes."""
    document = json.loads(raw_json)
    models = document.get("models") if isinstance(document, dict) else None
    if not isinstance(models, list) or not models or not registered_models:
        return raw_json
    template = next((entry for entry in models if entry.get("slug") == CATALOG_TEMPLATE_SLUG), None)
    if template is None:
        listed = [entry for entry in models if entry.get("visibility") == "list"]
        template = max(listed or models, key=lambda entry: entry.get("priority", 0))
    existing = {entry.get("slug") for entry in models}
    for slug, registered in registered_models.items():
        if slug in existing:
            continue
        endpoint = endpoint_for(registered) if isinstance(registered, dict) and "config" in registered else {"openrouter": True}
        if endpoint.get("openrouter"):
            service_tiers = OPENROUTER_SERVICE_TIERS
        elif (endpoint.get("cursor") or endpoint.get("wire") == "cursor") and cursor_offers_fast(slug, cursor_fast_models):
            service_tiers = CURSOR_SERVICE_TIERS
        else:
            service_tiers = []
        price = pricing.pricing_for(slug, price_table or {}) if endpoint.get("openrouter") else None
        context = price.get("context") if price else None
        modalities = [m for m in (price.get("modalities") if price else None) or [] if m in ("text", "image")] or ["text"]
        entry = json.loads(json.dumps(template))
        entry.pop("guardian", None)
        entry.update({
            "slug": slug,
            "display_name": (display_names or {}).get(slug) or friendly_model_name(slug),
            "description": endpoint_description(endpoint, pricing.price_line(price)),
            "visibility": "list", "priority": 50, "supported_in_api": True,
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [{"effort": "low", "description": "Low reasoning"},
                                           {"effort": "medium", "description": "Medium reasoning"},
                                           {"effort": "high", "description": "High reasoning"}],
            "shell_type": "unified_exec", "apply_patch_tool_type": None, "supports_search_tool": False,
            "web_search_tool_type": "text", "use_responses_lite": False, "tool_mode": "direct",
            "multi_agent_version": "v2", "multi_agent_reasoning_effort": "low", "model_messages": None,
            "input_modalities": modalities,
            "context_window": context or 256000, "max_context_window": context or 256000,
            "prefer_websockets": False,
            "service_tiers": json.loads(json.dumps(service_tiers)),
            "additional_speed_tiers": ["fast"] if service_tiers else [],
            "default_service_tier": None, "upgrade": None, "availability_nux": None, "comp_hash": None,
            "supports_reasoning_summary_parameter": False, "default_reasoning_summary": "none",
            "support_verbosity": False, "default_verbosity": None, "experimental_supported_tools": [],
            "supports_experimental_context": False, "supports_image_detail_original": False,
        })
        models.append(entry)
    return json.dumps(document).encode("utf-8")


def injected_etag(etag, fingerprint="deck"):
    """OpenAI's catalog etag plus a fingerprint of the registered models.

    Codex refetches its catalog whenever the etag on a turn's response differs from the one it
    has cached, so the same value goes on both. A change to the registered models or their names
    changes the fingerprint, and Codex reloads the picker on the next turn without a restart.
    """
    if not etag:
        return None
    suffix = f"-{fingerprint}"
    return etag[:-1] + suffix + '"' if etag.endswith('"') else etag + suffix


# ---------------------------------------------------------------------------
# OpenRouter key retrieval through the Keychain helper


class KeychainKeyProvider:
    """Runs the registered credential command per key account. Keys live only in memory."""

    def __init__(self, registry):
        self.registry = registry
        self.cache = {}
        self.lock = threading.Lock()

    def key_for(self, model):
        entry = self.registry.load_models().get(model)
        if entry is None:
            raise RouterError(f"Model {model} is not registered in Model Deck.")
        auth = entry["config"]["model_providers"]["openrouter-settings"].get("auth")
        if auth is None:
            raise RouterError(f"Model {model} runs on an endpoint without a saved key.")
        account = auth["args"][1]
        with self.lock:
            cached = self.cache.get(account)
            if cached and cached[1] > time.monotonic():
                return cached[0]
            try:
                completed = subprocess.run([auth["command"], *auth["args"]], stdin=subprocess.DEVNULL,
                                           capture_output=True, timeout=auth["timeout_ms"] / 1000)
            except (OSError, subprocess.TimeoutExpired):
                raise RouterError("The endpoint's API key could not be read from the Keychain. "
                                  "Open Model Deck → Endpoints → Test to authorize it.") from None
            key = completed.stdout.decode("utf-8", "replace").strip()
            if completed.returncode != 0 or not key:
                raise RouterError("The endpoint's API key could not be read from the Keychain. "
                                  "Open Model Deck → Endpoints → Test to authorize it.")
            self.cache[account] = (key, time.monotonic() + auth["refresh_interval_ms"] / 1000)
            return key


# ---------------------------------------------------------------------------
# The HTTP server


def _redacted_headers(headers):
    return {name: ("<present>" if name.lower() in SENSITIVE_HEADERS else value) for name, value in headers}


def _decode_body(body, encoding):
    encoding = (encoding or "").lower()
    if not body or not encoding or encoding == "identity":
        return body
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "zstd" and zstd is not None:
        return zstd.decompress(body)
    raise RouterError(f"Unsupported request encoding: {encoding}")


def _turn_metadata(headers):
    try:
        metadata = json.loads(headers.get("x-codex-turn-metadata") or "{}")
    except ValueError:
        metadata = {}
    return {key: metadata.get(key) for key in ("thread_id", "agent_name", "turn_id")} if isinstance(metadata, dict) else {}


class RouterHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_any()

    def do_POST(self):
        self.handle_any()

    def do_PUT(self):
        self.handle_any()

    def do_DELETE(self):
        self.handle_any()

    def do_PATCH(self):
        self.handle_any()

    @property
    def router(self):
        return self.server.router

    def handle_any(self):
        try:
            self._handle()
        except (RouterError, AgentMessageError, ContinuationError) as error:
            self._send_failed(str(error))
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception:
            self.router.log("router request failed unexpectedly")
            try:
                self._send_failed("Model Deck router failed while handling this request. Check the router log.")
            except Exception:
                pass

    def _handle(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            # Codex falls back to HTTP streaming for the session when the upgrade is refused.
            self._send_empty(426)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        body = _decode_body(raw_body, self.headers.get("Content-Encoding"))
        request = None
        if body and (self.headers.get("Content-Type") or "").startswith("application/json"):
            try:
                request = json.loads(body)
            except ValueError:
                request = None
        model = request.get("model") if isinstance(request, dict) else None
        registered = self.router.registered_models()
        route = route_for_model(model, registered)
        metadata = _turn_metadata(self.headers)
        cwd = self.router.thread_directory(metadata.get("thread_id"))
        if cwd:
            metadata["cwd"] = cwd
        self.router.log(f"{self.command} {self.path} model={model} route={route} "
                        f"agent={metadata.get('agent_name')} subagent={self.headers.get('x-openai-subagent')}")
        if route == "unregistered":
            raise RouterError(f"Model {model} is not registered in Model Deck. Add it under Models, then try again.")
        if route == "endpoint":
            self._route_endpoint(model, registered[model], request, metadata)
        else:
            self._route_openai(raw_body, model, metadata)

    # --- OpenAI passthrough -------------------------------------------------

    def _route_openai(self, raw_body, model, metadata):
        host, port, secure = self.router.chatgpt_upstream
        headers = {name: value for name, value in self.headers.items() if name.lower() not in HOP_BY_HOP_HEADERS}
        headers["Host"] = host if port in (80, 443) else f"{host}:{port}"
        if raw_body:
            headers["Content-Length"] = str(len(raw_body))
        is_catalog = self.path.startswith(CODEX_BACKEND_PREFIX + "/models")
        if is_catalog:
            headers["Accept-Encoding"] = "identity"
            self.router.catalog_requested.set()
        plaintext_agents = False
        native_stream_request = False
        body_is_identity = (self.headers.get("Content-Encoding") or "identity").lower() == "identity"
        if (self.command == "POST" and raw_body and self.path.rstrip("/") == CODEX_BACKEND_PREFIX + "/responses"
                and self.router.registered_models()):
            prepared = _decode_body(raw_body, self.headers.get("Content-Encoding"))
            try:
                document = json.loads(prepared)
            except ValueError:
                document = None
            choices_added = annotate_spawn_tools(document, self.router.model_choices())
            if choices_added:
                prepared = json.dumps(document).encode("utf-8")
            prepared, plaintext_agents = prepare_request(prepared)
            if plaintext_agents or choices_added:
                raw_body = prepared
                native_stream_request = json.loads(prepared).get("stream") is True
                headers = {name: value for name, value in headers.items()
                           if name.lower() not in {"content-encoding", "accept-encoding"}}
                headers["Content-Length"] = str(len(raw_body))
                headers["Accept-Encoding"] = "identity"
                body_is_identity = True
        if self.command == "POST" and raw_body:
            decoded_body = raw_body if body_is_identity else _decode_body(raw_body, self.headers.get("Content-Encoding"))
            sanitized_body, dropped = sanitize_openai_input(decoded_body)
            if dropped:
                raw_body = sanitized_body
                headers = {name: value for name, value in headers.items() if name.lower() != "content-encoding"}
                body_is_identity = True
                headers["Content-Length"] = str(len(raw_body))
                self.router.log(f"removed {dropped} foreign reasoning item(s) or local continuation identifiers before forwarding")
        heal_attempts = 0
        while True:
            connection = (http.client.HTTPSConnection if secure else http.client.HTTPConnection)(host, port, timeout=600)
            try:
                connection.request(self.command, self.path, body=raw_body or None, headers=headers)
                response = connection.getresponse()
            except OSError as error:
                self.router.log(f"upstream {host} unreachable: {error.__class__.__name__}")
                self._send_json(502, {"error": {"message": "Model Deck router could not reach chatgpt.com."}})
                return
            if response.status < 400 or self.command != "POST" or heal_attempts >= 3 \
                    or not body_is_identity:
                break
            error_body = response.read()
            connection.close()
            healed = heal_rejected_encrypted_item(raw_body, error_body)
            if healed is None:
                self._replay_error(response, error_body)
                self.router.record({"route": "openai", "model": model, "status": response.status, **metadata})
                return
            heal_attempts += 1
            raw_body = healed
            headers["Content-Length"] = str(len(raw_body))
            self.router.log("removed a foreign encrypted reasoning item OpenAI rejected; retrying")
        response_headers = response.getheaders()
        fingerprint = self.router.catalog_fingerprint()
        if is_catalog and response.status == 200:
            payload = inject_catalog(response.read(), self.router.registered_models(), self.router.display_names(),
                                     self.router.price_table(), self.router.cursor_fast_models())
            self.send_response(200)
            for name, value in response_headers:
                if name.lower() not in HOP_BY_HOP_HEADERS | {"content-encoding", "etag"}:
                    self.send_header(name, value)
            etag = injected_etag(dict((name.lower(), value) for name, value in response_headers).get("etag"), fingerprint)
            if etag:
                self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            self.router.catalog_served.set()
            return
        self.send_response(response.status)
        for name, value in response_headers:
            if name.lower() in HOP_BY_HOP_HEADERS:
                continue
            if name.lower() == "x-models-etag":
                value = injected_etag(value, fingerprint)  # keep in step with the catalog we serve
            self.send_header(name, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        total = 0
        content_type = response.getheader("Content-Type") or ""
        # The actual ChatGPT backend can omit Content-Type on a successful SSE
        # response. Native Codex uses the requested stream mode in that case.
        stream_response = "text/event-stream" in content_type or (not content_type and native_stream_request)
        parser = SseParser() if plaintext_agents and 200 <= response.status < 300 and stream_response else None

        def write_events(events):
            for kind, value in events:
                if kind == "comment":
                    self._write_chunk(value + b"\n\n")
                else:
                    self._write_chunk(encode_event(restore_event(value)))

        try:
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                total += len(chunk)
                if parser is None:
                    self._write_chunk(chunk)
                else:
                    write_events(parser.feed(chunk))
            if parser is not None:
                write_events(parser.flush())
        finally:
            connection.close()
        lowered = {name.lower(): value for name, value in response_headers}
        self.router.record({"route": "openai", "model": model, "status": response.status, "bytes": total,
                            "primary_used_percent": lowered.get("x-codex-primary-used-percent"),
                            "plan_type": lowered.get("x-codex-plan-type"), **metadata})
        self._write_chunk(b"")

    # --- Registered endpoints (OpenRouter, another provider's API, a local server) ---

    def _route_endpoint(self, model, entry, request, metadata):
        reject_encrypted_agent_messages(request)
        if self.command == "POST" and self.path.rstrip("/") == CODEX_BACKEND_PREFIX + "/responses":
            annotate_spawn_tools(request, self.router.model_choices())
        endpoint = endpoint_for(entry)
        name = endpoint.get("name") or "the endpoint"
        compaction = self._compaction_mode(request)
        if endpoint.get("cursor") or endpoint.get("wire") == "cursor":
            self._route_cursor(model, endpoint, request, metadata, compaction)
            return
        if self.command != "POST" or (self.path.rstrip("/") != CODEX_BACKEND_PREFIX + "/responses" and not compaction):
            raise RouterError(f"This operation is not supported for models on {name}.")
        if not isinstance(request, dict) or "input" not in request:
            raise RouterError(f"Only Responses API requests can be routed to {name}.")
        if compaction:
            # No backend can compact for this model, so the model writes the handoff summary itself.
            request = context_compaction.summarization_request(request)
        base_url = endpoint["base_url"]
        if endpoint.get("openrouter") and self.router.openrouter_upstream != OPENROUTER_UPSTREAM:
            host, port, secure = self.router.openrouter_upstream  # tests point OpenRouter at a stand-in
            prefix = "/api/v1"
        else:
            host, port, secure, prefix = split_base_url(base_url)
        key = self.router.key_provider.key_for(model) if endpoint.get("has_key") else None
        translated, alias_map = translate_request(request, openrouter=bool(endpoint.get("openrouter")),
                                                 preserve_continuation=True)
        scope = continuation_scope(endpoint, translated["model"])
        provider = provider_for_base_url(base_url)
        route_name = "openrouter" if endpoint.get("openrouter") else "endpoint"
        metadata = dict(metadata, service_tier=request.get("service_tier"), openrouter_model=translated["model"],
                        endpoint=name)
        if compaction:
            metadata["compaction"] = True
        wire = endpoint.get("wire") or "auto"
        if wire == "auto":
            wire = self.router.wire_overrides.get(base_url, "responses")
        attempted_fallback = False
        while True:
            if wire == "chat":
                body = chat_request_from_responses(translated, self.router.continuation, scope,
                                                   provider["id"] if provider else None)
                path = prefix + "/chat/completions"
            else:
                body = dict(translated, input=self.router.continuation.restore_responses_input(scope, translated["input"]))
                path = prefix + "/responses"
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers = {"Host": host if port in (80, 443) else f"{host}:{port}", "Content-Type": "application/json",
                       "Accept": "text/event-stream", "Content-Length": str(len(payload))}
            if key:
                headers["Authorization"] = "Bearer " + key
            if endpoint.get("openrouter"):
                headers["HTTP-Referer"] = "https://github.com/model-deck"
                headers["X-Title"] = "Model Deck for Codex"
            connection = (http.client.HTTPSConnection if secure else http.client.HTTPConnection)(host, port, timeout=600)
            try:
                connection.request("POST", path, body=payload, headers=headers)
                response = connection.getresponse()
            except OSError as error:
                self.router.log(f"upstream {host}:{port} unreachable: {error.__class__.__name__}")
                raise RouterError(f"Model Deck router could not reach {name} at {base_url}.") from None
            if response.status == 200:
                break
            error_body = response.read()
            connection.close()
            # Servers without a Responses API answer 404/405/501 there; switch to chat completions once.
            if wire == "responses" and endpoint.get("wire", "auto") == "auto" and not attempted_fallback \
                    and response.status in (404, 405, 501):
                attempted_fallback = True
                wire = "chat"
                self.router.wire_overrides[base_url] = "chat"
                self.router.log(f"{name}: no Responses API (HTTP {response.status}); using chat completions")
                continue
            message, code = openrouter_error_message(response.status, error_body, name)
            self.router.log(f"{name} status={response.status} code={code}")
            self.router.record({"route": route_name, "model": model, "status": response.status, "wire": wire, **metadata})
            self._send_failed(message, code)
            return
        collected = [] if compaction else None
        if compaction != "unary":
            self._begin_event_stream()
        parser = SseParser()
        translator = ChatStreamTranslator(self.router.continuation, scope) if wire == "chat" else None
        response_continuation = self.router.continuation if wire == "responses" and not endpoint.get("openrouter") else None
        response_nonce = os.urandom(16).hex()
        completed = None
        finished = False

        def emit_event(raw):
            nonlocal completed, finished
            if finished:
                return
            event = translate_event(raw, alias_map, response_continuation, scope, response_nonce)
            if event is None:
                return
            if event["type"] in ("response.completed", "response.failed", "response.incomplete"):
                completed = event
                finished = True
            if collected is not None:
                collected.append(event)
            else:
                self._write_chunk(encode_event(event))

        def emit(value):
            for raw in translator.feed(value) if translator else [value]:
                emit_event(raw)

        try:
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                for kind, value in parser.feed(chunk):
                    if kind == "comment":
                        if compaction != "unary":
                            self._write_chunk(value + b"\n\n")  # keepalive while the summary is written
                    else:
                        emit(value)
            for kind, value in parser.flush():
                if kind == "event":
                    emit(value)
            if translator and not finished:
                for raw in translator.finish():
                    emit_event(raw)
        except (ConnectionError, BrokenPipeError):
            raise
        except (ContinuationError, OSError, http.client.HTTPException) as error:
            message = str(error) if isinstance(error, ContinuationError) else f"{name} interrupted the response stream."
            emit_event(failed_event(message, "server_error"))
        finally:
            connection.close()
        if not finished:
            emit_event(failed_event(f"{name} ended the stream before the response completed.", "server_error"))
        response_document = completed.get("response") if isinstance(completed, dict) else None
        response_document = response_document if isinstance(response_document, dict) else {}
        outcome = completed.get("type", "response.failed") if isinstance(completed, dict) else "response.failed"
        self.router.record({"route": route_name, "model": model, "status": 200 if outcome == "response.completed" else 502,
                            "outcome": outcome.removeprefix("response."), "wire": wire,
                            "usage": response_document.get("usage"),
                            "generation_id": response_document.get("id"), **metadata})
        if compaction:
            self._finish_compaction(compaction, model, collected, completed, name)
            return
        self._write_chunk(b"")

    def _route_cursor(self, model, endpoint, request, metadata, compaction=None):
        """Cursor asks for tools; Codex executes them and returns their results on its next request.

        A compaction request runs as a tool-less summarization turn on the same Cursor model.
        """
        if (self.command != "POST" or (self.path.rstrip("/") != CODEX_BACKEND_PREFIX + "/responses" and not compaction)
                or not isinstance(request, dict) or not isinstance(request.get("input"), list)):
            raise RouterError("Cursor SDK supports ordinary agent turns and context compaction only.")
        if not endpoint.get("has_key") or not endpoint.get("account"):
            raise RouterError("Save a Cursor API key on Model Deck's Endpoints page before using this agent.")
        if compaction:
            request = context_compaction.summarization_request(request)
            metadata = dict(metadata, compaction=True)
        key = self.router.key_provider.key_for(model)
        stream = None
        started = False
        failed = False
        collected = [] if compaction else None
        terminal = None
        try:
            stream = self.router.cursor_agents().stream(request, metadata, key, endpoint["account"])
            for event in stream:
                if not started and compaction != "unary":
                    self._begin_event_stream()
                    started = True
                if event is None:
                    if started:
                        self._write_chunk(b": cursor agent active\n\n")
                    continue
                if event.get("type") in ("response.completed", "response.failed", "response.incomplete"):
                    terminal = event
                if collected is not None:
                    collected.append(event)
                else:
                    self._write_chunk(encode_event(event))
            if terminal is None:
                raise RouterError("Cursor ended the stream before completing its response.")
        except (ConnectionError, BrokenPipeError):
            raise
        except Exception as error:
            # SDK exceptions can contain upstream request details. Only our known local errors
            # are suitable for display; credentials and prompts never enter the router log.
            from cursor_sdk_runtime import CursorRuntimeError
            failed = True
            message = str(error) if isinstance(error, (RouterError, CursorRuntimeError)) else (
                "Cursor SDK could not complete this turn. Check SDK setup and the Cursor key on Endpoints.")
            if started:
                self._write_chunk(encode_event(failed_event(message, "server_error")))
            else:
                self._send_failed(message, "server_error")
        finally:
            if stream is not None:
                stream.close()
            document = (terminal or {}).get("response") or {}
            self.router.record({"route": "cursor", "model": model, "wire": "cursor",
                                "status": 200 if terminal and document.get("status") != "failed" else 502,
                                "endpoint": endpoint.get("name") or "Cursor",
                                "usage": document.get("usage"), "generation_id": document.get("id"),
                                "billing": "Cursor subscription", "cost_source": "cursor_sdk",
                                "cursor_agent_id": document.get("cursor_agent_id"),
                                "cost": document.get("cost"), **metadata})
        if compaction and not failed:
            if compaction == "stream" and not started:
                self._begin_event_stream()
            self._finish_compaction(compaction, model, collected, terminal, endpoint.get("name") or "Cursor")
        elif started:
            self._write_chunk(b"")

    def _compaction_mode(self, request):
        """How Codex asked for context compaction.

        "stream": a compaction_trigger item on an ordinary /responses turn (Codex's current path).
        "unary": a POST to /responses/compact (its older path). None: not a compaction request.
        """
        if self.command != "POST" or not isinstance(request, dict):
            return None
        path = self.path.rstrip("/")
        if path == CODEX_BACKEND_PREFIX + "/responses/compact":
            return "unary"
        if path == CODEX_BACKEND_PREFIX + "/responses" and context_compaction.has_trigger(request):
            return "stream"
        return None

    def _finish_compaction(self, mode, model, events, terminal, name):
        """Answer Codex's compaction request from the summarization turn's events.

        Codex keeps exactly one compaction item from a streamed compaction turn, or the output list
        of a unary /responses/compact call. Either way the item carries the summary itself.
        """
        summary = context_compaction.summary_from_events(events or [])
        response = terminal.get("response") if isinstance(terminal, dict) else None
        response = response if isinstance(response, dict) else {}
        if not isinstance(terminal, dict) or terminal.get("type") != "response.completed":
            failure = (response.get("error") or {}).get("message") or f"{name} did not complete the context summary."
        elif not summary:
            failure = f"{name} returned an empty context summary. Try compacting again."
        else:
            failure = None
        if failure:
            self.router.log(f"context compaction on {name} failed")
            if mode == "unary":
                self._send_json(502, {"error": {"code": "server_error", "message": failure}})
            else:
                self._write_chunk(encode_event(failed_event(failure, "server_error")))
                self._write_chunk(b"")
            return
        item = context_compaction.compaction_item(context_compaction.encode(summary, model))
        self.router.log(f"context compacted on {name}: summary of {len(summary)} characters")
        if mode == "unary":
            self._send_json(200, {"output": [item]})
            return
        for event in context_compaction.response_events(item, response.get("usage")):
            self._write_chunk(encode_event(event))
        self._write_chunk(b"")

    # --- helpers ------------------------------------------------------------

    def _begin_event_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write_chunk(self, chunk):
        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
        self.wfile.flush()

    def _replay_error(self, response, error_body):
        self.send_response(response.status)
        for name, value in response.getheaders():
            if name.lower() not in HOP_BY_HOP_HEADERS | {"content-length"}:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(error_body)))
        self.end_headers()
        self.wfile.write(error_body)
        self.wfile.flush()

    def _send_empty(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def _send_json(self, status, document):
        payload = json.dumps(document).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_failed(self, message, code="invalid_prompt"):
        if self.path.rstrip("/") == CODEX_BACKEND_PREFIX + "/responses/compact":
            self._send_json(502, {"error": {"code": code, "message": message}})  # unary endpoint: JSON, never SSE
            return
        payload = encode_event(failed_event(message, code))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()


class RouterServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # Codex closes keep-alive connections abruptly after a refused upgrade; that is not an error.
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, BrokenPipeError, TimeoutError)):
            return
        self.router.log(f"connection handler failed: {error.__class__.__name__}")


class LocalRouter:
    def __init__(self, registry, key_provider=None, chatgpt_upstream=CHATGPT_UPSTREAM,
                 openrouter_upstream=OPENROUTER_UPSTREAM, ledger_path=None, log_path=None, pricing_loader=None,
                 cursor_manager=None, benchmark_path=None, refresh_benchmarks=False, continuation_path=None,
                 refresh_cursor_catalog=False):
        self.registry = registry
        self.refresh_cursor_catalog = refresh_cursor_catalog
        self._cursor_fast_cache = (None, None)
        self.pricing_loader = pricing_loader  # e.g. pricing.load; None keeps the router offline for prices
        self._price_table = pricing.load_cached()[0] if pricing_loader is not None else {}
        self.key_provider = key_provider or KeychainKeyProvider(registry)
        self.chatgpt_upstream = chatgpt_upstream
        self.openrouter_upstream = openrouter_upstream
        support = support_directory()
        self.benchmark_path = Path(benchmark_path) if benchmark_path is not None else support / "benchmarks-cache.json"
        self.refresh_benchmarks = refresh_benchmarks
        self._benchmark_cache = (None, {})
        self._benchmark_lock = threading.Lock()
        self.ledger_path = Path(ledger_path) if ledger_path is not None else support / "router-ledger.jsonl"
        self.continuation = ProviderContinuationStore(continuation_path or self.ledger_path.parent / "provider-continuation.sqlite")
        self.log_path = Path(log_path) if log_path is not None else support / "router.log"
        self.server = None
        self.thread = None
        self.lock = threading.Lock()
        self._registered_cache = ({}, 0.0)
        self.catalog_requested = threading.Event()
        self.catalog_served = threading.Event()
        self.wire_overrides = {}  # base_url -> "chat" once an endpoint proved it has no Responses API
        self._cursor_manager = cursor_manager
        self._cursor_lock = threading.Lock()
        self._thread_directories = {}

    def cursor_agents(self):
        with self._cursor_lock:
            if self._cursor_manager is None:
                from cursor_agent import CursorAgentManager
                self._cursor_manager = CursorAgentManager()
            return self._cursor_manager

    def remember_thread(self, thread_id, cwd):
        if isinstance(thread_id, str) and isinstance(cwd, str) and Path(cwd).is_absolute():
            with self._cursor_lock:
                self._thread_directories[thread_id] = cwd

    def thread_directory(self, thread_id):
        with self._cursor_lock:
            return self._thread_directories.get(thread_id)

    def cancel_cursor_turn(self, thread_id, turn_id=None):
        with self._cursor_lock:
            manager = self._cursor_manager
        if manager is not None:
            manager.cancel(thread_id, turn_id)

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}{CODEX_BACKEND_PREFIX}"

    def codex_arguments(self):
        """Config overrides that make Codex send every model request through this router."""
        return ["-c", f'openai_base_url="{self.base_url}"', "-c", "features.enable_request_compression=false"]

    def start(self):
        self.server = RouterServer(("127.0.0.1", 0), RouterHandler)
        self.server.router = self
        self.thread = threading.Thread(target=self.server.serve_forever, name="model-deck-router", daemon=True)
        self.thread.start()
        self.log(f"router listening on {self.base_url}")
        if self.pricing_loader is not None:
            threading.Thread(target=self._load_prices, name="model-deck-pricing", daemon=True).start()
        if self.refresh_benchmarks:
            threading.Thread(target=self._refresh_benchmarks, name="model-deck-benchmarks", daemon=True).start()
        if self.refresh_cursor_catalog:
            threading.Thread(target=self._refresh_cursor_catalog, name="model-deck-cursor-catalog", daemon=True).start()
        return self

    def _refresh_cursor_catalog(self):
        """Cache Cursor's SDK catalog so the picker offers Fast only where Cursor does."""
        from cursor_sdk_runtime import catalog_cache_age, list_models
        cursor_models = [model for model, entry in self.registered_models().items()
                         if endpoint_for(entry).get("cursor") and endpoint_for(entry).get("has_key")]
        if not cursor_models:
            return
        age = catalog_cache_age()
        if age is not None and age < CURSOR_CATALOG_MAX_AGE:
            return
        try:
            result = list_models(self.key_provider.key_for(cursor_models[0]))  # saves the cache on success
        except Exception:
            result = None
        if result and result.get("ok"):
            self.log(f"cursor catalog cached for {len(result.get('models') or [])} models")
        else:
            self.log("cursor catalog unavailable; Fast stays offered for every Cursor model")

    def cursor_fast_models(self):
        """cursor/<id> models whose cached SDK catalog offers Fast; None until a catalog is cached."""
        from cursor_sdk_runtime import catalog_cache_path, fast_capable_models
        try:
            state = catalog_cache_path().stat()
            signature = (state.st_ino, state.st_mtime_ns, state.st_size)
        except OSError:
            signature = None
        with self._cursor_lock:
            previous, capable = self._cursor_fast_cache
            if signature is None:
                return None
            if signature != previous:
                try:
                    capable = fast_capable_models()
                except Exception:
                    capable = None
                self._cursor_fast_cache = (signature, capable)
            return None if capable is None else set(capable)

    def _refresh_benchmarks(self):
        """Refresh public evidence at startup, independently of inference requests."""
        try:
            BenchmarkStore(self.benchmark_path).ensure()
        except Exception:
            self.log("benchmark refresh unavailable; retained cached evidence")

    def benchmark_lines(self):
        """Cached-only summaries; observe MCP refreshes and advance freshness once a minute."""
        try:
            state = self.benchmark_path.stat()
            signature = (state.st_ino, state.st_mtime_ns, state.st_size, int(time.time() // 60))
        except OSError:
            signature = (None, int(time.time() // 60))
        with self._benchmark_lock:
            previous, lines = self._benchmark_cache
            if signature != previous:
                lines = load_benchmark_lines(BenchmarkStore(self.benchmark_path))
                self._benchmark_cache = (signature, lines)
            return dict(lines)

    def model_choices(self):
        return model_choice_lines(self.registered_models(), self.price_table(), self.benchmark_lines())

    def _load_prices(self):
        try:
            table = self.pricing_loader()
        except Exception:
            return
        if isinstance(table, dict):
            self._price_table = table
            self.log(f"pricing loaded for {len(table)} OpenRouter models")

    def price_table(self):
        return self._price_table

    def price_lines(self):
        """Model id -> list-price text for registered OpenRouter models (empty when unknown)."""
        lines = {}
        for model, entry in self.registered_models().items():
            if endpoint_for(entry).get("openrouter"):
                line = pricing.price_line(pricing.pricing_for(model, self._price_table))
                if line:
                    lines[model] = line
        return lines

    def wait_for_catalog(self, timeout):
        """True once Codex has fetched the catalog with the registered models through this router.

        Spawning a registered model before that fetch lands makes Codex report the model as
        unknown, so the bridge holds the first task start briefly. A missing fetch never blocks
        for longer than `timeout` seconds.
        """
        if self.catalog_served.is_set():
            return True
        if not self.catalog_requested.wait(timeout):
            return False
        if self.catalog_served.wait(timeout):
            time.sleep(0.5)  # let Codex apply the catalog it just received
            return True
        return False

    def stop(self):
        if self._cursor_manager is not None:
            self._cursor_manager.close()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def registered_models(self):
        with self.lock:
            models, loaded_at = self._registered_cache
            if time.monotonic() - loaded_at > 5:
                try:
                    models = self.registry.load_models()
                except Exception:
                    models = {}
                self._registered_cache = (models, time.monotonic())
            return models

    def display_names(self):
        named = getattr(self.registry, "display_name_for", None)
        return {model: (named(model) if named else friendly_model_name(model)) for model in self.registered_models()}

    def catalog_fingerprint(self):
        """Short hash of the registered models, their picker names, prices, and Cursor Fast offers."""
        fast = self.cursor_fast_models()
        document = json.dumps([sorted(self.display_names().items()), sorted(self.price_lines().items()),
                               sorted(fast) if fast is not None else None],
                              separators=(",", ":")).encode("utf-8")
        return "deck" + hashlib.sha256(document).hexdigest()[:10]

    def log(self, message):
        self._append(self.log_path, time.strftime("%Y-%m-%dT%H:%M:%S ") + message + "\n")

    def record(self, entry):
        entry = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}
        self._append(self.ledger_path, json.dumps(entry, sort_keys=True) + "\n")

    def _append(self, path, text):
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
                stream.write(text)
        except OSError:
            pass
