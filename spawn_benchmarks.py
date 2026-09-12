"""Cached model evidence embedded in the native spawn_agent tool description.

This module does not fetch, resolve model aliases, alter tool schemas, or install tools.
"""
from datetime import datetime, timezone
import math
import re
import unicodedata

import model_benchmarks
import pricing
from routing_registry import RoutingRegistry

MAX_MODELS = 40
MAX_BENCHMARK_LINE = 350
MAX_CHOICE_LINE = 1100
MAX_BLOCK = 32000
START = "\n\n[Model Deck choices]\n"
END = "\n[/Model Deck choices]"
_BLOCK = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
MISSING = "No exact published benchmark match"
NATIVE_NAMESPACES = frozenset({"collaboration", "multi_agent_v1", "multi_agent_v2", "functions"})
ARENA_CATEGORIES = ("codecategories", "website", "uicomponent", "dataviz", "gamedev", "fullstack", "webapps")


def _clean(value, limit=300):
    if not isinstance(value, str):
        return ""
    value = " ".join("".join(" " if unicodedata.category(char).startswith("C") else char for char in value).split())
    return value.replace("[Model Deck choices]", "Model Deck choices").replace("[/Model Deck choices]", "Model Deck choices")[:limit]


def _finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _utc_date(value):
    try:
        if _finite(value):
            return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OSError, OverflowError):
        pass
    return None


def _date_range(dates):
    dates = sorted({date for date in dates if date})
    return dates[0] if len(dates) == 1 else dates[0] + ".." + dates[-1] if dates else "unknown"


def _evidence_line(rows):
    aa, arena = {}, {}
    for row in rows:
        if not isinstance(row, dict) or not _finite(row.get("score")) or row.get("direction") != "higher":
            continue
        metric, source = row.get("metric"), row.get("source")
        if source == "artificial-analysis" and metric in ("coding_index", "agentic_index", "intelligence_index"):
            aa.setdefault(metric, row)
        elif source == "design-arena" and metric == "elo":
            category, arena_name = row.get("category"), row.get("arena")
            if isinstance(category, str) and isinstance(arena_name, str) and category in ARENA_CATEGORIES and arena_name in ("models", "agents", "builders"):
                arena.setdefault((category, arena_name), row)
    chosen = list(aa.values())
    parts = []
    if aa:
        parts.append("Artificial Analysis " + ", ".join(metric.removesuffix("_index") + "=" + format(aa[metric]["score"], ".5g")
                     for metric in ("coding_index", "agentic_index", "intelligence_index") if metric in aa) + " (higher better)")
    arena_rows = [row for _, row in sorted(arena.items(), key=lambda item: (ARENA_CATEGORIES.index(item[0][0]), item[0][1]))][:2]
    # Add complete source/category evidence only when it and provenance fit the bound.
    for row in arena_rows:
        candidate = "Design Arena " + row["arena"] + "/" + row["category"] + " Elo=" + format(row["score"], ".5g") + " (higher better)"
        if len("; ".join(parts + [candidate])) <= MAX_BENCHMARK_LINE - 100:
            parts.append(candidate)
            chosen.append(row)
    if not parts:
        return None
    retrieved = _date_range([_utc_date(row.get("fetched_at")) for row in chosen])
    parts.append("retrieved UTC " + retrieved + "; cache " + ("stale" if any(row.get("stale") is not False for row in chosen) else "fresh"))
    evaluation = _date_range([_utc_date(row.get("as_of")) for row in chosen])
    if evaluation != "unknown":
        parts.append("publisher as_of " + evaluation)
    result = "; ".join(parts)
    return result if len(result) <= MAX_BENCHMARK_LINE else None


def load_benchmark_lines(store=None):
    """Read cached evidence only. Exact routable ids supplied by BenchmarkStore remain exact."""
    try:
        store = store if store is not None else model_benchmarks.BenchmarkStore()
        catalog, scores, _ = store.evidence()
    except Exception:
        return {}
    if not isinstance(catalog, dict) or not isinstance(scores, list):
        return {}
    grouped = {}
    for row in scores[:100000]:
        if not isinstance(row, dict):
            continue
        model = row.get("model")
        if not isinstance(model, str) or model not in catalog or not model or _clean(model) != model:
            continue
        grouped.setdefault(model, []).append(row)
    return {model: line for model, rows in grouped.items() if (line := _evidence_line(rows))}


def _endpoint(entry):
    if isinstance(entry.get("endpoint"), dict):
        return entry["endpoint"]
    config = entry.get("config")
    providers = config.get("model_providers") if isinstance(config, dict) else None
    if not isinstance(providers, dict):
        return {}
    provider = providers.get(entry.get("provider"))
    if not isinstance(provider, dict):
        return {}
    try:
        return RoutingRegistry.endpoint_summary(provider, {})
    except Exception:
        return {}


