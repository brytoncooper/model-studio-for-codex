# HTTP POST transport adapter

## Purpose

`http_transport.py` owns one stdlib HTTP(S) connection per `post_stream` call
and returns an owned `HttpStreamResponse` handle. It extracts the low-level
POST behavior previously inline in `local_router.py` (`connection.request` /
`getresponse` with a 600s timeout) without parsing, fallback, request
translation, credential lookup, logging, or event policy.

## Contract

`post_stream(*, host, port, secure, path, payload: bytes, headers: Mapping,
timeout=600, connection_factory=...) -> HttpStreamResponse`

The caller supplies exact `payload` bytes and `headers`; they are forwarded
unchanged. `secure=True` selects `HTTPSConnection`, otherwise
`HTTPConnection`, unless a custom `connection_factory(host, port, secure,
timeout)` is injected. The handle exposes `status`, `read(amt?)`,
`read1(amt?)`, and `close()`, plus context-manager support. Its `repr`
carries only status and closed state, never headers or payload bytes.

## Invariants

One owned connection per call; no implicit retry. A connection created but
failing in `request` or `getresponse` is closed before the error propagates
(the legacy inline path leaked it). `close()` is idempotent on success or
error and tolerates connection-close failures. A lock guards close
ownership only and is never held across blocking reads or the underlying
close, so a concurrent `close` never blocks a read and duplicate closes
resolve to one underlying close. Reads may race a local close result.
Closing releases the local
connection only and does not confirm remote cancellation. Status and
streaming read semantics come straight from the underlying response.

## Extension

Keep this adapter free of SSE parsing, wire fallback, auth, logging, and
event policy. Add higher-level behavior in callers that already own those
concerns. The factory seam exists for tests and alternate transports, not
for embedding retry or routing here.

## Tests and limits

Focused suite: `tests.provider_openai_compatible.test_http_transport` (run
from `python/` with `PYTHONPATH=src`). Fake connections cover success,
non-200 status, read errors, creation/request/getresponse failures, close
idempotency (including two concurrent closes closing exactly once), exact
header/byte value forwarding without identity-copy assertions on immutable
bytes, and repr secrecy. No real network,
live app, settings, or build is exercised.
