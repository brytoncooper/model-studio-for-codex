"""OpenRouter benchmark-feed source adapter, ported from the legacy root `model_benchmarks.py`.

Maps one-to-one onto `benchmark_record` and `source_provenance`
(contracts/engine.v1/vocabulary.schema.json). Unlike the legacy `BenchmarkStore`, this
module does not aggregate feeds, match identities against a model catalog, rank, or
compare — that orchestration belongs to the engine (C8c), which composes several
`BenchmarkFeedSnapshot`s (one per feed) into the `benchmarks.query`/`benchmarks.refresh`
methods. This module only fetches and parses one feed's evidence at a time.

`benchmark_record` (frozen by C8a) has no `arena`/`category` field, so a Design Arena row's
arena and category are folded into `feed` instead of being dropped — otherwise two
different-category scores could collide under the same (source_id, feed, metric) tuple and
silently overwrite each other downstream. See `_design_arena_feed_label` below. This is an
adapter-level choice, not a contract requirement; flag it to C8c if a different encoding is
wanted.

`provider_model_id` is always None here: mapping a feed's own `source_model_ref` to a
registered provider model needs the model registry, which this adapter does not have. An
unmapped row stays visible (per the C8a benchmark_record shape) rather than being dropped.

Refusal to invent evidence, carried over from the legacy code: a metric the row's JSON
never mentions is skipped (no record). A metric the row *does* mention but with no usable
number (JSON null, non-numeric, NaN/Inf) becomes a record with `score: null` — explicit
"measured, no value" rather than silence or a fabricated 0.
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from model_deck_contracts.json_util import canonical_json_bytes

from model_deck.adapters.evidence.provenance import (
    SourceProvenance,
    format_fetched_at,
    is_stale_at,
    normalize_as_of,
)

CATALOG_URL = "https://openrouter.ai/api/v1/models?output_modalities=all"
BENCHMARK_URL = "https://openrouter.ai/api/v1/benchmarks"

FEED_CATALOG = "catalog"
FEED_ARTIFICIAL_ANALYSIS = "artificial-analysis"

FEEDS: dict[str, str] = {
    FEED_CATALOG: CATALOG_URL,
    FEED_ARTIFICIAL_ANALYSIS: BENCHMARK_URL + "?source=artificial-analysis",
    **{
        f"design-arena/{arena}": BENCHMARK_URL + f"?source=design-arena&arena={arena}"
        for arena in ("models", "builders", "agents")
    },
}

# Reverse-domain id of the endpoint every feed above is read from.
SOURCE_ID = "ai.openrouter"
# Reverse-domain ids and homepages of the underlying publishers the feeds cite. Distinct
# from SOURCE_ID: these describe where the *evidence* originates, not which HTTP endpoint
# this adapter called.
PUBLISHER_SOURCE_IDS = {
    "artificial-analysis": "ai.artificialanalysis",
    "design-arena": "org.designarena",
}
PUBLISHER_HOMEPAGES = {
    "artificial-analysis": "https://artificialanalysis.ai/",
    "design-arena": "https://designarena.org/",
}
METRICS_BY_PUBLISHER = {
    "artificial-analysis": ("intelligence_index", "coding_index", "agentic_index"),
    "design-arena": ("elo", "win_rate", "rank", "avg_generation_time_ms"),
}

DEFAULT_MAX_AGE_SECONDS = 6 * 3600  # matches the legacy TTL
MAX_ROWS = 20000
_FEED_LABEL_LIMIT = 128
_CITATION_LIMIT = 512  # source_provenance.citation maxLength (the legacy code allowed 2000)
_SOURCE_URL_LIMIT = 500


class BenchmarkSourceError(Exception):
    """Base error for the benchmark adapter. A failure never returns a partial snapshot."""


class BenchmarkTransportError(BenchmarkSourceError):
    """The transport callable failed before any parsing happened."""


class BenchmarkFormatError(BenchmarkSourceError):
    """The source (or a persisted snapshot document) did not match the expected shape."""


@dataclass(frozen=True, slots=True)
class BenchmarkRecord:
    """One `benchmark_record`: one score, for one model, on one metric, from one feed."""

    source_model_ref: str
    provider_model_id: str | None
    source_id: str
    feed: str
    metric: str
    score: float | None
    provenance: SourceProvenance

    def to_wire(self) -> dict[str, Any]:
        return {
            "source_model_ref": self.source_model_ref,
            "provider_model_id": self.provider_model_id,
            "source_id": self.source_id,
            "feed": self.feed,
            "metric": self.metric,
            "score": self.score,
            "provenance": self.provenance.to_wire(),
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> "BenchmarkRecord":
        return cls(
            source_model_ref=str(payload["source_model_ref"]),
            provider_model_id=payload["provider_model_id"],
            source_id=str(payload["source_id"]),
            feed=str(payload["feed"]),
            metric=str(payload["metric"]),
            score=payload["score"],
            provenance=SourceProvenance.from_wire(payload["provenance"]),
        )


@dataclass(frozen=True, slots=True)
class BenchmarkFeedSnapshot:
    """A full read of one feed at one point in time: every score row from that feed's
    document, plus the feed-level provenance (the HTTP call this adapter made)."""

    feed: str
    records: tuple[BenchmarkRecord, ...]
    provenance: SourceProvenance

    def to_wire(self) -> dict[str, Any]:
        return {
            "feed": self.feed,
            "fetched_at": self.provenance.fetched_at,
            "provenance": self.provenance.to_wire(),
            "benchmarks": [record.to_wire() for record in self.records],
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> "BenchmarkFeedSnapshot":
        provenance = SourceProvenance.from_wire(payload["provenance"])
        records = tuple(BenchmarkRecord.from_wire(row) for row in payload["benchmarks"])
        return cls(feed=str(payload["feed"]), records=records, provenance=provenance)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _source_url(value: Any, publisher: str) -> str:
    text = _text(value, _SOURCE_URL_LIMIT)
    if text and (text.startswith("http://") or text.startswith("https://")):
        return text
    return PUBLISHER_HOMEPAGES[publisher]


def _rows_from_payload(payload: Any) -> list[Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise BenchmarkFormatError("Unexpected benchmark document: expected an object with a 'data' array")
    rows = payload["data"]
    if len(rows) > MAX_ROWS:
        raise BenchmarkFormatError("Benchmark source exceeded the row limit")
    links = payload.get("links")
    if isinstance(links, dict) and links.get("next"):
        raise BenchmarkFormatError("Benchmark source returned an incomplete paginated response")
    total_count = payload.get("total_count")
    if isinstance(total_count, int) and total_count > len(rows):
        raise BenchmarkFormatError("Benchmark source returned an incomplete catalog")
    return rows


def _row_provenance(publisher: str, meta: Mapping[str, Any], now: float) -> SourceProvenance:
    return SourceProvenance(
        source_id=PUBLISHER_SOURCE_IDS[publisher],
        source_url=_source_url(meta.get("source_url"), publisher),
        citation=_text(meta.get("citation"), _CITATION_LIMIT),
        as_of=normalize_as_of(meta.get("as_of")),
        fetched_at=format_fetched_at(now),
        stale=False,
    )


def _metric_records(
    row: Mapping[str, Any],
    publisher: str,
    feed_label: str,
    source_model_ref: str,
    meta: Mapping[str, Any],
    now: float,
) -> list[BenchmarkRecord]:
    records: list[BenchmarkRecord] = []
    for metric in METRICS_BY_PUBLISHER[publisher]:
        if metric not in row:
            continue  # the source never reports this metric class for this row; don't invent it
        records.append(
            BenchmarkRecord(
                source_model_ref=source_model_ref,
                provider_model_id=None,  # mapping to a registered model is an engine concern
                source_id=PUBLISHER_SOURCE_IDS[publisher],
                feed=feed_label[:_FEED_LABEL_LIMIT],
                metric=metric,
                score=_finite_number(row.get(metric)),  # null stays null, never coerced to 0
                provenance=_row_provenance(publisher, meta, now),
            )
        )
    return records


def _design_arena_feed_label(base_feed: str, arena: str, category: str) -> str:
    """Fold arena/category into the feed label so two categories never collide under the
    same (source_id, feed, metric) key. `base_feed` for a standalone design-arena feed
    already names the arena (e.g. "design-arena/models"); for the catalog feed it does
    not, so arena is included too."""
    if base_feed == FEED_CATALOG:
        return f"{base_feed}/design-arena/{arena}/{category}"
    return f"{base_feed}/{category}"


def _records_from_catalog_row(
    row: Mapping[str, Any], identity: str, feed: str, meta: Mapping[str, Any], now: float
) -> list[BenchmarkRecord]:
    benchmarks = row.get("benchmarks")
    if not isinstance(benchmarks, dict):
        return []
    records: list[BenchmarkRecord] = []
    artificial_analysis = benchmarks.get("artificial_analysis")
    if isinstance(artificial_analysis, dict):
        records.extend(_metric_records(artificial_analysis, "artificial-analysis", feed, identity, meta, now))
    arena_rows = benchmarks.get("design_arena")
    if isinstance(arena_rows, list):
        for arena_row in arena_rows:
            if not isinstance(arena_row, dict):
                continue
            arena, category = arena_row.get("arena"), arena_row.get("category")
            if not isinstance(arena, str) or not isinstance(category, str):
                continue
            label = _design_arena_feed_label(feed, arena, category)
            records.extend(_metric_records(arena_row, "design-arena", label, identity, meta, now))
    return records


def _records_from_feed_row(
    row: Mapping[str, Any], feed: str, meta: Mapping[str, Any], now: float
) -> list[BenchmarkRecord] | None:
    """None means the row itself was invalid (counts toward `invalid_rows`); an empty list
    means a valid row that happened to carry none of the expected metric keys."""
    source_model_ref = _text(row.get("model_permaslug"), 256)
    if not source_model_ref:
        return None
    expected_publisher = feed.split("/")[0]
    if row.get("source", expected_publisher) != expected_publisher:
        return None
    if expected_publisher == "artificial-analysis":
        return _metric_records(row, expected_publisher, feed, source_model_ref, meta, now)
    arena, category = row.get("arena"), row.get("category")
    if not isinstance(arena, str) or not isinstance(category, str):
        return None
    label = _design_arena_feed_label(feed, arena, category)
    return _metric_records(row, "design-arena", label, source_model_ref, meta, now)


def parse_benchmarks(payload: Any, feed: str, now: float) -> BenchmarkFeedSnapshot:
    """Pure parse of one feed's document into a `BenchmarkFeedSnapshot`. Never touches the
    network. `now` (epoch seconds) becomes every record's `provenance.fetched_at` and the
    snapshot's own provenance, since all records come from the same feed fetch."""
    if feed not in FEEDS:
        raise BenchmarkFormatError(f"Unknown benchmark feed: {feed!r}")
    rows = _rows_from_payload(payload)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}

    records: list[BenchmarkRecord] = []
    catalog_identities = 0
    invalid_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            invalid_rows += 1
            continue
        if feed == FEED_CATALOG:
            identity = _text(row.get("id"), 256)
            if not identity:
                invalid_rows += 1
                continue
            catalog_identities += 1
            records.extend(_records_from_catalog_row(row, identity, feed, meta, now))
        else:
            row_records = _records_from_feed_row(row, feed, meta, now)
            if row_records is None:
                invalid_rows += 1
                continue
            records.extend(row_records)

    # A legitimate source can list models with no scores at all; that is not an error and
    # is not invented evidence. Only a wholly unusable response is rejected.
    if feed == FEED_CATALOG and rows and catalog_identities == 0:
        raise BenchmarkFormatError("Benchmark catalog contains no valid model identities")
    if feed != FEED_CATALOG and rows and invalid_rows == len(rows) and not records:
        raise BenchmarkFormatError("Benchmark source contains no valid evidence rows")

    snapshot_provenance = SourceProvenance(
        source_id=SOURCE_ID,
        source_url=FEEDS[feed],
        fetched_at=format_fetched_at(now),
        stale=False,
    )
    return BenchmarkFeedSnapshot(feed=feed, records=tuple(records), provenance=snapshot_provenance)