def _price_capabilities(entry):
    if not isinstance(entry, dict):
        return "API price unknown; context/tools/vision unknown"
    numeric = {key: entry.get(key) if _finite(entry.get(key)) and entry[key] >= 0 else None
               for key in ("input", "output", "cache_read", "cache_write", "context")}
    line = pricing.price_line(numeric)
    if numeric["cache_write"] is not None:
        line += ("; " if line else "") + "cache write " + pricing._money(numeric["cache_write"]) + "/M"
    if numeric["input"] is None or numeric["output"] is None:
        line = (line + "; " if line else "") + "API price unknown or incomplete"
    if numeric["context"] is None:
        line += "; context unknown"
    tools = "yes" if entry.get("tools") is True else "no" if entry.get("tools") is False else "unknown"
    modalities = entry.get("modalities")
    vision = "yes" if isinstance(modalities, list) and "image" in modalities else "no" if isinstance(modalities, list) else "unknown"
    return line + "; tools=" + tools + "; vision=" + vision


def model_choice_lines(registrations, price_table=None, benchmark_lines=None):
    """Return bounded registered choices with endpoint-owned billing, not name-based routing."""
    if not isinstance(registrations, dict):
        return {}
    if price_table is None:
        try:
            price_table, _ = pricing.load_cached()
        except Exception:
            price_table = {}
    price_table = price_table if isinstance(price_table, dict) else {}
    benchmark_lines = load_benchmark_lines() if benchmark_lines is None else benchmark_lines
    benchmark_lines = benchmark_lines if isinstance(benchmark_lines, dict) else {}
    result = {}
    for model in sorted(key for key in registrations if isinstance(key, str)):
        if len(result) >= MAX_MODELS:
            break
        entry = registrations[model]
        if not isinstance(entry, dict) or not model or _clean(model) != model:
            continue
        endpoint = _endpoint(entry)
        name = _clean(endpoint.get("name"), 80) or "Saved endpoint"
        role = _clean(entry.get("role")) or "unknown"
        if endpoint.get("cursor") or endpoint.get("wire") == "cursor":
            billing = "Cursor subscription IDE/Cloud usage pools; account limits and overages apply; API price unknown; context/tools/vision unknown"
        elif endpoint.get("openrouter"):
            billing = "OpenRouter credits; " + _price_capabilities(price_table.get(model))
        elif endpoint.get("has_key"):
            billing = "Provider API key; API price unknown; context/tools/vision unknown"
        else:
            billing = "Local/no API billing; API price unknown; context/tools/vision unknown" if endpoint else "Endpoint billing unknown; API price unknown"
        # A same-named id on a different provider is not evidence of an OpenRouter identity.
        benchmark = benchmark_lines.get(model) if endpoint.get("openrouter") else None
        evidence = _clean(benchmark, MAX_BENCHMARK_LINE) if isinstance(benchmark, str) and benchmark else MISSING
        line = model + " | role=" + role + " | " + name + ": " + billing + " | " + evidence
        if len(line) <= MAX_CHOICE_LINE:
            result[model] = line
    return result


def annotate_spawn_tools(request, choice_lines):
    """Replace only our description block on exact spawn_agent function declarations."""
    if not isinstance(request, dict) or not isinstance(choice_lines, dict) or not choice_lines:
        return False
    lines = []
    for model in sorted(key for key in choice_lines if isinstance(key, str))[:MAX_MODELS]:
        line = _clean(choice_lines[model], MAX_CHOICE_LINE)
        if line and len("\n".join(lines + [line])) <= MAX_BLOCK - 750:
            lines.append(line)
    if not lines:
        return False
    block = START + "Registered exact model/role ids, saved endpoint billing, cached prices/context and published benchmarks:\n" + "\n".join(lines)
    block += "\nNative gpt-* choices: ChatGPT subscription usage; no per-token API rate assigned and no benchmark identity inferred from OpenRouter variants."
    block += "\nCompare scores only within the same source, test and snapshot; no combined score. Missing evidence is unknown, not zero. Prices are USD list prices per million tokens, may vary by provider/routing tier, and are not settled task cost. Retrieved date is not evaluation date. Codex tool schemas and approval policies still apply." + END
    containers = []
    if isinstance(request.get("tools"), list):
        containers.append(request["tools"])
    if isinstance(request.get("input"), list):
        containers.extend(item["tools"] for item in request["input"] if isinstance(item, dict)
                          and item.get("type") == "additional_tools" and isinstance(item.get("tools"), list))
    changed = False
    def annotate(tools, in_namespace=False):
        nonlocal changed
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "namespace":
                if not in_namespace and isinstance(tool.get("name"), str) and tool["name"] in NATIVE_NAMESPACES and isinstance(tool.get("tools"), list):
                    annotate(tool["tools"], in_namespace=True)
            elif tool.get("type") == "function":
                function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
                if function.get("name") != "spawn_agent":
                    continue
                original = function.get("description", "")
                if not isinstance(original, str):
                    continue
                updated = _BLOCK.sub("", original) + block
                if updated != original:
                    function["description"] = updated
                    changed = True
    for container in containers:
        annotate(container)
    return changed
