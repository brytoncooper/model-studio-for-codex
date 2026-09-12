from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

USAGE_QUERY_MAX_RECORDS = 1000

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
    _present_optional_fields: frozenset[str] = field(default_factory=frozenset, repr=False)

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> UsageRecord:
        """Preserve both values and optional-field presence from validated input."""
        return cls(
            **{name: payload[name] for name in _REQUIRED_USAGE_FIELDS},
            **{name: payload.get(name) for name in _OPTIONAL_USAGE_FIELDS},
            _present_optional_fields=frozenset(
                name for name in _OPTIONAL_USAGE_FIELDS if name in payload
            ),
        )

    def to_wire(self) -> dict[str, Any]:
        payload = {name: getattr(self, name) for name in _REQUIRED_USAGE_FIELDS}
        for name in _OPTIONAL_USAGE_FIELDS:
            value = getattr(self, name)
            if name in self._present_optional_fields or value is not None:
                payload[name] = value
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


@runtime_checkable
class UsageRepository(Protocol):
    def store(self, record: UsageRecord, sequence: int) -> RecordUsageResult:
        ...

    def query(self, since: str | None, until: str | None) -> QueryUsageResult:
        ...


class UsageConflictError(ValueError):
    pass


class UsageEventMismatchError(ValueError):
    pass


class UsageQueryValidationError(ValueError):
    pass


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
