"""OpenRouter list-price source adapter, ported from the legacy root `pricing.py`.

Maps one-to-one onto `price_record` and `source_provenance`
(contracts/engine.v1/vocabulary.schema.json). `unit_prices` always carries all three unit
kinds (input_tokens, output_tokens, cached_tokens); a source that publishes no price for
one is an explicit `null`, never a substituted zero.

Prices are per SINGLE unit (one token), matching the frozen contract and the engine's
`estimate_cost`, which multiplies a unit price by a raw token count. Nothing here scales
to per-million; the MCP presenter does that for display.

No implicit network: `parse_prices` never touches the network, and reading a persisted
snapshot back with `deserialize_price_snapshot` never fetches either. The only path to the
network is `fetch_prices`, and only when the caller supplies a transport callable.
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
)

PRICING_URL = "https://openrouter.ai/api/v1/models"
# Reverse-domain id of the endpoint these prices are read from (openrouter.ai).
SOURCE_ID = "ai.openrouter"
DEFAULT_MAX_AGE_SECONDS = 6 * 3600  # matches the legacy CACHE_TTL

# `price_record.unit_prices` is priced per SINGLE unit (one token), because that is what
# the frozen contract says and what the engine's `estimate_cost` multiplies by a raw token
# count. OpenRouter already publishes per-token prices, so no scaling happens here at all;
# the per-million figures humans read are produced at the presentation edge.
# Rounding only tames float noise: 12 decimal places on a per-token price is the same
# resolution the legacy code kept at 6 decimal places on a per-million price.
UNIT_PRICE_DECIMALS = 12
MAX_MODELS = 4096
# OpenRouter's public catalog prices every model in USD; the /models document itself
# carries no per-model currency field, so this is a fact about the endpoint, not a value
# invented per record.
CURRENCY = "USD"


class PriceSourceError(Exception):
    """Base error for the price adapter. A failure never returns a partial snapshot."""


class PriceTransportError(PriceSourceError):
    """The transport callable failed before any parsing happened."""


class PriceFormatError(PriceSourceError):
    """The source (or a persisted snapshot document) did not match the expected shape."""


@dataclass(frozen=True, slots=True)
class UnitPrices:
    """Price per single unit, in `CURRENCY`, keyed by `usage_record.unit_kind`."""

    input_tokens: float | None
    output_tokens: float | None
    cached_tokens: float | None

    def to_wire(self) -> dict[str, float | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> "UnitPrices":
        return cls(
            input_tokens=payload["input_tokens"],
            output_tokens=payload["output_tokens"],
            cached_tokens=payload["cached_tokens"],
        )


@dataclass(frozen=True, slots=True)
class PriceRecord:
    """One `price_record`: one provider model's cached unit prices plus their provenance."""

    provider_model_id: str
    currency: str | None
    unit_prices: UnitPrices
    provenance: SourceProvenance
    registration_id: str | None = None

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

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> "PriceRecord":
        return cls(
            provider_model_id=str(payload["provider_model_id"]),
            currency=payload["currency"],
            unit_prices=UnitPrices.from_wire(payload["unit_prices"]),
            provenance=SourceProvenance.from_wire(payload["provenance"]),
            registration_id=payload.get("registration_id"),
        )


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    """A full read of the price source at one point in time: every record plus its own
    provenance (all records from one `parse_prices`/`fetch_prices` call share the same
    provenance object), and the snapshot's own provenance for when the whole cache is
    empty."""

    records: tuple[PriceRecord, ...]
    provenance: SourceProvenance

    def to_wire(self) -> dict[str, Any]:
        return {
            "fetched_at": self.provenance.fetched_at,
            "provenance": self.provenance.to_wire(),
            "records": [record.to_wire() for record in self.records],
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> "PriceSnapshot":
        provenance = SourceProvenance.from_wire(payload["provenance"])
        records = tuple(PriceRecord.from_wire(row) for row in payload["records"])
        return cls(records=records, provenance=provenance)


def _unit_price(value: Any) -> float | None:
    """OpenRouter's published per-token price, kept per-token, or None when unpriced.

    -1 means "priced per request / dynamic" in OpenRouter's convention, which is unknown
    here rather than a literal negative price. The value is passed through unscaled: the
    contract prices one single unit, so a $3/M model records 0.000003 and a consumer that
    wants per-million multiplies at display time.
    """
    if isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price < 0:
        return None
    return round(price, UNIT_PRICE_DECIMALS)


def parse_prices(payload: Any, now: float) -> PriceSnapshot:
    """Pure parse of an OpenRouter `/models` document into a `PriceSnapshot`.

    Never touches the network. `now` (epoch seconds) becomes every record's
    `provenance.fetched_at`, since all records come from the same document.
    """
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise PriceFormatError("Unexpected pricing document: expected an object with a 'data' array")

    provenance = SourceProvenance(
        source_id=SOURCE_ID,
        source_url=PRICING_URL,
        fetched_at=format_fetched_at(now),
        stale=False,
    )

    records: list[PriceRecord] = []
    for row in rows[:MAX_MODELS]:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        prices = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
        unit_prices = UnitPrices(
            input_tokens=_unit_price(prices.get("prompt")),
            output_tokens=_unit_price(prices.get("completion")),
            cached_tokens=_unit_price(prices.get("input_cache_read")),
        )
        records.append(
            PriceRecord(
                provider_model_id=model_id,
                currency=CURRENCY,
                unit_prices=unit_prices,
                provenance=provenance,
            )
        )
    return PriceSnapshot(records=tuple(records), provenance=provenance)


def fetch_prices(fetcher: Callable[[str], bytes], now: float) -> PriceSnapshot:
    """Fetch and parse the OpenRouter price catalog through an injected transport.

    `fetcher` is the only thing in this module that may reach the network — tests pass a
    fake; real callers pass `transport.default_transport` or their own bound transport.
    A transport failure raises `PriceTransportError`; a malformed response raises
    `PriceFormatError`. Neither returns a partial snapshot.
    """
    try:
        raw = fetcher(PRICING_URL)
    except PriceSourceError:
        raise
    except Exception as error:
        raise PriceTransportError(f"Price source transport failed: {error}") from error
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise PriceFormatError(f"Price source returned invalid JSON: {error}") from error
    return parse_prices(payload, now)


def is_stale(snapshot: PriceSnapshot, now: float, max_age: float = DEFAULT_MAX_AGE_SECONDS) -> bool:
    """True once `now` is at least `max_age` seconds past the snapshot's `fetched_at`."""
    return is_stale_at(snapshot.provenance.fetched_at, now, max_age)


def serialize_price_snapshot(snapshot: PriceSnapshot) -> str:
    """Canonical JSON document for a snapshot, including top-level `fetched_at` and the
    full `provenance` object, for a cache file or the plugin_data store."""
    return canonical_json_bytes(snapshot.to_wire()).decode("utf-8")


def deserialize_price_snapshot(document_text: str) -> PriceSnapshot:
    """Inverse of `serialize_price_snapshot`. Never fetches; a malformed document raises
    `PriceFormatError` rather than returning a partial snapshot."""
    try:
        document = json.loads(document_text)
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise PriceFormatError(f"Price snapshot document is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise PriceFormatError("Price snapshot document must be a JSON object")
    try:
        return PriceSnapshot.from_wire(document)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise PriceFormatError(f"Price snapshot document is malformed: {error}") from error
