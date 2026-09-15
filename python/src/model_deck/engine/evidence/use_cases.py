"""Cache-only evidence reads and the explicit refresh that fills the cache."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

from model_deck.engine.evidence.ports import (
    BENCHMARKS_KIND,
    DEFAULT_EVIDENCE_MAX_AGE_SECONDS,
    ENGINE_EVIDENCE_SOURCE_IDS,
    MAX_BENCHMARK_RECORDS,
    MAX_CACHED_RECORDS_BY_KIND,
    MAX_PRICE_RECORDS,
    NEVER_FETCHED_AT,
    NEVER_REFRESHED_ERROR,
    PRICES_KIND,
    REFRESH_FETCH_FAILED,
    REFRESH_INVALID_PAYLOAD,
    REFRESH_TOO_MANY_RECORDS,
    BenchmarkRecord,
    EvidenceCacheRepository,
    EvidenceQueryValidationError,
    EvidenceResourceExhaustedError,
    EvidenceSnapshot,
    EvidenceSourcePort,
    EvidenceSourceUnavailableError,
    EvidenceTimestampError,
    FetchedEvidence,
    PriceRecord,
    SourceProvenance,
    compute_stale,
    instant_seconds,
    require_evidence_kind,
    utc_now_iso8601,
)

PRICE_RECORD_REF = (
    "contracts/engine.v1/vocabulary.schema.json#/definitions/price_record"
)
BENCHMARK_RECORD_REF = (
    "contracts/engine.v1/vocabulary.schema.json#/definitions/benchmark_record"
)
PRICES_QUERY_PARAMS_REF = "contracts/engine.v1/methods/prices.query.params.schema.json"
PRICES_QUERY_RESULT_REF = "contracts/engine.v1/methods/prices.query.result.schema.json"
BENCHMARKS_QUERY_PARAMS_REF = (
    "contracts/engine.v1/methods/benchmarks.query.params.schema.json"
)
BENCHMARKS_QUERY_RESULT_REF = (
    "contracts/engine.v1/methods/benchmarks.query.result.schema.json"
)

RECORD_REFS_BY_KIND: Mapping[str, str] = {
    PRICES_KIND: PRICE_RECORD_REF,
    BENCHMARKS_KIND: BENCHMARK_RECORD_REF,
}
FETCH_OPERATIONS_BY_KIND: Mapping[str, str] = {
    PRICES_KIND: "fetch_prices",
    BENCHMARKS_KIND: "fetch_benchmarks",
}


@dataclass(frozen=True, slots=True)
class QueryPricesResult:
    records: tuple[PriceRecord, ...]
    snapshot: SourceProvenance

    def to_wire(self) -> dict[str, Any]:
        return {
            "records": [record.to_wire() for record in self.records],
            "snapshot": self.snapshot.to_wire(),
            "cached": True,
        }


@dataclass(frozen=True, slots=True)
class QueryBenchmarksResult:
    benchmarks: tuple[BenchmarkRecord, ...]
    snapshot: SourceProvenance

    def to_wire(self) -> dict[str, Any]:
        return {
            "benchmarks": [record.to_wire() for record in self.benchmarks],
            "snapshot": self.snapshot.to_wire(),
            "cached": True,
        }


@dataclass(frozen=True, slots=True)
class RefreshEvidenceResult:
    kind: str
    refreshed: bool
    record_count: int
    error_code: str | None = None


def snapshot_provenance(
    kind: str,
    snapshot: EvidenceSnapshot | None,
    *,
    now: str,
    max_age_seconds: float,
) -> SourceProvenance:
    """Provenance of a cached kind, with staleness computed at read time.

    A kind that has never been fetched still reports provenance, so a caller can
    always tell how old the cache is and why the last refresh failed.
    """
    fallback_source_id = ENGINE_EVIDENCE_SOURCE_IDS[require_evidence_kind(kind)]
    if snapshot is None or snapshot.fetched_at is None:
        recorded_error = snapshot.last_refresh_error if snapshot is not None else None
        return SourceProvenance(
            source_id=(snapshot.source_id if snapshot is not None else None)
            or fallback_source_id,
            fetched_at=NEVER_FETCHED_AT,
            stale=True,
            last_refresh_error=recorded_error or NEVER_REFRESHED_ERROR,
        )
    return SourceProvenance(
        source_id=snapshot.source_id or fallback_source_id,
        fetched_at=snapshot.fetched_at,
        stale=compute_stale(
            fetched_at=snapshot.fetched_at,
            now=now,
            max_age_seconds=max_age_seconds,
            last_refresh_error=snapshot.last_refresh_error,
        ),
        source_url=snapshot.source_url,
        citation=snapshot.citation,
        as_of=snapshot.as_of,
        last_refresh_error=snapshot.last_refresh_error,
    )


def _require_max_age(max_age_seconds: float) -> float:
    if isinstance(max_age_seconds, bool) or not isinstance(
        max_age_seconds, (int, float)
    ):
        raise ValueError("evidence max age must be a number of seconds")
    if max_age_seconds < 0:
        raise ValueError("evidence max age must not be negative")
    return float(max_age_seconds)


class QueryPricesUseCase:
    """Serves cached prices. Never fetches: an empty answer is fixed by a refresh."""

    def __init__(
        self,
        repository: EvidenceCacheRepository,
        *,
        max_age_seconds: float = DEFAULT_EVIDENCE_MAX_AGE_SECONDS,
        clock: Callable[[], str] = utc_now_iso8601,
    ) -> None:
        self._repository = repository
        self._max_age_seconds = _require_max_age(max_age_seconds)
        self._clock = clock

    def query(
        self,
        *,
        registration_id: str | None = None,
        provider_model_id: str | None = None,
        include_stale: bool = True,
    ) -> QueryPricesResult:
        params: dict[str, Any] = {"include_stale": include_stale}
        if registration_id is not None:
            params["registration_id"] = registration_id
        if provider_model_id is not None:
            params["provider_model_id"] = provider_model_id
        try:
            validate_schema_ref(PRICES_QUERY_PARAMS_REF, params)
        except SchemaValidationError as exc:
            raise EvidenceQueryValidationError(str(exc)) from exc
        snapshot = self._repository.get_snapshot(PRICES_KIND)
        provenance = self._provenance(snapshot)
        records = _price_records(
            snapshot,
            provenance,
            registration_id=registration_id,
            provider_model_id=provider_model_id,
        )
        if not include_stale:
            records = tuple(
                record for record in records if not record.provenance.stale
            )
        if len(records) > MAX_PRICE_RECORDS:
            raise EvidenceResourceExhaustedError(
                f"price query matches more than {MAX_PRICE_RECORDS} records;"
                " narrow it with registration_id or provider_model_id"
            )
        result = QueryPricesResult(records=records, snapshot=provenance)
        validate_schema_ref(PRICES_QUERY_RESULT_REF, result.to_wire())
        return result

    def price_for(
        self,
        *,
        provider_model_id: str | None,
        registration_id: str | None = None,
    ) -> PriceRecord | None:
        """The cached price that applies to one usage record, or None.

        A registration-scoped price wins over a provider-wide one. A stale price
        is still returned: the estimate carries the stale provenance rather than
        disappearing.
        """
        if provider_model_id is None:
            return None
        snapshot = self._repository.get_snapshot(PRICES_KIND)
        candidates = _price_records(
            snapshot,
            self._provenance(snapshot),
            provider_model_id=provider_model_id,
        )
        if registration_id is not None:
            for record in candidates:
                if record.registration_id == registration_id:
                    return record
        for record in candidates:
            if record.registration_id is None:
                return record
        return None

    def _provenance(self, snapshot: EvidenceSnapshot | None) -> SourceProvenance:
        return snapshot_provenance(
            PRICES_KIND,
            snapshot,
            now=self._clock(),
            max_age_seconds=self._max_age_seconds,
        )


def _price_records(
    snapshot: EvidenceSnapshot | None,
    provenance: SourceProvenance,
    *,
    registration_id: str | None = None,
    provider_model_id: str | None = None,
) -> tuple[PriceRecord, ...]:
    if snapshot is None:
        return ()
    matched = []
    for body in snapshot.records:
        if registration_id is not None and body.get("registration_id") != registration_id:
            continue
        if (
            provider_model_id is not None
            and body.get("provider_model_id") != provider_model_id
        ):
            continue
        matched.append(PriceRecord.from_body(body, provenance))
    return tuple(matched)


class QueryBenchmarksUseCase:
    """Serves cached benchmark scores. Never fetches."""

    def __init__(
        self,
        repository: EvidenceCacheRepository,
        *,
        max_age_seconds: float = DEFAULT_EVIDENCE_MAX_AGE_SECONDS,
        clock: Callable[[], str] = utc_now_iso8601,
    ) -> None:
        self._repository = repository
        self._max_age_seconds = _require_max_age(max_age_seconds)
        self._clock = clock

    def query(
        self, *, model_id: str | None = None, include_stale: bool = True
    ) -> QueryBenchmarksResult:
        params: dict[str, Any] = {}
        if model_id is not None:
            params["model_id"] = model_id
        try:
            validate_schema_ref(BENCHMARKS_QUERY_PARAMS_REF, params)
        except SchemaValidationError as exc:
            raise EvidenceQueryValidationError(str(exc)) from exc
        snapshot = self._repository.get_snapshot(BENCHMARKS_KIND)
        provenance = snapshot_provenance(
            BENCHMARKS_KIND,
            snapshot,
            now=self._clock(),
            max_age_seconds=self._max_age_seconds,
        )
        records: tuple[BenchmarkRecord, ...] = ()
        if snapshot is not None:
            records = tuple(
                BenchmarkRecord.from_body(body, provenance)
                for body in snapshot.records
                if _matches_model(body, model_id)
            )
        if not include_stale:
            records = tuple(
                record for record in records if not record.provenance.stale
            )
        if len(records) > MAX_BENCHMARK_RECORDS:
            raise EvidenceResourceExhaustedError(
                f"benchmark query matches more than {MAX_BENCHMARK_RECORDS} records;"
                " narrow it with model_id"
            )
        result = QueryBenchmarksResult(benchmarks=records, snapshot=provenance)
        validate_schema_ref(BENCHMARKS_QUERY_RESULT_REF, result.to_wire())
        return result


def _matches_model(body: Mapping[str, Any], model_id: str | None) -> bool:
    """An unmapped row stays reachable by the identifier its own feed uses."""
    if model_id is None:
        return True
    return model_id in (body.get("provider_model_id"), body.get("source_model_ref"))


class RefreshEvidenceUseCase:
    """The only path that reaches a source. Failure keeps the last good snapshot."""

    def __init__(
        self,
        *,
        source: EvidenceSourcePort,
        repository: EvidenceCacheRepository,
        clock: Callable[[], str] = utc_now_iso8601,
    ) -> None:
        self._source = source
        self._repository = repository
        self._clock = clock

    def refresh(self, kind: str) -> RefreshEvidenceResult:
        require_evidence_kind(kind)
        fetch = getattr(self._source, FETCH_OPERATIONS_BY_KIND[kind], None)
        if not callable(fetch):
            raise EvidenceSourceUnavailableError(
                f"no evidence source is composed for {kind}"
            )
        attempted_at = self._clock()
        try:
            fetched = fetch()
        except Exception:
            # The reason never reaches the cache: last_refresh_error is a fixed
            # code, so no response body, URL or credential can leak into a read.
            return self._record_failure(kind, attempted_at, REFRESH_FETCH_FAILED)
        try:
            self._validate(kind, fetched)
        except EvidenceResourceExhaustedError:
            return self._record_failure(kind, attempted_at, REFRESH_TOO_MANY_RECORDS)
        except (
            SchemaValidationError,
            EvidenceTimestampError,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            return self._record_failure(kind, attempted_at, REFRESH_INVALID_PAYLOAD)
        snapshot = EvidenceSnapshot.from_fetch(kind, fetched)
        self._repository.put_snapshot(kind, snapshot)
        return RefreshEvidenceResult(
            kind=kind, refreshed=True, record_count=len(snapshot.records)
        )

    def _record_failure(
        self, kind: str, attempted_at: str, error_code: str
    ) -> RefreshEvidenceResult:
        self._repository.record_refresh_failure(kind, attempted_at, error_code)
        return RefreshEvidenceResult(
            kind=kind, refreshed=False, record_count=0, error_code=error_code
        )

    def _validate(self, kind: str, fetched: FetchedEvidence) -> None:
        instant_seconds(fetched.fetched_at)
        if not isinstance(fetched.source_id, str):
            raise TypeError("fetched source_id must be a string")
        records = tuple(fetched.records)
        # The cache bound, not the answer bound: a catalog too large to serve in
        # one answer is still worth caching, because a lookup for one model
        # reads it and an oversized answer is narrowed by the caller.
        if len(records) > MAX_CACHED_RECORDS_BY_KIND[kind]:
            raise EvidenceResourceExhaustedError(
                f"{kind} refresh exceeds {MAX_CACHED_RECORDS_BY_KIND[kind]} cached records"
            )
        provenance = SourceProvenance(
            source_id=fetched.source_id,
            fetched_at=fetched.fetched_at,
            stale=False,
            source_url=fetched.source_url,
            citation=fetched.citation,
            as_of=fetched.as_of,
        ).to_wire()
        record_ref = RECORD_REFS_BY_KIND[kind]
        for body in records:
            if "provenance" in body:
                raise ValueError(
                    "fetched record bodies must not carry provenance;"
                    " provenance is attached from the snapshot on read"
                )
            validate_schema_ref(record_ref, {**dict(body), "provenance": provenance})
