"""Read the native picker catalog and locally registered provider models."""
import json
import sys

import pricing
from provider_connections import provider_billing_description, provider_for_base_url
from provider_usage import NativeUsageClient
from routing_registry import CURSOR_BILLING, RoutingRegistry, endpoint_description

MAX_MODELS = 256
MAX_OUTPUT = 128 * 1024


def text_field(value, maximum, required=False):
    if not isinstance(value, str) or (required and not value) or any(ord(char) < 32 for char in value):
        raise ValueError("Invalid catalog text")
    return value[:maximum]


def model_id(value):
    if not isinstance(value, str) or not value or len(value) > 256 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Invalid model identifier")
    return value


def parse_native_page(page):
    if not isinstance(page, dict) or not isinstance(page.get("data"), list):
        raise ValueError("Invalid model catalog page")
    cursor = page.get("nextCursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 4096):
        raise ValueError("Invalid catalog cursor")
    rows = []
    identifiers = set()
    if len(page["data"]) > MAX_MODELS:
        raise ValueError("Catalog page exceeds limit")
    for entry in page["data"]:
        if not isinstance(entry, dict) or type(entry.get("hidden")) is not bool:
            raise ValueError("Invalid model catalog entry")
        model = model_id(entry.get("model"))
        if model in identifiers:
            raise ValueError("Duplicate catalog model")
        identifiers.add(model)
        if entry["hidden"]:
            continue
        rows.append({"model": model, "display_name": text_field(entry.get("displayName"), 128, True),
                     "description": text_field(entry.get("description"), 256),
                     "provider": "openai", "role": None})
    return rows, cursor, identifiers


def collect_openai_models():
    client = None
    try:
        client = NativeUsageClient()
        client.request("initialize", {"clientInfo": {"name": "openrouter_settings_catalog", "version": "1"},
                                      "capabilities": {"experimentalApi": True}})
        client.send({"method": "initialized", "params": {}})
        rows, seen_models, seen_cursors = [], set(), set()
        cursor = None
        while True:
            params = {"includeHidden": False, "limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            page_rows, cursor, identifiers = parse_native_page(client.request("model/list", params))
            if seen_models.intersection(identifiers):
                raise ValueError("Duplicate catalog model across pages")
            seen_models.update(identifiers)
            if len(seen_models) > MAX_MODELS:
                raise ValueError("Catalog exceeds limit")
            rows.extend(page_rows)
            if cursor is None:
                return {"ok": True}, rows
            if cursor in seen_cursors or len(seen_cursors) >= MAX_MODELS:
                raise ValueError("Repeated or excessive catalog pagination")
            seen_cursors.add(cursor)
    except Exception:
        return {"ok": False, "error": "Could not read the native Codex model catalog."}, []
    finally:
        if client:
            client.close()


def collect_openrouter_models():
    try:
        registry = RoutingRegistry()
        models = registry.load_models()
        if len(models) > MAX_MODELS:
            raise ValueError("Registered catalog exceeds limit")
        custom_names = registry.load_display_names()
        price_table = pricing.load(timeout=8) if any((entry.get("endpoint") or {"openrouter": True}).get("openrouter")
                                                     for entry in models.values()) else {}
        rows = []
        for model, entry in models.items():
            model_id(model)
            endpoint = entry.get("endpoint") or {"name": "OpenRouter", "openrouter": True, "has_key": True}
            preset = provider_for_base_url(endpoint.get("base_url"))
            provider_id = ("cursor" if endpoint.get("cursor") else "openrouter" if endpoint.get("openrouter")
                           else preset["id"] if preset else "custom")
            provider_name = ("Cursor" if provider_id == "cursor" else "OpenRouter" if provider_id == "openrouter"
                             else preset["name"] if preset else endpoint.get("name") or "Custom endpoint")
            billing_note = (CURSOR_BILLING if provider_id == "cursor" else "Uses OpenRouter credits." if provider_id == "openrouter"
                            else provider_billing_description(endpoint.get("base_url"), endpoint.get("has_key", False)))
            billing = ("Cursor subscription" if provider_id == "cursor" else "OpenRouter credits" if provider_id == "openrouter"
                       else preset["billing"] if preset and endpoint.get("has_key") else "key required" if preset
                       else "API key" if endpoint.get("has_key") else "endpoint managed")
            price = pricing.pricing_for(model, price_table) if endpoint.get("openrouter") else None
            rows.append({"model": model, "display_name": text_field(registry.display_name_for(model), 128, True),
                         "description": text_field(endpoint_description(endpoint), 256),
                         "provider": provider_id, "provider_name": text_field(provider_name, 64, True),
                         "billing": billing, "billing_note": billing_note, "role": text_field(entry["role"], 128, True),
                         "custom_name": model in custom_names,
                         "endpoint": text_field(endpoint.get("name") or "OpenRouter", 64, True),
                         "endpoint_account": endpoint.get("account"), "endpoint_base_url": endpoint.get("base_url"),
                         "endpoint_wire": endpoint.get("wire", "auto"),
                         "billed": bool(endpoint.get("has_key", True)),
                         "pricing": text_field(pricing.price_line(price), 128),
                         "context": price.get("context") if price else None,
                         "openrouter": bool(endpoint.get("openrouter")), "cursor": bool(endpoint.get("cursor"))})
        return {"ok": True}, rows
    except Exception:
        return {"ok": False, "error": "Could not read registered models."}, []


def collect_catalog():
    openai, native_rows = collect_openai_models()
    openrouter, registered_rows = collect_openrouter_models()
    # Codex persists our injected /models rows in its native catalog cache.
    # Rebuild those rows from today's registry, including endpoint and billing
    # metadata, instead of mistaking them for conflicting OpenAI models. This
    # also keeps a removed registration out of the library until cache refresh.
    native_rows = [entry for entry in native_rows
                   if not (entry.get("description", "").startswith(("Routed to ", "Routed through "))
                           and " by Model Deck." in entry.get("description", ""))]
    if {entry["model"] for entry in native_rows}.intersection(entry["model"] for entry in registered_rows):
        openrouter = {"ok": False, "error": "A registered model conflicts with the native catalog."}
        registered_rows = []
    rows = native_rows + registered_rows
    if len(rows) > MAX_MODELS:
        openrouter = {"ok": False, "error": "Combined model catalog exceeds the display limit."}
        rows = native_rows
    result = {"openai": openai, "openrouter": openrouter, "models": rows}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_OUTPUT - 1:
        return {"openai": {"ok": False, "error": "Model catalog exceeds the display size limit."},
                "openrouter": {"ok": False, "error": "Model catalog exceeds the display size limit."}, "models": []}
    return result


def main():
    # Consume the UI request without accepting executable, path, or endpoint overrides.
    sys.stdin.buffer.read(4097)
    print(json.dumps(collect_catalog(), ensure_ascii=False))


if __name__ == "__main__":
    main()
