"""OpenRouter list prices and context sizes, cached locally so the picker, the agent roster, and
the Model Deck MCP tools can show what a model costs. Public data only; no key is ever sent."""
import json
import os
import tempfile
import time
import urllib.request

from routing_registry import support_directory

PRICING_URL = "https://openrouter.ai/api/v1/models"
CACHE_FILE = "pricing-cache.json"
CACHE_TTL = 6 * 3600
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_MODELS = 4096
PER_MILLION = 1_000_000


def cache_path():
    return support_directory() / CACHE_FILE


def _per_million(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price < 0:  # OpenRouter uses -1 for "priced per request / dynamic"
        return None
    return round(price * PER_MILLION, 6)


def parse_pricing(document):
    """Reduce OpenRouter's /models document to {id: {input, output, cache_read, cache_write, context, ...}}."""
    rows = document.get("data") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Unexpected pricing document")
    pricing = {}
    for row in rows[:MAX_MODELS]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            continue
        prices = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
        architecture = row.get("architecture") if isinstance(row.get("architecture"), dict) else {}
        modalities = architecture.get("input_modalities")
        parameters = row.get("supported_parameters")
        entry = {
            "name": row.get("name") if isinstance(row.get("name"), str) else row["id"],
            "input": _per_million(prices.get("prompt")),
            "output": _per_million(prices.get("completion")),
            "cache_read": _per_million(prices.get("input_cache_read")),
            "cache_write": _per_million(prices.get("input_cache_write")),
            "context": row.get("context_length") if isinstance(row.get("context_length"), int) and row["context_length"] > 0 else None,
            "modalities": [m for m in modalities if isinstance(m, str)] if isinstance(modalities, list) else ["text"],
            "tools": "tools" in parameters if isinstance(parameters, list) else None,
            "reasoning": "reasoning" in parameters if isinstance(parameters, list) else None,
            "description": (row.get("description") or "")[:400] if isinstance(row.get("description"), str) else "",
        }
        pricing[row["id"]] = entry
    return pricing


def load_cached(path=None, max_age=CACHE_TTL):
    """(pricing, fresh). Missing or unreadable caches count as empty and stale."""
    path = path or cache_path()
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_DOCUMENT_BYTES:
            return {}, False
        document = json.loads(path.read_text(encoding="utf-8"))
        pricing = document.get("pricing")
        fetched = document.get("fetched")
        if not isinstance(pricing, dict) or not isinstance(fetched, (int, float)):
            return {}, False
        return pricing, (time.time() - fetched) < max_age
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return {}, False


def refresh(path=None, timeout=15, opener=None):
    """Fetch OpenRouter's public catalog and rewrite the cache. Returns the parsed pricing."""
    path = path or cache_path()
    request = urllib.request.Request(PRICING_URL, headers={"Accept": "application/json", "User-Agent": "ModelDeck/1.9"})
    with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
        raw = response.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("Pricing document too large")
    pricing = parse_pricing(json.loads(raw))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".pricing-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"fetched": time.time(), "pricing": pricing}, stream)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return pricing


def load(path=None, refresh_if_stale=True, timeout=15):
    """Cached pricing, refreshed when older than the TTL. Never raises; stale data beats none."""
    pricing, fresh = load_cached(path)
    if fresh or not refresh_if_stale:
        return pricing
    try:
        return refresh(path, timeout=timeout)
    except Exception:
        return pricing


def base_model(model):
    """Strip an OpenRouter routing variant (`:nitro`, `:floor`, `:free`) so pricing lookups match."""
    return model.split(":", 1)[0] if ":" in model else model


def pricing_for(model, pricing):
    return pricing.get(model) or pricing.get(base_model(model))


def _money(value):
    if value is None:
        return None
    if value == 0:
        return "$0"
    if value < 0.01:
        return f"${value:.4f}".rstrip("0").rstrip(".")
    if value < 1:
        return f"${value:.3f}".rstrip("0").rstrip(".")
    return f"${value:.2f}".rstrip("0").rstrip(".")


def _tokens(value):
    if value is None:
        return None
    if value >= 1_000_000:
        return f"{value / 1_000_000:g}M"
    return f"{value // 1000}k"


def price_line(entry):
    """Short human line: 'in $0.15/M · out $0.60/M · cached $0.003/M · 1M context'."""
    if not entry:
        return ""
    parts = []
    if entry.get("input") is not None:
        parts.append(f"in {_money(entry['input'])}/M")
    if entry.get("output") is not None:
        parts.append(f"out {_money(entry['output'])}/M")
    if entry.get("cache_read") is not None:
        parts.append(f"cached {_money(entry['cache_read'])}/M")
    if entry.get("context"):
        parts.append(f"{_tokens(entry['context'])} context")
    return " · ".join(parts)


def estimate_cost(entry, usage):
    """Dollars for a usage record ({input_tokens, output_tokens, cached_tokens}); None when unpriced."""
    if not entry or entry.get("input") is None or entry.get("output") is None:
        return None
    cached = usage.get("cached_tokens") or 0
    uncached = max((usage.get("input_tokens") or 0) - cached, 0)
    cache_rate = entry.get("cache_read") if entry.get("cache_read") is not None else entry["input"]
    return (uncached * entry["input"] + cached * cache_rate + (usage.get("output_tokens") or 0) * entry["output"]) / PER_MILLION
