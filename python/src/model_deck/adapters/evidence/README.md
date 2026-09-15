# Evidence adapters (B16 / C8b)

Price and benchmark evidence, read only from OpenRouter's public `/models` and
`/benchmarks` endpoints (no API key ever sent) and shaped to match `price_record`,
`benchmark_record` and `source_provenance` (contracts/engine.v1/vocabulary.schema.json),
ported from the legacy root `pricing.py` and `model_benchmarks.py`.

## Modules

- `prices.py` — `parse_prices(payload, now)` (pure), `fetch_prices(fetcher, now)`,
  `is_stale(snapshot, now, max_age)`, `serialize_price_snapshot`/`deserialize_price_snapshot`.
  `unit_prices` is priced per **single** token, as the contract and `estimate_cost`
  require; per-million is a display convention applied by the MCP presenter.
- `benchmarks.py` — the same shape, one feed at a time: `parse_benchmarks(payload, feed, now)`,
  `fetch_benchmarks(fetcher, feed, now)`, `is_stale`, `serialize_benchmark_snapshot`/
  `deserialize_benchmark_snapshot`. `FEEDS` lists the five feeds (`catalog`,
  `artificial-analysis`, `design-arena/{models,builders,agents}`).
- `provenance.py` — the shared `SourceProvenance` wire type and staleness math.
- `transport.py` — the default bounded `urllib` transport (`Callable[[str], bytes]`), wired in explicitly by a caller, never used on its own.
- `source.py` — `HttpEvidenceSource`, the `EvidenceSourcePort` bridge C8c/C8d compose: one feed per refresh, record bodies handed over provenance-free.

## What this never does

- **No hidden refresh.** `parse_*`/`deserialize_*` never touch the network; only
  `fetch_*` does, and only through the caller's own transport callable — the C8a "reads
  never fetch" guarantee (`prices.query.result.cached` is always `true`).
- **No invented values.** A price OpenRouter doesn't publish, or a metric a feed never
  mentions, stays absent/`null`. `unit_prices` always carries all three unit keys even
  when every one is `null`; OpenRouter's `-1` ("dynamic/per-request") is `null` too,
  never a literal `-1` or a substituted `0`.
- **No collision to fake comparability.** `benchmark_record` has no `arena`/`category`
  field, so a Design Arena row's arena and category are folded into `feed` instead
  (`design-arena/models/coding`, `catalog/.../builders/webapp`) — otherwise two
  categories could collide under one `(source_id, feed, metric)` key. Adapter-level
  choice, not a contract requirement; flag to C8c if a different encoding is wanted.
- **No model-registry mapping.** `provider_model_id` is always `None` here; matching a
  feed's `source_model_ref` to a registered model needs the model registry, which this
  package does not have.

## How C8c consumes this

C8c defines the engine ports and composes these into `prices.query`/`prices.refresh` and
`benchmarks.query`/`benchmarks.refresh`: call `fetch_*` inside the refresh jobs
(`com.modeldeck.engine.prices.refresh` / `...benchmarks.refresh`) with a real transport;
persist `serialize_*` output and read it back with `deserialize_*` for the read paths,
never re-fetching; recompute `provenance.stale` at read time with
`is_stale(snapshot, now, max_age)` rather than trusting the value frozen at fetch time;
map `source_model_ref` to `provider_model_id` and merge the five feed snapshots into one
`benchmarks.query.result`.
