"""Local cost estimation from a cached price record.

An estimate is never billed money. It is computed on read, labelled
cost_kind "estimated", and never written into a settled amount.
"""
from __future__ import annotations

from collections.abc import Mapping

from model_deck.engine.evidence.ports import PriceRecord


def estimate_cost(
    price_record: PriceRecord, usage_units: Mapping[str, float]
) -> float | None:
    """Cost of `usage_units` at `price_record`'s prices, or None when unknown.

    `usage_units` maps a usage_record unit_kind to the number of units observed.
    The answer is None — unknown, never zero — when the record states no
    currency, when the mapping is empty, or when any unit kind it names has no
    price. A partial sum is never returned as a cost.
    """
    if price_record.currency is None:
        return None
    if not usage_units:
        return None
    total = 0.0
    for unit_kind, units in usage_units.items():
        if units is None:
            return None
        unit_price = price_record.unit_prices.price_for(unit_kind)
        if unit_price is None:
            return None
        total += unit_price * units
    return total
