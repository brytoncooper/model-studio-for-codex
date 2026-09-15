"""Ports and value types for cached price and benchmark evidence (B16).

Reads are cache-only. Nothing in this package fetches: the source port is
satisfied by an adapter, and only an explicit refresh ever calls it.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

PRICES_KIND = "prices"
BENCHMARKS_KIND = "benchmarks"
EVIDENCE_KINDS: frozenset[str] = frozenset({PRICES_KIND, BENCHMARKS_KIND})

# Caching and serving are bounded by different things, so they carry different
# numbers. Holding one bound for both is what made a real refresh impossible:
# a catalog too large for one answer was refused outright, so nothing was
# cached, every price stayed unknown and no usage record could be estimated.
#
# The cache holds a whole source catalog. Its bound is the fetching adapter's
# own truncation point (adapters/evidence/prices.py stops at 4096 models), so a
# refresh never refuses a catalog the adapter was willing to read.
MAX_CACHED_PRICE_RECORDS = 4096
MAX_CACHED_BENCHMARK_RECORDS = 4096
MAX_CACHED_RECORDS_BY_KIND: Mapping[str, int] = {
    PRICES_KIND: MAX_CACHED_PRICE_RECORDS,
    BENCHMARKS_KIND: MAX_CACHED_BENCHMARK_RECORDS,
}

# One served answer must fit a single transport frame (1 MiB in
# adapters/transport/framing.py), which at roughly 300 bytes per record is the
# tighter limit. These are the maxItems the frozen result schemas carry. A query
# whose match set exceeds its bound is refused rather than silently truncated;
# the caller narrows it with registration_id, provider_model_id or model_id, and
# a lookup for one model (price_for) is never near the bound.
MAX_PRICE_RECORDS = 2048
MAX_BENCHMARK_RECORDS = 2048

# How old a successful fetch may be before its records are reported stale.
DEFAULT_EVIDENCE_MAX_AGE_SECONDS = 24 * 60 * 60

# Identity reported for a kind that has never been fetched successfully. The
# snapshot provenance is required even for an empty cache, so the engine names
# itself as the source rather than inventing a feed it has never read.
ENGINE_EVIDENCE_SOURCE_IDS: Mapping[str, str] = {
    PRICES_KIND: "com.modeldeck.engine.prices",
    BENCHMARKS_KIND: "com.modeldeck.engine.benchmarks",
}
NEVER_FETCHED_AT = "1970-01-01T00:00:00Z"

# Refresh failure codes. These are the only values ever written to
# last_refresh_error: fixed, payload-free, and carrying no URL or credential.
NEVER_REFRESHED_ERROR = "never_refreshed"
REFRESH_FETCH_FAILED = "fetch_failed"
REFRESH_INVALID_PAYLOAD = "invalid_payload"
REFRESH_TOO_MANY_RECORDS = "too_many_records"
REFRESH_ERROR_CODES: frozenset[str] = frozenset(
    {
        NEVER_REFRESHED_ERROR,
        REFRESH_FETCH_FAILED,
        REFRESH_INVALID_PAYLOAD,
        REFRESH_TOO_MANY_RECORDS,
    }
)


class EvidenceKindError(ValueError):
    """The named evidence kind is not one this engine caches."""


class EvidenceTimestampError(ValueError):
    """A timestamp was not an offset-carrying ISO 8601 instant."""


class EvidenceQueryValidationError(ValueError):
    """Query parameters did not satisfy the frozen params schema."""


class EvidenceResourceExhaustedError(ValueError):
    """A cached answer exceeds the frozen result bound for its kind."""


class EvidenceSourceUnavailableError(RuntimeError):
    """No source is composed for the requested evidence kind."""


def require_evidence_kind(kind: str) -> str:
    if kind not in EVIDENCE_KINDS:
        raise EvidenceKindError(f"unsupported evidence kind: {kind!r}")
    return kind


def utc_now_iso8601() -> str:
    """Default clock for refresh and staleness arithmetic."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def instant_seconds(value: str) -> float:
    """Seconds since the Unix epoch for an offset-carrying ISO 8601 instant."""
    if not isinstance(value, str):
        raise EvidenceTimestampError("timestamp must be a string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise EvidenceTimestampError("timestamp must be ISO 8601") from None
    if parsed.tzinfo is None:
        raise EvidenceTimestampError("timestamp must carry a UTC offset")
    return parsed.timestamp()


def compute_stale(
    *,
    fetched_at: str | None,
    now: str,
    max_age_seconds: float,
    last_refresh_error: str | None,
) -> bool:
    """Stale when the last refresh failed or the cached value is past its window."""
    if last_refresh_error is not None:
        return True
    if fetched_at is None:
        return True
    return instant_seconds(now) - instant_seconds(fetched_at) > max_age_seconds


_PROVENANCE_REQUIRED = ("source_id", "fetched_at", "stale")
_PROVENANCE_OPTIONAL = ("source_url", "citation", "as_of", "last_refresh_error")


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """Where a cached value came from and how old it is."""

    source_id: str
    fetched_at: str
    stale: bool
    source_url: str | None = None
    citation: str | None = None
    as_of: str | None = None
    last_refresh_error: str | None = None

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> SourceProvenance:
        return cls(
            **{name: payload[name] for name in _PROVENANCE_REQUIRED},
            **{name: payload.get(name) for name in _PROVENANCE_OPTIONAL},
        )

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            name: getattr(self, name) for name in _PROVENANCE_REQUIRED
        }
        for name in _PROVENANCE_OPTIONAL:
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload


