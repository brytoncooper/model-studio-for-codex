# process runtime (B18)

Bounded real-subprocess adapter for one supervised plugin activation.
Spawns exactly one explicitly injected child argv (no shell) and drives
`plugin.v1` lifecycle JSON-RPC (hello / activate / drain) over its stdio
pipes using the accepted `lifecycle_session` + `stdio_codec` layers.

## API

- `ProcessRuntimeConfig(argv, package_dir, timeout_s=5.0, max_frames=16, max_stderr_bytes=65536)`: explicit launch + bounds. `argv` is used verbatim; `package_dir` must exist and becomes the child cwd.
- `ProcessRuntime(config).spawn()`: launch the owned child. No shell, no downloads, no global/home discovery.
- `run_hello(session, nonce) / run_activation(session) / run_drain(session, deadline_ms)`: one bounded exchange each. Payload build and result acceptance stay inside `LifecycleSession`; mismatched response ids are rejected (`id_mismatch`), codec failures surface as `transport`, over-limit frames as `frame_limit`, deadlines as `timeout`, and child exit/EOF without a matching response as `malformed_eof`.
- `close()`: terminate the owned child, bounded-wait, kill on expiry, reap, and drain stderr up to `max_stderr_bytes`. `stderr_bytes_drained` reports the retained count.
- `ProcessRuntimeError.code`: stable code, fixed detail; never carries the activation token or child stderr bytes.

## Rules

Owns only the injected child it spawned; launches nothing else. Tests use synthetic `sys.executable -c` fixture children only. No plugin install, dispatch, or broker work lives here. Shared `LifecycleSession` / `StdioCodec` interfaces are reused unchanged.

## Limits

One exchange at a time per runtime (reentrant calls rejected); one-use lifecycle (spawn once, no respawn after close). Stdin/stdout are nonblocking under a single monotonic deadline covering write + read, so a ~900KB write to a never-reading child times out instead of hanging. A daemon thread drains stderr continuously (retained tail capped at `max_stderr_bytes`); a 200KB pre-hello flood cannot deadlock the handshake. Response frames are strictly validated (`jsonrpc == "2.0"`, integer non-bool `id` exactly matching the request, exactly one of `result`/`error`, `result` an object); mismatches and malformed shapes fail closed. `StdioCodec` state persists across exchanges so trailing frames are never discarded. Any handshake/transport/`SessionError` failure auto-closes (terminates/reaps) the owned child. `close()` wait is capped at `min(timeout_s, 5s)` + 5s kill reap. Tests use an outer watchdog + `addCleanup(close)` harness; 11 focused tests cover handshake, EOF/id-mismatch cleanup, bundled-unsolicited-frame rejection, pre-exchange-exit cleanup, large-write timeout, stderr flood, strict shapes, identity fail-close, and config/one-use guards. Every decoded batch is fully validated before the match is accepted, so an unsolicited complete frame bundled with a valid response fails closed (`id_mismatch`) instead of being dropped; exchange preconditions run inside the guarded region so a pre-exchange child exit still terminates/reaps the owned child.
