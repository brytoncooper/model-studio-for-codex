from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

from model_deck.engine.evidence.pricing import estimate_cost
from model_deck.engine.usage.ports import (
    COST_KIND_ESTIMATED,
    COST_KIND_PROVIDER_SETTLED,
    COST_KIND_SUBSCRIPTION_ALLOWANCE,
    COST_KINDS,
    PriceLookupPort,
    QueryUsageResult,
    RecordUsageResult,
    SubscriptionAllowancePort,
    SummarizeUsageResult,
    UsageConflictError,
    UsageCostProjectionError,
    UsageEventMismatchError,
    UsageQueryPort,
    UsageQueryValidationError,
    UsageRecord,
    UsageRepository,
    UsageResourceExhaustedError,
    UsageTotal,
    normalize_observed_at,
    parse_observed_at,
)

USAGE_EVENT_REF = (
    "contracts/engine.v1/vocabulary.schema.json#/definitions/run_event_usage_observed"
)
USAGE_RECORD_REF = "contracts/engine.v1/vocabulary.schema.json#/definitions/usage_record"
USAGE_QUERY_PARAMS_REF = "contracts/engine.v1/methods/usage.query.params.schema.json"
USAGE_QUERY_RESULT_REF = "contracts/engine.v1/methods/usage.query.result.schema.json"
USAGE_SUMMARY_PARAMS_REF = "contracts/engine.v1/methods/usage.summary.params.schema.json"
USAGE_SUMMARY_RESULT_REF = "contracts/engine.v1/methods/usage.summary.result.schema.json"


def guard_estimate_never_settled(record: UsageRecord) -> UsageRecord:
    """An estimate must never appear in settled money."""
    if record.cost_kind == COST_KIND_ESTIMATED and record.settled_amount is not None:
        raise UsageCostProjectionError(
            "an estimated record must not carry a settled amount"
        )
    return record


def project_cost_kind(
    record: UsageRecord,
    *,
    prices: PriceLookupPort | None = None,
    allowances: SubscriptionAllowancePort | None = None,
) -> UsageRecord:
    """Label one stored record with its cost kind, on read, in this order.

    1. A kind the producer already recorded is kept verbatim.
    2. A numeric settled_amount is provider-reported money: provider_settled.
    3. An allowance source that claims the record: subscription_allowance.
    4. A cached price that can price the record's units: estimated. The figure
       and its provenance are the engine's own only when the engine computed
       them; an observed estimate_amount is preserved untouched.
    5. Otherwise no kind, because the engine did not record one.

    Nothing here ever writes into settled_amount.
    """
    if record.cost_kind is not None:
        return guard_estimate_never_settled(record)
    if record.settled_amount is not None:
        return replace(record, cost_kind=COST_KIND_PROVIDER_SETTLED)
    if allowances is not None:
        allowance = allowances.allowance_for(record)
        if allowance is not None:
            return replace(
                record,
                cost_kind=COST_KIND_SUBSCRIPTION_ALLOWANCE,
                provenance=record.provenance or allowance.provenance,
            )
    if prices is None:
        return record
    price = prices.price_for(
        provider_model_id=record.provider_model_id,
        registration_id=record.registration_id,
    )
    if price is None:
        return record
    if record.estimate_amount is not None:
        # Observed figures are never recomputed or reattributed.
        return guard_estimate_never_settled(
            replace(record, cost_kind=COST_KIND_ESTIMATED)
        )
    if record.currency is not None and record.currency != price.currency:
        return record
    amount = estimate_cost(price, {record.unit_kind: record.units})
    if amount is None:
        return record
    return guard_estimate_never_settled(
        replace(
            record,
            estimate_amount=amount,
            currency=price.currency,
            cost_kind=COST_KIND_ESTIMATED,
            provenance=price.provenance,
        )
    )


def _monetary_amount(record: UsageRecord) -> float | None:
    """The one amount a record's own kind accounts for, or None for unknown."""
    if record.cost_kind == COST_KIND_PROVIDER_SETTLED:
        return record.settled_amount
    if record.cost_kind == COST_KIND_ESTIMATED:
        return record.estimate_amount
    # Allowance consumption is not money on the record; it is reported unknown.
    return None



