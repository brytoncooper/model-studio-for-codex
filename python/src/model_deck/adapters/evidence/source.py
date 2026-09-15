"""Bridge the price/benchmark fetchers onto the engine's EvidenceSourcePort.

``prices.py`` and ``benchmarks.py`` are plain functions over an injected
transport; the engine's refresh use case expects an object with
``fetch_prices()`` and ``fetch_benchmarks()`` returning ``FetchedEvidence``.
This is that object, and it is the only place the two vocabularies meet.

Two conversions happen here and are worth naming:

* **Provenance moves to the snapshot.** ``FetchedEvidence`` carries one
  provenance for the whole fetch and the engine attaches it to each record on
  read, so each record body is sent provenance-free. Benchmark rows whose
  per-record provenance named the underlying publisher therefore keep their
  ``source_id`` field (a body field) but lose their per-row provenance object.
* **One benchmark feed per refresh.** ``fetch_benchmarks`` in the adapter reads
  a single feed. Merging feeds would need a rule for ranking and de-duplicating
  rows across publishers under one bounded cache, so this composes exactly one
  feed and names it.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from model_deck.adapters.evidence.benchmarks import (
    FEED_ARTIFICIAL_ANALYSIS,
    FEEDS,
    fetch_benchmarks,
)
from model_deck.adapters.evidence.prices import fetch_prices
from model_deck.adapters.evidence.provenance import SourceProvenance
from model_deck.engine.evidence.ports import FetchedEvidence

__all__ = ["DEFAULT_BENCHMARK_FEED", "HttpEvidenceSource"]

DEFAULT_BENCHMARK_FEED = FEED_ARTIFICIAL_ANALYSIS


class HttpEvidenceSource:
    """An ``EvidenceSourcePort`` over the injected byte transport.

    Fetching is explicit: this object only reaches the network when a refresh
    job calls it, and a failure raises rather than returning a partial answer,
    so the engine keeps the previous snapshot.
    """

    def __init__(
        self,
        *,
        transport: Callable[[str], bytes],
        benchmark_feed: str = DEFAULT_BENCHMARK_FEED,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not callable(transport):
            raise TypeError("transport must be a callable returning bytes")
        if benchmark_feed not in FEEDS:
            raise ValueError(f"unknown benchmark feed: {benchmark_feed!r}")
        self._transport = transport
        self._benchmark_feed = benchmark_feed
        self._clock = clock

    def fetch_prices(self) -> FetchedEvidence:
        snapshot = fetch_prices(self._transport, self._clock())
        return _as_fetched(snapshot.provenance, snapshot.records)

    def fetch_benchmarks(self) -> FetchedEvidence:
        snapshot = fetch_benchmarks(
            self._transport, self._benchmark_feed, self._clock()
        )
        return _as_fetched(snapshot.provenance, snapshot.records)


def _as_fetched(provenance: SourceProvenance, records: Any) -> FetchedEvidence:
    return FetchedEvidence(
        source_id=provenance.source_id,
        fetched_at=provenance.fetched_at,
        records=tuple(_body(record.to_wire()) for record in records),
        source_url=provenance.source_url,
        citation=provenance.citation,
        as_of=provenance.as_of,
    )


def _body(wire: Mapping[str, Any]) -> dict[str, Any]:
    """A record body without its provenance; the snapshot supplies that on read."""
    return {key: value for key, value in wire.items() if key != "provenance"}