# The unit kinds price_record prices. Every other usage unit_kind is unpriced,
# which makes its estimate unknown rather than zero.
PRICED_UNIT_KINDS = ("input_tokens", "output_tokens", "cached_tokens")


@dataclass(frozen=True, slots=True)
class UnitPrices:
    """Price for one unit of each priced unit_kind. None is unknown, never zero."""

    input_tokens: float | None = None
    output_tokens: float | None = None
    cached_tokens: float | None = None

    def price_for(self, unit_kind: str) -> float | None:
        """None both for an unpriced unit kind and for a kind priced as null."""
        if unit_kind not in PRICED_UNIT_KINDS:
            return None
        return getattr(self, unit_kind)

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> UnitPrices:
        return cls(**{name: payload.get(name) for name in PRICED_UNIT_KINDS})

    def to_wire(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in PRICED_UNIT_KINDS}


@dataclass(frozen=True, slots=True)
class PriceRecord:
    provider_model_id: str
    currency: str | None
    unit_prices: UnitPrices
    provenance: SourceProvenance
    registration_id: str | None = None

    @classmethod
    def from_body(
        cls, body: Mapping[str, Any], provenance: SourceProvenance
    ) -> PriceRecord:
        return cls(
            provider_model_id=body["provider_model_id"],
            currency=body.get("currency"),
            unit_prices=UnitPrices.from_wire(body.get("unit_prices") or {}),
            provenance=provenance,
            registration_id=body.get("registration_id"),
        )

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider_model_id": self.provider_model_id,
            "currency": self.currency,
            "unit_prices": self.unit_prices.to_wire(),
            "provenance": self.provenance.to_wire(),
        }
        if self.registration_id is not None:
            payload["registration_id"] = self.registration_id
        return payload


@dataclass(frozen=True, slots=True)
class BenchmarkRecord:
    source_model_ref: str
    provider_model_id: str | None
    source_id: str
    feed: str
    metric: str
    score: float | None
    provenance: SourceProvenance

    @classmethod
    def from_body(
        cls, body: Mapping[str, Any], provenance: SourceProvenance
    ) -> BenchmarkRecord:
        return cls(
            source_model_ref=body["source_model_ref"],
            provider_model_id=body.get("provider_model_id"),
            source_id=body["source_id"],
            feed=body["feed"],
            metric=body["metric"],
            score=body.get("score"),
            provenance=provenance,
        )

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