class RecordUsageUseCase:
    def __init__(self, repository: UsageRepository) -> None:
        self._repository = repository

    def record(self, event: Mapping[str, Any]) -> RecordUsageResult:
        if not isinstance(event, Mapping):
            raise UsageEventMismatchError("event must be a mapping")
        if event.get("kind") != "usage.observed":
            raise UsageEventMismatchError(
                f"unsupported event kind: {event.get('kind')!r}"
            )
        try:
            validate_schema_ref(USAGE_EVENT_REF, dict(event))
        except SchemaValidationError as exc:
            raise UsageEventMismatchError(str(exc)) from exc
        usage = event["usage"]
        if (
            usage["run_id"] != event["run_id"]
            or usage["session_id"] != event["session_id"]
        ):
            raise UsageEventMismatchError(
                "usage run_id/session_id must match the event envelope"
            )
        try:
            parse_observed_at(usage["observed_at"])
        except ValueError as exc:
            raise UsageEventMismatchError(str(exc)) from exc
        record = UsageRecord.from_wire(usage)
        return self._repository.store(record, event["sequence"])


class QueryUsageUseCase:
    def __init__(
        self,
        repository: UsageRepository,
        *,
        prices: PriceLookupPort | None = None,
        subscription_allowance: SubscriptionAllowancePort | None = None,
    ) -> None:
        self._repository = repository
        self._prices = prices
        self._subscription_allowance = subscription_allowance

    def query(
        self, since: str | None = None, until: str | None = None
    ) -> QueryUsageResult:
        params: dict[str, Any] = {}
        if since is not None:
            params["since"] = since
        if until is not None:
            params["until"] = until
        try:
            validate_schema_ref(USAGE_QUERY_PARAMS_REF, params)
        except SchemaValidationError as exc:
            raise UsageQueryValidationError(str(exc)) from exc
        normalized_since = self._normalize_bound(since, "since")
        normalized_until = self._normalize_bound(until, "until")
        if (
            normalized_since is not None
            and normalized_until is not None
            and normalized_since > normalized_until
        ):
            raise UsageQueryValidationError("since must not be after until")
        stored = self._repository.query(normalized_since, normalized_until)
        result = QueryUsageResult(
            records=tuple(
                project_cost_kind(
                    record,
                    prices=self._prices,
                    allowances=self._subscription_allowance,
                )
                for record in stored.records
            )
        )
        validate_schema_ref(USAGE_QUERY_RESULT_REF, result.to_wire())
        return result

    @staticmethod
    def _normalize_bound(value: str | None, name: str) -> str | None:
        if value is None:
            return None
        try:
            return normalize_observed_at(value)
        except ValueError as exc:
            raise UsageQueryValidationError(f"{name}: {exc}") from exc


class SummarizeUsageUseCase:
    """Totals for one window, grouped by cost kind and never merged across kinds."""

    def __init__(self, query_usage: UsageQueryPort) -> None:
        self._query_usage = query_usage

    def summarize(
        self, since: str | None = None, until: str | None = None
    ) -> SummarizeUsageResult:
        params: dict[str, Any] = {}
        if since is not None:
            params["since"] = since
        if until is not None:
            params["until"] = until
        try:
            validate_schema_ref(USAGE_SUMMARY_PARAMS_REF, params)
        except SchemaValidationError as exc:
            raise UsageQueryValidationError(str(exc)) from exc
        page = self._query_usage.query(since, until)
        grouped: dict[str, list[UsageRecord]] = {}
        for record in page.records:
            # A record with no recorded kind is read through usage.query, not
            # folded into a total whose kind nobody established.
            if record.cost_kind is None:
                continue
            if record.cost_kind not in COST_KINDS:
                raise UsageCostProjectionError(
                    f"unsupported cost kind: {record.cost_kind!r}"
                )
            grouped.setdefault(record.cost_kind, []).append(record)
        result = SummarizeUsageResult(
            totals=tuple(
                _total_for(cost_kind, grouped[cost_kind])
                for cost_kind in COST_KINDS
                if cost_kind in grouped
            )
        )
        validate_schema_ref(USAGE_SUMMARY_RESULT_REF, result.to_wire())
        return result


def _total_for(cost_kind: str, records: list[UsageRecord]) -> UsageTotal:
    """A partial sum is never reported as a total: unknown makes the amount null."""
    currencies = {record.currency for record in records}
    amounts = [_monetary_amount(record) for record in records]
    amount: float | None = None
    currency: str | None = None
    only_currency = next(iter(currencies)) if len(currencies) == 1 else None
    if only_currency is not None and None not in amounts:
        amount = sum(amounts)
        currency = only_currency
    return UsageTotal(
        cost_kind=cost_kind,
        amount=amount,
        currency=currency,
        units=sum(record.units for record in records),
        record_count=len(records),
    )
