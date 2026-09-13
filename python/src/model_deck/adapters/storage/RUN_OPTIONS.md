# Durable run options

## Purpose and ownership

`SQLiteSessionRunRepository` persists the typed options admitted with a run so
an accepted, unclaimed run can be reconstructed after a process restart. The
adapter owns the SQLite representation and its compatibility behavior. The
engine owns the `RunOptions` types and the shared wire codec.

## Storage contract

New runs store `runs.options_json` as a canonical versioned envelope:

```json
{"options":{},"schema_version":1}
```

The `options` value is produced by `run_options_to_wire` and recovered with
`parse_run_options`. The adapter does not maintain another list of option
fields. Explicit values such as an empty instruction string, `false` parallel
tool calls, the `standard` service tier, a named tool, and a JSON Schema output
format therefore retain their exact meaning across restart.

An existing database gains the nullable `options_json` column through an
idempotent `ALTER TABLE` step. Tables are not recreated. A missing value on a
preexisting row is the only legacy representation and recovers as
`RunOptions()`. Adding the column does not rewrite the row, change its admission
request hash, or create another dispatch claim.

Present data must use the supported envelope and pass the shared codec. A
malformed value or unknown storage version raises
`StoredRunOptionsCompatibilityError` with the fixed message `stored run options
require explicit compatibility handling`. Recovery does not silently discard
present options.

## Restart behavior

Recovery decodes options only for accepted runs without a dispatch claim and
returns them as dispatchable `RunRequest` values. A previously claimed active
run is interrupted and is never reconstructed or resubmitted, so corrupt or
obsolete options on that row cannot cause a second provider dispatch.

## Verification

From `Architecture/python`:

```sh
PYTHONPATH=src /tmp/md-b18-venv/bin/python -m unittest tests.engine.test_run_options_persistence
```

The tests use temporary SQLite databases and cover exact typed round trips,
empty options, additive evolution of an old schema, claimed-run recovery, and
fail-closed handling of corrupt stored data.

## Limitations

This adapter does not define request hashing, provider option mapping, or live
database cutover. Those responsibilities remain with their owning systems.
