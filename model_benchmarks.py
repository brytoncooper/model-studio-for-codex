"""Published OpenRouter benchmark evidence, with exact catalog identities and per-feed cache.

Sources: https://openrouter.ai/docs/api/api-reference/benchmarks/get-benchmarks
https://openrouter.ai/docs/guides/overview/models
The public catalog currently embeds Artificial Analysis and Design Arena scores. Optional
unified feeds add publisher timestamps/citations. No name heuristics or inference requests.
"""
import concurrent.futures
import json
import math
import os
from pathlib import Path
import tempfile
import time
import urllib.request

from routing_registry import support_directory

CATALOG_URL = "https://openrouter.ai/api/v1/models?output_modalities=all"
BENCHMARK_URL = "https://openrouter.ai/api/v1/benchmarks"
TTL = 6 * 3600
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 20000
SOURCES = {"artificial-analysis": "https://artificialanalysis.ai/", "design-arena": "https://designarena.org/"}
FEEDS = {"catalog": CATALOG_URL, "artificial-analysis": BENCHMARK_URL + "?source=artificial-analysis",
         **{"design-arena/" + arena: BENCHMARK_URL + "?source=design-arena&arena=" + arena
            for arena in ("models", "builders", "agents")}}
METRICS = {"intelligence_index": ("higher", "index", "intelligence"),
           "coding_index": ("higher", "index", "coding"), "agentic_index": ("higher", "index", "agentic"),
           "elo": ("higher", "Elo", None), "win_rate": ("higher", "percent", None),
           "rank": ("lower", "rank among OpenRouter models", None),
           "avg_generation_time_ms": ("lower", "milliseconds", None)}
GUIDANCE = ("Compare only the same source, metric, arena, category and evaluation snapshot. "
            "No combined score is computed. Missing means unmeasured or unavailable, never zero. "
            "Benchmark results suggest candidates, not verified task success or provider availability. "
            "Cursor SDK identities are not automatically equated to OpenRouter identities.")


class BenchmarkError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BenchmarkError("Benchmark source redirected; credentials were not forwarded.")


def fetch_json(url, headers=None):
    if url not in FEEDS.values():
        raise BenchmarkError("Unsupported benchmark source URL.")
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ModelDeck/2", **(headers or {})})
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=8) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise BenchmarkError("Benchmark source exceeded the response size limit.")
    return json.loads(raw)


def _number(value):
    try:
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None
    except OverflowError:
        return None


def _text(value, limit=300):
    return value[:limit] if isinstance(value, str) else None


