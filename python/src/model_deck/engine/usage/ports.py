from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from model_deck.engine.evidence.ports import (
    PriceRecord,
    SourceProvenance,
    SubscriptionAllowance,
)

USAGE_QUERY_MAX_RECORDS = 1000

# The three kinds of money, from the frozen cost_kind vocabulary. They are never
# summed together and never share a field.
COST_KIND_ESTIMATED = "estimated"
COST_KIND_PROVIDER_SETTLED = "provider_settled"
COST_KIND_SUBSCRIPTION_ALLOWANCE = "subscription_allowance"
COST_KINDS: tuple[str, ...] = (
    COST_KIND_PROVIDER_SETTLED,
    COST_KIND_ESTIMATED,
    COST_KIND_SUBSCRIPTION_ALLOWANCE,
)

UNIT_KINDS: frozenset[str] = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "tool_calls",
        "requests",
        "other",
    }
)


_OPTIONAL_USAGE_FIELDS = (
    "registration_id", "connection_id", "provider_model_id",
    "settled_amount", "currency", "estimate_amount",
    "cost_kind", "provenance",
)
_REQUIRED_USAGE_FIELDS = ("run_id", "session_id", "observed_at", "units", "unit_kind")


@dataclass(frozen=True, slots=True)
class UsageRecord:
    run_id: str
    session_id: str
    observed_at: str
    units: float
    unit_kind: str
    registration_id: str | None = None
    connection_id: str | None = None
    provider_model_id: str | None = None
    settled_amount: float | None = None
    currency: str | None = None
    estimate_amount: float | None = None
    cost_kind: str | None = None
    provenance: SourceProvenance | None = None
    _present_optional_fields: frozenset[str] = field(default_factory=frozenset, repr=False)

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> UsageRecord:
        """Preserve both values and optional-field presence from validated input."""
        optional: dict[str, Any] = {
            name: payload.get(name) for name in _OPTIONAL_USAGE_FIELDS
        }
        if optional["provenance"] is not None:
            optional["provenance"] = SourceProvenance.from_wire(optional["provenance"])
        return cls(
            **{name: payload[name] for name in _REQUIRED_USAGE_FIELDS},
            **optional,
            _present_optional_fields=frozenset(
                name for name in _OPTIONAL_USAGE_FIELDS if name in payload
            ),
        )

    def to_wire(self) -> dict[str, Any]:
        payload = {name: getattr(self, name) for name in _REQUIRED_USAGE_FIELDS}
        for name in _OPTIONAL_USAGE_FIELDS:
            value = getattr(self, name)
            if name in self._present_optional_fields or value is not None:
                payload[name] = (
                    value.to_wire() if isinstance(value, SourceProvenance) else value
                )
        return payload


@dataclass(frozen=True, slots=True)
class RecordUsageResult:
    record: UsageRecord
    duplicate: bool


@dataclass(frozen=True, slots=True)
class QueryUsageResult:
    records: tuple[UsageRecord, ...]

    def to_wire(self) -> dict[str, Any]:
        return {"records": [record.to_wire() for record in self.records]}


@dataclass(frozen=True, slots=True)
class UsageTotal:
    """Totals for one cost_kind. Unknown stays null rather than collapsing to zero."""

    cost_kind: str
    amount: float | None
    currency: str | None
    units: float | None
    record_count: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "cost_kind": self.cost_kind,
            "amount": self.amount,
            "currency": self.currency,
            "units": self.units,
            "record_count": self.record_count,
        }


@dataclass(frozen=True, slots=True)
class SummarizeUsageResult:
    totals: tuple[UsageTotal, ...]

    def to_wire(self) -> dict[str, Any]:
        return {"totals": [total.to_wire() for total in self.totals]}


@runtime_checkable
class UsageRepository(Protocol):
    def store(self, record: UsageRecord, sequence: int) -> RecordUsageResult:
        ...

    def query(self, since: str | None, until: str | None) -> QueryUsageResult:
        ...


@runtime_checkable
class UsageQueryPort(Protocol):
    """A bounded chronological usage read, reconciled or direct."""

    def query(
        self, since: str | None = None, until: str | None = None
    ) -> QueryUsageResult:
        ...


@runtime_checkable
class PriceLookupPort(Protocol):
    """Cached price lookup for read-time estimates. Never fetches."""

    def price_for(
        self,
        *,
        provider_model_id: str | None,
        registration_id: str | None = None,
    ) -> PriceRecord | None:
        ...


@runtime_checkable
class SubscriptionAllowancePort(Protocol):
    """Provider-reported allowance a usage record draws on.

    Returns None for unknown, which is also the answer when no source is
    composed at all: the engine never infers allowance consumption.
    """

    def allowance_for(self, record: UsageRecord) -> SubscriptionAllowance | None:
        ...


class UsageConflictError(ValueError):
    pass


class UsageEventMismatchError(ValueError):
    pass


class UsageQueryValidationError(ValueError):
    pass


class UsageCostProjectionError(ValueError):
    """A read-time cost label would have contradicted the frozen cost rules."""


class UsageResourceExhaustedError(ValueError):
    pass


_TIMESTAMP = re.compile(
    r"(?P<second>[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]+))?"
    r"(?P<offset>[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def parse_observed_at(value: Any) -> tuple[int, str]:
    """Return exact UTC whole seconds (ordinal-day epoch) and fractional digits."""
    if not isinstance(value, str):
        raise ValueError("observed_at must be a string")
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise ValueError("observed_at must be an offset-carrying timestamp")
    whole_second = match["second"].upper() + match["offset"].upper()
    try:
        local = datetime.fromisoformat(whole_second)
    except ValueError:
        raise ValueError("observed_at has an invalid calendar date or time") from None
    offset = local.utcoffset()
    assert offset is not None
    # Integer ordinal arithmetic also handles an offset crossing UTC year
    # boundaries beyond datetime's representable local calendar endpoints.
    offset_seconds = offset.days * 86400 + offset.seconds
    utc_seconds = (
        local.toordinal() * 86400 + local.hour * 3600
        + local.minute * 60 + local.second - offset_seconds
    )
    fraction = (match["fraction"] or "").rstrip("0") or "0"
    return utc_seconds, fraction


def normalize_observed_at(value: Any) -> str:
    """Build an internal lexical UTC sort key, never a wire timestamp."""
    whole_seconds, fraction = parse_observed_at(value)
    # Whole seconds fit in 12 digits for every accepted four-digit local year.
    # Fractional trailing zero removal makes equal instants identical, while
    # ordinary string prefix ordering compares the exact decimal fractions.
    return f"{whole_seconds:012d}.{fraction}"
