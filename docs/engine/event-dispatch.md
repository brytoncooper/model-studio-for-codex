# Run event dispatch

The engine [dispatch adapter](../../python/src/model_deck/engine/dispatch.py)
turns application run events into authenticated `engine.v1.event` notifications.
Application sequencing, terminal state and persistence remain owned by runs and
its repository; dispatch owns public event serialization and subscription delivery.

## Tool-request boundary

Internal `tool.requested` payloads contain `call_id`, `tool_name` and `arguments`
directly. The SQLite run repository consumes this shape to record the outstanding
call and enter `WAITING_FOR_TOOL`. The deterministic provider emits that same
internal shape. The frozen public event instead contains a nested `tool_call`.

`_flatten_application_run_event` validates the complete internal tool payload
against the frozen tool-call schema, then creates a detached nested `tool_call`
at the wire boundary. Missing, invalid, already-nested, mixed or extra fields
reject; no fields are silently removed. Internal stored events remain flat and
unchanged. The complete notification also passes the existing event schema gate.
Other event kinds retain their existing serialization.

## Extension and checks

Add any event serialization change at this boundary together with a schema-valid
notification fixture and an internal-state assertion. Provider adapters emit
application payloads, not public notification envelopes. Do not change storage
representation merely to match a client envelope.

From `python`, run `PYTHONPATH=src python -m unittest
tests.engine.test_engine_event_dispatch tests.engine.test_deterministic_provider`.
The authenticated socket fixture uses the real deterministic provider, SQLite
repository and run coordinator with an explicitly tool-capable fixture route.
It asserts `WAITING_FOR_TOOL` and a valid nested tool notification. Direct tests
check detached arguments and invalid/mixed shapes; existing content notification
tests remain represented. This is fixture integration evidence, not live provider
or installed-app qualification.
