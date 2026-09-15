# `examples/protocol-fixture/` — JSON-lines plugin reference fixture

An engine-independent, standard-library-only Node plugin that exercises the
frozen `contracts/plugin.v1/lifecycle/*` schemas. It exists so that contract
tests and downstream host implementations can drive a real, isolated plugin
process without pulling in the private engine package or building a
production plugin.

## What this fixture is

- **Engine-independent.** No import of `model_deck.*`, no imports of any
  third-party npm package. Only Node's built-in `crypto` and `util` modules.
- **Network-free.** Never opens a socket, never reads from the network, never
  DNS-resolves anything. The host's stdin/stdout are the only I/O channels.
- **Filesystem-free (relative to the plugin cwd).** Does not read or write
  any file outside the directory the host extracts it into. Does not touch
  `/tmp`, `/etc`, `$HOME`, or environment variables.
- **Stateless outside the process.** Every reload is a fresh state machine.
- **Deterministic.** All nonces, UUIDs, and timestamps used by handlers
  come from caller-supplied inputs or `crypto.randomUUID()` (no
  `Date.now()`-based randomness leaks into reply ordering).

## Wire discipline

- **Transport:** newline-delimited JSON on `stdin` and `stdout`. One JSON
  object per line. No batch frames.
- **Frame limit:** each frame must be at most 1,048,576 bytes (1 MiB). A
  larger frame closes the codec with an invalid-request error and the
  process exits non-zero.
- **JSON strictness:** decoded duplicate object keys, non-finite numbers,
  excessive depth/node counts, and lone UTF-16 surrogates in keys or values
  are rejected before dispatch. Repeated values and valid surrogate pairs are
  accepted.
- **Encoding:** UTF-8 only. Each original input buffer is decoded with a fatal
  standard-library decoder before JSON parsing, so malformed bytes never turn
  into replacement characters.
- **Errors:** malformed frames produce a single JSON-RPC error response
  with `id = null` and `code = -32600` (invalid request). The raw input is
  never echoed back in error messages.

## Lifecycle state machine

```
created
   |  plugin.v1.hello  (or legacy plugin.v1.lifecycle.hello)
   v
hello_verified
   |  plugin.v1.activate  (or legacy plugin.v1.lifecycle.activate)
   v
active
   |  plugin.v1.drain  (or legacy plugin.v1.lifecycle.drain)
   v
draining  --(automatic)--> inactive
   |  plugin.v1.deactivate
   v
deactivated   <-- terminal; refuses every subsequent method
```

After `deactivate` the plugin writes its terminal `deactivated: true`
reply, pauses `stdin`, and exits within 250 ms. Any frame already buffered
is dropped on the floor and produces no response.

## Canonical method names (primary)

| Method                                   | Direction        |
| ---------------------------------------- | ---------------- |
| `plugin.v1.hello`                        | inbound request  |
| `plugin.v1.activate`                     | inbound request  |
| `plugin.v1.heartbeat`                    | inbound request  |
| `plugin.v1.invoke`                       | inbound request  |
| `plugin.v1.cancel`                       | inbound request  |
| `plugin.v1.drain`                        | inbound request  |
| `plugin.v1.deactivate`                   | inbound request  |

These are the frozen names in `contracts/operations.inventory.json`. The
corresponding parameter and result shapes live in
`contracts/plugin.v1/lifecycle/`.

## Legacy aliases (accepted, explicitly documented)

| Legacy alias                           | Canonical form              |
| -------------------------------------- | --------------------------- |
| `plugin.v1.lifecycle.hello`            | `plugin.v1.hello`           |
| `plugin.v1.lifecycle.activate`         | `plugin.v1.activate`        |
| `plugin.v1.lifecycle.heartbeat`        | `plugin.v1.heartbeat`       |
| `plugin.v1.lifecycle.invoke`           | `plugin.v1.invoke`          |
| `plugin.v1.lifecycle.cancel`           | `plugin.v1.cancel`          |
| `plugin.v1.lifecycle.drain`            | `plugin.v1.drain`           |
| `plugin.v1.lifecycle.deactivate`       | `plugin.v1.deactivate`      |

The bundled runtime still emits some lifecycle-prefixed forms during its
migration. This fixture accepts those explicit aliases while treating the
inventory spellings as primary.

Canonical forms remain the only names that future host implementations need
to emit. The lifecycle-prefixed aliases remain for current runtime migration
compatibility.

## Operations contributed

| Operation id                                | Effect | Behaviour                                       |
| ------------------------------------------- | ------ | ----------------------------------------------- |
| `org.example.protocol-fixture.echo`         | read   | Synchronously returns `{echo: <input>}`          |
| `org.example.protocol-fixture.pending`      | read   | Returns a cancellable job id                      |

`echo` preserves the full input value verbatim. `false`, `0`, `""`, `null`,
nested arrays, and nested objects all round-trip exactly.

`pending` is the cancel-wiring proof. Its invoke reply immediately returns
`{output: {pending: true}, job_id: "..."}` so the host can issue a matching
`plugin.v1.cancel`. Cancel returns `{accepted: true}` and removes the pending
job without producing a late second invoke response. Drain and deactivate
clear any remaining fixture jobs before returning their terminal replies.

Both fixture operations use the bundled common `json_value` schema for their
operation-level input and output. Lifecycle invoke parameter/result schemas
describe the surrounding RPC envelope and are not contribution schemas.

## Why this fixture exists

- **Contract coverage.** The frozen `contracts/plugin.v1/lifecycle/*.schema.json`
  set needs something concrete to drive. The fixture's responses are
  produced by real code against those schemas, so schema drift is caught
  immediately.
- **Engine independence.** A real host test would have to stage the plugin
  through `ProcessRuntime`, allocate a rendezvous file, and negotiate a
  Unix socket. That couples the test to the engine's wire and timing.
  This fixture is the simplest possible driver that still speaks the same
  wire.
- **Reference implementation.** A host can compare its codec / dispatcher
  behaviour against this fixture's behaviour and see whether the JSON-RPC
  contract is being honoured end to end.

## Running the fixture manually

```sh
# Hello, then activate, then drain (interactive smoke test):
printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"plugin.v1.hello","params":{"offered_api":{"major":1,"minor":0},"nonce":"x"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"plugin.v1.activate","params":{"activation_token":"t","allowed_broker_methods":[]}}' \
  '{"jsonrpc":"2.0","id":3,"method":"plugin.v1.drain","params":{"deadline_ms":1000}}' \
  | node examples/protocol-fixture/plugin.js
```

The fixture takes no CLI arguments, no environment variables, and writes
nothing to disk.

## Caveats

- This is a fixture, not a production plugin. It deliberately does not
  enforce any authorisation, capability check, or capability gating. The
  bundled runtime remains the owner of validation and authorization.
- The `pending` operation's 60 s timer only bounds in-memory fixture state;
  the cancel path is the canonical cleanup. Don't rely on the timer.
- The fixture is single-threaded and serialises one request at a time.
  Real plugins may parallelise; the lifecycle contract does not change.