def _document(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise BenchmarkError("Benchmark source returned an unsupported response shape.")
    if len(payload["data"]) > MAX_ROWS:
        raise BenchmarkError("Benchmark source exceeded the row limit.")
    links = payload.get("links")
    if isinstance(links, dict) and links.get("next"):
        raise BenchmarkError("Benchmark source returned an incomplete paginated response.")
    count = payload.get("total_count")
    if isinstance(count, int) and count > len(payload["data"]):
        raise BenchmarkError("Benchmark source returned an incomplete model catalog.")
    return payload["data"]


def _clean_scores(row, source, feed, identity, meta, arena=None, category=None):
    fields = ("intelligence_index", "coding_index", "agentic_index") if source == "artificial-analysis" else ("elo", "win_rate", "rank", "avg_generation_time_ms")
    result = []
    for metric in fields:
        score = _number(row.get(metric))
        if score is None:
            continue
        direction, unit, task = METRICS[metric]
        result.append({"source": source, "feed": feed, "metric": metric, "score": score,
                       "direction": direction, "unit": unit, "task": task or category,
                       "arena": arena, "category": category, "benchmark_model_id": identity,
                       "source_url": _text(meta.get("source_url"), 500) or SOURCES[source],
                       "citation": _text(meta.get("citation"), 2000), "as_of": _text(meta.get("as_of")),
                       "version": _text(meta.get("version"))})
    return result


def normalize(payload, feed):
    """Keep only documented evidence fields; credentials and unrelated payload never enter cache."""
    rows = _document(payload)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    models, scores, unknown = {}, [], set()
    invalid_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            invalid_rows += 1
            continue
        identity = _text(row.get("id" if feed == "catalog" else "model_permaslug"))
        if not identity:
            invalid_rows += 1
            continue
        if feed == "catalog":
            models[identity] = {"id": identity, "name": _text(row.get("name")) or identity,
                                "canonical_slug": _text(row.get("canonical_slug"))}
            benchmarks = row.get("benchmarks")
            if not isinstance(benchmarks, dict):
                continue
            unknown.update(set(benchmarks) - {"artificial_analysis", "design_arena"})
            aa = benchmarks.get("artificial_analysis")
            if isinstance(aa, dict):
                scores.extend(_clean_scores(aa, "artificial-analysis", feed, identity, meta))
            arena_rows = benchmarks.get("design_arena") or []
            if not isinstance(arena_rows, list):
                invalid_rows += 1
                continue
        else:
            expected = feed.split("/")[0]
            if row.get("source", expected) != expected:
                invalid_rows += 1
                continue
            if expected == "artificial-analysis":
                scores.extend(_clean_scores(row, expected, feed, identity, meta))
                continue
            arena_rows = [row]
        for arena_row in arena_rows:
            if not isinstance(arena_row, dict) or not isinstance(arena_row.get("arena"), str) or not isinstance(arena_row.get("category"), str):
                invalid_rows += 1
                continue
            scores.extend(_clean_scores(arena_row, "design-arena", feed, identity, meta,
                                        _text(arena_row["arena"]), _text(arena_row["category"])))
    if rows and not models and not scores and not invalid_rows:
        # A legitimate source can have only missing scores; do not invent evidence.
        pass
    if rows and invalid_rows and not scores and feed != "catalog":
        raise BenchmarkError("Benchmark source contains no valid evidence rows.")
    if feed == "catalog" and not models:
        raise BenchmarkError("Benchmark catalog contains no valid model identities.")
    return {"models": models, "scores": scores, "upstream_rows": len(rows),
            "invalid_rows": invalid_rows, "unsupported_benchmark_keys": sorted(unknown)[:50]}


class BenchmarkStore:
    def __init__(self, path=None, fetch=None, clock=None):
        self.path = Path(path) if path else support_directory() / "benchmarks-cache.json"
        self.fetch = fetch or fetch_json
        self.clock = clock or time.time
        self.feeds = {}
        self.cache_error = None
        self.last_attempt = None
        try:
            if not self.path.is_symlink() and self.path.stat().st_size <= MAX_BYTES:
                data = json.loads(self.path.read_text())
                if data.get("version") == 1 and isinstance(data.get("feeds"), dict):
                    self.feeds = {key: value for key, value in data["feeds"].items() if key in FEEDS and isinstance(value, dict)
                                  and isinstance(value.get("scores"), list) and isinstance(value.get("models"), dict)
                                  and _number(value.get("fetched_at")) is not None}
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self):
        temporary = None
        try:
            if self.path.is_symlink():
                raise OSError("symlink")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, prefix=".benchmarks-", delete=False) as output:
                temporary = output.name
                json.dump({"version": 1, "feeds": self.feeds}, output, allow_nan=False)
            os.replace(temporary, self.path)
            self.cache_error = None
        except (OSError, ValueError):
            self.cache_error = "Benchmark cache could not be saved; results are available in this process."
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def refresh(self, headers=None):
        """One public request, optionally four authenticated metadata feeds; no retries."""
        self.last_attempt = self.clock()
        requested = list(FEEDS) if headers else ["catalog"]
        def fetch_feed(feed):
            payload = self.fetch(FEEDS[feed], headers if feed != "catalog" else {})
            return normalize(payload, feed)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = {feed: pool.submit(fetch_feed, feed) for feed in requested}
            for feed, future in futures.items():
                try:
                    normalized = future.result()
                    self.feeds[feed] = {**normalized, "fetched_at": self.clock(), "error": None}
                except Exception:
                    previous = self.feeds.get(feed, {"models": {}, "scores": [], "fetched_at": 0})
                    self.feeds[feed] = {**previous, "error": "Source unavailable or malformed; any previous evidence is retained as stale."}
        self._save()
        return self.status()

    def ensure(self):
        catalog = self.feeds.get("catalog")
        if (not catalog or self.clock() - catalog.get("fetched_at", 0) >= TTL) and (self.last_attempt is None or self.clock() - self.last_attempt >= 60):
            self.refresh()
        return self

    def _stale(self, feed):
        return bool(feed.get("error")) or self.clock() - feed.get("fetched_at", 0) >= TTL

    def evidence(self):
        catalog = self.feeds.get("catalog", {}).get("models", {})
        aliases = {}
        for model_id, model in catalog.items():
            canonical = model.get("canonical_slug")
            if canonical:
                aliases.setdefault(canonical, []).append(model_id)
        selected, unmatched = {}, []
        for feed_id, feed in self.feeds.items():
            for score in feed.get("scores", []):
                if not isinstance(score, dict) or _number(score.get("score")) is None:
                    continue
                identity = score.get("benchmark_model_id")
                ids = [identity] if identity in catalog else aliases.get(identity, [])
                if not ids:
                    unmatched.append({"benchmark_model_id": identity, "source": score.get("source"), "feed": feed_id})
                for model_id in ids:
                    key = (model_id, score.get("source"), score.get("metric"), score.get("arena"), score.get("category"))
                    candidate = {**score, "model": model_id, "identity_match": "exact_id" if identity == model_id else "catalog_canonical_slug",
                                 "fetched_at": feed["fetched_at"], "stale": self._stale(feed)}
                    previous = selected.get(key)
                    # Fresh evidence wins; authenticated provenance breaks equally fresh ties.
                    priority = (not candidate["stale"], feed_id != "catalog", candidate["fetched_at"])
                    old_priority = (not previous["stale"], previous["feed"] != "catalog", previous["fetched_at"]) if previous else None
                    if previous is None or priority > old_priority:
                        selected[key] = candidate
        return catalog, list(selected.values()), unmatched

    def status(self):
        catalog, scores, unmatched = self.evidence()
        feeds = []
        for feed_id, url in FEEDS.items():
            feed = self.feeds.get(feed_id, {})
            feeds.append({"feed": feed_id, "url": url, "loaded": bool(feed.get("fetched_at")),
                          "fetched_at": feed.get("fetched_at"), "stale": self._stale(feed),
                          "error": feed.get("error"), "requires_saved_openrouter_key": feed_id != "catalog",
                          "score_count": len(feed.get("scores", [])), "invalid_rows": feed.get("invalid_rows", 0),
                          "unsupported_benchmark_keys": feed.get("unsupported_benchmark_keys", [])})
        unmatched_unique = {json.dumps(row, sort_keys=True): row for row in unmatched}
        return {"catalog_models": len(catalog), "models_with_scores": len({row["model"] for row in scores}),
                "models_without_scores": len(catalog) - len({row["model"] for row in scores}),
                "score_count": len(scores), "unmatched_identities": len(unmatched_unique),
                "unmatched_sample": list(unmatched_unique.values())[:20], "feeds": feeds,
                "metrics": sorted({row["metric"] for row in scores}),
                "tasks": sorted({row["task"] for row in scores if row.get("task")})[:100],
                "task_count": len({row["task"] for row in scores if row.get("task")}),
                "cache_error": self.cache_error, "guidance": GUIDANCE,
                "coverage_note": "All returned catalog models are retained, including unscored models. Only published supported metrics are normalized. Public catalog has no publisher evaluation timestamp; fetched_at is retrieval time."}

    def profile(self, model, limit=100, offset=0):
        catalog, scores, _ = self.evidence()
        matching = [score for score in scores if score["model"] == model]
        return {"model": model, "in_catalog": model in catalog, "identity": catalog.get(model),
                "score_count": len(matching), "scores": matching[offset:offset + limit],
                "next_offset": offset + limit if offset + limit < len(matching) else None,
                "missing_reason": None if matching else "No exact catalog-matched published scores available.", "guidance": GUIDANCE}

    def compare(self, models, task=None, limit=50, offset=0):
        _, scores, _ = self.evidence()
        groups = {}
        for score in scores:
            if score["model"] not in models or task and score.get("task") != task:
                continue
            key = (score["source"], score["metric"], score.get("arena") or "", score.get("category") or "", score.get("as_of") or "", score["feed"])
            groups.setdefault(key, []).append(score)
        comparisons = []
        for key, rows in sorted(groups.items()):
            rows.sort(key=lambda row: ((-row["score"] if row["direction"] == "higher" else row["score"]), row["model"]))
            comparisons.append({"source": key[0], "metric": key[1], "arena": key[2] or None, "category": key[3] or None,
                                "as_of": key[4] or None, "scores": rows,
                                "missing_models": [model for model in models if model not in {row["model"] for row in rows}]})
        return {"models": models, "comparison_count": len(comparisons), "comparisons": comparisons[offset:offset + limit],
                "next_offset": offset + limit if offset + limit < len(comparisons) else None, "guidance": GUIDANCE}

    def rank(self, task, source="artificial-analysis", metric=None, arena="models", limit=20, offset=0):
        _, scores, _ = self.evidence()
        metric = metric or (task + "_index" if source == "artificial-analysis" else "elo")
        rows = [score for score in scores if score["source"] == source and score["metric"] == metric
                and score.get("task") == task and (source != "design-arena" or score.get("arena") == arena)]
        snapshots = {}
        for row in rows:
            key = (row.get("as_of"), row["feed"], row["fetched_at"], row["stale"])
            snapshots.setdefault(key, []).append(row)
        if len(snapshots) > 1:
            rankings = []
            for key, snapshot_rows in sorted(snapshots.items(), key=lambda item: str(item[0])):
                snapshot_rows.sort(key=lambda row: ((-row["score"] if row["direction"] == "higher" else row["score"]), row["model"]))
                rankings.append({"as_of": key[0], "feed": key[1], "fetched_at": key[2], "stale": key[3],
                                 "matches": len(snapshot_rows), "models": snapshot_rows[offset:offset + limit],
                                 "next_offset": offset + limit if offset + limit < len(snapshot_rows) else None})
            return {"task": task, "source": source, "metric": metric, "matches": len(rows), "models": [],
                    "rankings_by_snapshot": rankings, "guidance": GUIDANCE,
                    "note": "Evidence has different feed snapshots; rankings are kept separate. Offset applies within each snapshot."}
        rows.sort(key=lambda row: ((-row["score"] if row["direction"] == "higher" else row["score"]), row["model"]))
        return {"task": task, "source": source, "metric": metric, "arena": arena if source == "design-arena" else None,
                "matches": len(rows), "models": rows[offset:offset + limit],
                "next_offset": offset + limit if offset + limit < len(rows) else None,
                "guidance": GUIDANCE, "availability": "Catalog presence only; live provider availability has not been checked."}
