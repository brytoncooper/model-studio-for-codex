from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

from model_deck.engine.usage.ports import (
    QueryUsageResult,
    RecordUsageResult,
    UsageConflictError,
    UsageEventMismatchError,
    UsageQueryValidationError,
    UsageRecord,
    UsageRepository,
    UsageResourceExhaustedError,
    normalize_observed_at,
    parse_observed_at,
)

USAGE_EVENT_REF = (
    "contracts/engine.v1/vocabulary.schema.json#/definitions/run_event_usage_observed"
)
USAGE_RECORD_REF = "contracts/engine.v1/vocabulary.schema.json#/definitions/usage_record"
USAGE_QUERY_PARAMS_REF = "contracts/engine.v1/methods/usage.query.params.schema.json"
USAGE_QUERY_RESULT_REF = "contracts/engine.v1/methods/usage.query.result.schema.json"



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
    def __init__(self, repository: UsageRepository) -> None:
        self._repository = repository

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
        result = self._repository.query(normalized_since, normalized_until)
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
