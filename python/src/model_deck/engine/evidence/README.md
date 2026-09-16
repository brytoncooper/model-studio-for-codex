# Evidence cache (B16)

`model_deck.engine.evidence` owns cached prices and benchmark scores: the ports
a source and a cache must satisfy, the read use cases, and the one use case that
refreshes. It holds no networking. Fetching lives in an adapter that satisfies
`EvidenceSourcePort`; durable storage lives in
`python/src/model_deck/adapters/storage/sqlite_evidence.py`.

## Reads are cache-only

`QueryPricesUseCase.query()` and `QueryBenchmarksUseCase.query()` read the
cached snapshot and nothing else. They never call the source, so `cached` is
`const true` in both result schemas and an empty or old answer is resolved by
running a refresh, not by retrying the read.

Every answer carries `source_provenance`, including for a kind that was never
fetched: the engine names itself as the source, reports the epoch as
`fetched_at`, and sets `last_refresh_error` to `never_refreshed`. `stale` is
computed on read from `fetched_at`, the configured `max_age_seconds` (default
one day) and the last failure — it is never persisted. A value is stale when its
age is strictly greater than the window, or when the newest refresh failed;
exactly at the window it is still fresh. `include_stale=False` omits stale
records but still returns the snapshot provenance, so the caller can see why
fewer records came back.

## Refresh semantics

`RefreshEvidenceUseCase.refresh(kind)` is the only path that reaches a source.
It fetches, validates every record against the frozen `price_record` /
`benchmark_record` schema, refuses a fetch larger than the cache bound
(`MAX_CACHED_PRICE_RECORDS` / `MAX_CACHED_BENCHMARK_RECORDS`, the fetching
adapter's own truncation point), then writes the new snapshot in one atomic
replace.

Caching and serving are bounded separately, and the numbers differ. A whole
catalog is worth caching even when it is too large for one answer, because a
lookup for one model reads it and estimates keep working. One answer is bounded
instead by what fits a transport frame — `MAX_PRICE_RECORDS` /
`MAX_BENCHMARK_RECORDS`, the `maxItems` the result schemas carry — and a query
matching more than that is refused rather than truncated; the caller narrows it
with `registration_id`, `provider_model_id` or `model_id`.

On failure nothing is replaced: the last good snapshot stays readable and
`record_refresh_failure(kind, attempted_at, error_code)` attaches the reason, so
the next query reports `stale` plus `last_refresh_error`. Error codes are fixed
and payload-free (`fetch_failed`, `invalid_payload`, `too_many_records`,
`never_refreshed`); a response body, URL or credential never reaches the cache.
Records are cached without provenance and are given the snapshot's provenance on
read, so a stale mark can never be baked into a stored row.

## The three cost kinds

`cost_kind` separates three things that are never summed together:

- `provider_settled` — money the provider reported as billed. Only a
  provider-reported event ever produces it, and only it may set
  `settled_amount`.
- `estimated` — computed locally by `estimate_cost(price_record, usage_units)`
  from a cached price, on read. `estimate_cost` returns `None` — unknown, never
  zero — when the price record states no currency or any needed unit price is
  null. An estimate is written to `estimate_amount` and never to
  `settled_amount`; `guard_estimate_never_settled` refuses the contradiction.
- `subscription_allowance` — consumption drawn against a prepaid or included
  allowance, supplied by a `SubscriptionAllowancePort`. With no source composed
  the answer is `None`, which means unknown, never zero.

A record the engine cannot place carries no kind, and `usage.summary` leaves it
to `usage.query` rather than folding it into a total.

## Reaching the wire

`feature.py` declares the four evidence operations as kernel feature
descriptors and binds them to the use cases here. Reads and refreshes are
separate features: a composed cache with no source still answers
`prices.query` and `benchmarks.query` — with an empty, self-described snapshot
— while the refresh operations simply do not appear in discovery.

These are composed features, not engine built-ins, so they travel the kernel's
generic invocation path, where a handler failure is redacted to one `internal`
error. A query that matches more records than one answer may carry is the
caller's to narrow, so the read handlers translate
`EvidenceResourceExhaustedError` into `KernelDomainError("resource_exhausted")`.
Only the code crosses that boundary — dispatch writes the sentence the caller
reads from its own table, so nothing these handlers hold can be published with
it. Every other failure stays redacted.

A refresh never fetches inline. `refresh_jobs.py` turns one into a first-party
job (see `engine/jobs/README.md`), returns its `job_id`, and runs
`RefreshEvidenceUseCase` on the engine's in-process runner. Repeating an
`idempotency_key` returns the same job while it is still active, so one key is
one fetch. A failed refresh fails the job and leaves the previous snapshot
readable; the reason stays a fixed `last_refresh_error` code on the snapshot,
never an upstream message. A refresh whose worker cannot even be started is
failed too, through `CreateFirstPartyJobUseCase.abandon`, so no key is ever held
by a job nothing will finish.

The source itself is an adapter: `adapters/evidence/source.py` bridges the
price and benchmark fetchers onto `EvidenceSourcePort`, and bootstrap injects
its byte transport, so nothing in this package imports a network module.
