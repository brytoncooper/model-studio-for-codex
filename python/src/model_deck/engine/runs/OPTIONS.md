# Run options

## Purpose and ownership

Run options are application-owned preferences carried with a normalized run. The
canonical wire definition is
`contracts/engine.v1/vocabulary.schema.json#/definitions/run_options` and is
referenced by both `run_request` and `runs.start` parameters. The run subsystem
owns the typed values in `ports.py`; host and provider integrations translate
them at their own boundaries.

Host identity, thread and turn identity, working directory, account identity,
approvals, tool aliases, credentials, encrypted state, and continuation handles
remain outside run options.

## Contract

`RunOptions` is an immutable value with these optional fields:

- `instructions`: a string of at most 65,536 characters; an empty string is an
  explicit value.
- `reasoning_effort`: an advisory string from 1 through 64 characters. The
  vocabulary is intentionally open because supported values depend on the model.
- `service_tier`: `standard`, `priority`, or `economy`.
- `max_output_tokens`: an integer from 1 through 10,000,000.
- `parallel_tool_calls`: a Boolean.
- `output_format`: tagged `text`, `json_object`, or `json_schema`. A
  `json_schema` value includes a bounded name, an optional bounded description,
  a bounded JSON object schema, and optional strictness.
- `tool_choice`: tagged `auto`, `none`, `required`, or `named`. A named choice
  carries the authorized engine tool name before any provider aliasing.

Omitted options and an explicit empty object have the same normalized meaning.
Admission normalization and hashing implement that equivalence. Explicit
`false`, empty instructions, and `standard` remain distinct values and must be
preserved through persistence and dispatch.

## Invariants

- Null and unknown fields are invalid.
- Tagged objects reject fields that do not belong to their selected type.
- Options are transport preferences. Their presence does not claim that every
  provider enforces them.
- An adapter must reject an enforced option it cannot support before starting
  provider work. This includes strict structured output and required or named
  tool selection.
- Named tool selection resolves only against the run's authorized tool
  definitions.
- Provider-specific settings and secret material do not enter this contract.

## Extending the contract

Add a setting only when it belongs to normalized provider execution across host
boundaries. Update the shared canonical `run_options` definition and the typed
port value together. Preserve omission compatibility, define exact bounds, and
state whether the setting is advisory or requires pre-dispatch enforcement.
Provider translations, capability checks, persistence, and generated language
resources are separate owners and must be updated explicitly.

## Tests

Run the focused contract checks from `python/`:

```sh
PYTHONPATH=src python -m unittest tests.contracts.test_run_options_contract -v
```

The test loads schemas directly from the canonical repository `contracts/`
tree, so generated package copies may remain stale until the contract snapshot
owner synchronizes them.

## Current limitations

This contract slice defines the wire vocabulary and typed values only. Admission
parsing, canonical hashing, size accounting, storage and recovery, capability
checks, provider translation, generated contract copies, and live qualification
remain responsibilities for their respective owners.