@dataclass(frozen=True, slots=True)
class AllowanceWindow:
    start: str
    end: str

    def to_wire(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True, slots=True)
class SubscriptionAllowance:
    """A prepaid or included allowance, never mixed into settled or estimated money."""

    provider_id: str
    window: AllowanceWindow
    allowance: float | None
    used: float | None
    provenance: SourceProvenance
    currency: str | None = None

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider_id": self.provider_id,
            "window": self.window.to_wire(),
            "allowance": self.allowance,
            "used": self.used,
            "provenance": self.provenance.to_wire(),
        }
        if self.currency is not None:
            payload["currency"] = self.currency
        return payload


_SNAPSHOT_SOURCE_FIELDS = ("source_id", "fetched_at", "source_url", "citation", "as_of")


@dataclass(frozen=True, slots=True)
class FetchedEvidence:
    """One successful read of a source, before it becomes the cached snapshot.

    `records` are provenance-free record bodies. Provenance is attached from the
    snapshot on read, so freshness is computed at read time and never persisted.
    """

    source_id: str
    fetched_at: str
    records: tuple[Mapping[str, Any], ...] = ()
    source_url: str | None = None
    citation: str | None = None
    as_of: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    """The cached state of one evidence kind.

    `fetched_at` is None when the kind has never been fetched successfully; such
    a snapshot exists only to carry the failure of the refresh that tried.
    """

    kind: str
    source_id: str | None = None
    fetched_at: str | None = None
    records: tuple[Mapping[str, Any], ...] = ()
    source_url: str | None = None
    citation: str | None = None
    as_of: str | None = None
    last_refresh_error: str | None = None
    attempted_at: str | None = None

    @classmethod
    def from_fetch(cls, kind: str, fetched: FetchedEvidence) -> EvidenceSnapshot:
        return cls(
            kind=require_evidence_kind(kind),
            source_id=fetched.source_id,
            fetched_at=fetched.fetched_at,
            records=tuple(dict(record) for record in fetched.records),
            source_url=fetched.source_url,
            citation=fetched.citation,
            as_of=fetched.as_of,
        )

    def to_document(self) -> dict[str, Any]:
        """The durable JSON body. Failure fields are stored separately."""
        document: dict[str, Any] = {
            "records": [dict(record) for record in self.records]
        }
        for name in _SNAPSHOT_SOURCE_FIELDS:
            value = getattr(self, name)
            if value is not None:
                document[name] = value
        return document

    @classmethod
    def from_document(
        cls,
        kind: str,
        document: Mapping[str, Any] | None,
        *,
        last_refresh_error: str | None = None,
        attempted_at: str | None = None,
    ) -> EvidenceSnapshot:
        body = document or {}
        return cls(
            kind=require_evidence_kind(kind),
            records=tuple(dict(record) for record in body.get("records", ())),
            last_refresh_error=last_refresh_error,
            attempted_at=attempted_at,
            **{name: body.get(name) for name in _SNAPSHOT_SOURCE_FIELDS},
        )


@runtime_checkable
class EvidenceSourcePort(Protocol):
    """Fetches evidence from outside the process. Only a refresh ever calls it."""

    def fetch_prices(self) -> FetchedEvidence:
        ...

    def fetch_benchmarks(self) -> FetchedEvidence:
        ...


@runtime_checkable
class EvidenceCacheRepository(Protocol):
    """Durable home of the last good snapshot per kind, plus its last failure."""

    def get_snapshot(self, kind: str) -> EvidenceSnapshot | None:
        ...

    def put_snapshot(self, kind: str, snapshot: EvidenceSnapshot) -> None:
        ...

    def record_refresh_failure(
        self, kind: str, attempted_at: str, error_code: str
    ) -> None:
        ...
