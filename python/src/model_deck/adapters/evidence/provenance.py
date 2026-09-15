"""Shared `source_provenance` wire type and staleness math for the evidence adapters.

Both `prices.py` and `benchmarks.py` cache values that carry a `source_provenance`
(contracts/engine.v1/vocabulary.schema.json#/definitions/source_provenance). This module
is the one place that shape is built, serialized, and aged so the two adapters stay in
lockstep with the frozen schema and with each other.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Swift's hand-rolled validator only accepts uuid, date-time and uri formats (see the C8a
# open questions), so source_provenance.as_of is a plain YYYY-MM-DD pattern, not format:"date".
AS_OF_LENGTH = 10


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """One `source_provenance` record. `fetched_at` and `stale` are always present."""

    source_id: str
    fetched_at: str
    stale: bool
    source_url: str | None = None
    citation: str | None = None
    as_of: str | None = None
    last_refresh_error: str | None = None

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_id": self.source_id,
            "fetched_at": self.fetched_at,
            "stale": self.stale,
        }
        if self.source_url is not None:
            payload["source_url"] = self.source_url
        if self.citation is not None:
            payload["citation"] = self.citation
        if self.as_of is not None:
            payload["as_of"] = self.as_of
        if self.last_refresh_error is not None:
            payload["last_refresh_error"] = self.last_refresh_error
        return payload

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> "SourceProvenance":
        return cls(
            source_id=str(payload["source_id"]),
            fetched_at=str(payload["fetched_at"]),
            stale=bool(payload["stale"]),
            source_url=payload.get("source_url"),
            citation=payload.get("citation"),
            as_of=payload.get("as_of"),
            last_refresh_error=payload.get("last_refresh_error"),
        )

    def with_staleness(self, stale: bool, *, last_refresh_error: str | None = None) -> "SourceProvenance":
        """A copy with `stale` (and optionally `last_refresh_error`) replaced.

        Used by a caller re-serving a cached snapshot: the provenance a record was
        parsed with never changes, but whether it still counts as fresh depends on the
        clock at read time, computed by `is_stale_at` below.
        """
        return SourceProvenance(
            source_id=self.source_id,
            fetched_at=self.fetched_at,
            stale=stale,
            source_url=self.source_url,
            citation=self.citation,
            as_of=self.as_of,
            last_refresh_error=last_refresh_error if last_refresh_error is not None else self.last_refresh_error,
        )


def format_fetched_at(now: float) -> str:
    """Epoch seconds -> wire iso8601 timestamp, matching the codebase's `...Z` UTC convention."""
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_fetched_at(value: str) -> float:
    """Wire iso8601 timestamp -> epoch seconds, for staleness math on a read-back snapshot."""
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"fetched_at is not a valid iso8601 timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def is_stale_at(fetched_at: str, now: float, max_age: float) -> bool:
    """True once `now` is at least `max_age` seconds past `fetched_at`. Never fetches."""
    return (now - parse_fetched_at(fetched_at)) >= max_age


_AS_OF_DIGITS = frozenset("0123456789")


def normalize_as_of(value: Any) -> str | None:
    """A source's own `YYYY-MM-DD` effective date, or None when absent or not that shape.

    A source publishing an unparseable date is treated the same as publishing none: the
    value is dropped rather than passed through and rejected later by the frozen schema.
    """
    if not isinstance(value, str) or len(value) != AS_OF_LENGTH:
        return None
    if value[4] != "-" or value[7] != "-":
        return None
    digits = value[0:4] + value[5:7] + value[8:10]
    if len(digits) != 8 or not set(digits) <= _AS_OF_DIGITS:
        return None
    return value