def fetch_benchmarks(fetcher: Callable[[str], bytes], feed: str, now: float) -> BenchmarkFeedSnapshot:
    """Fetch and parse one benchmark feed through an injected transport.

    `fetcher` is the only thing in this module that may reach the network — tests pass a
    fake; real callers pass `transport.default_transport` or their own bound transport.
    A transport failure raises `BenchmarkTransportError`; a malformed response raises
    `BenchmarkFormatError`. Neither returns a partial snapshot.
    """
    if feed not in FEEDS:
        raise BenchmarkFormatError(f"Unknown benchmark feed: {feed!r}")
    try:
        raw = fetcher(FEEDS[feed])
    except BenchmarkSourceError:
        raise
    except Exception as error:
        raise BenchmarkTransportError(f"Benchmark source transport failed: {error}") from error
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise BenchmarkFormatError(f"Benchmark source returned invalid JSON: {error}") from error
    return parse_benchmarks(payload, feed, now)


def is_stale(snapshot: BenchmarkFeedSnapshot, now: float, max_age: float = DEFAULT_MAX_AGE_SECONDS) -> bool:
    """True once `now` is at least `max_age` seconds past the snapshot's `fetched_at`."""
    return is_stale_at(snapshot.provenance.fetched_at, now, max_age)


def serialize_benchmark_snapshot(snapshot: BenchmarkFeedSnapshot) -> str:
    """Canonical JSON document for a feed snapshot, including top-level `fetched_at` and
    the full `provenance` object, for a cache file or the plugin_data store."""
    return canonical_json_bytes(snapshot.to_wire()).decode("utf-8")


def deserialize_benchmark_snapshot(document_text: str) -> BenchmarkFeedSnapshot:
    """Inverse of `serialize_benchmark_snapshot`. Never fetches; a malformed document
    raises `BenchmarkFormatError` rather than returning a partial snapshot."""
    try:
        document = json.loads(document_text)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise BenchmarkFormatError(f"Benchmark snapshot document is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise BenchmarkFormatError("Benchmark snapshot document must be a JSON object")
    try:
        return BenchmarkFeedSnapshot.from_wire(document)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise BenchmarkFormatError(f"Benchmark snapshot document is malformed: {error}") from error
