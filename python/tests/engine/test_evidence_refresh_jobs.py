"""The evidence operations over the real socket, end to end.

A refresh is the only thing that reaches a source, and it reaches it as an
engine-owned job: the operation returns a job id, ``jobs.get`` observes it to a
terminal state, and only then does a read see new records. Every test here
injects its own transport, so nothing in this module can touch the network.
"""
from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.transport.rendezvous import load_rendezvous_file
from model_deck.adapters.transport.unix_client import UnixSocketEngineClient
from model_deck.bootstrap import build_engine_server
from model_deck.engine.jobs.first_party import (
    JOB_KIND_BENCHMARKS_REFRESH,
    JOB_KIND_PRICES_REFRESH,
)
from model_deck_contracts.paths import repo_root
from model_deck_contracts.validator import validate_schema_ref

CLIENT_NAME = "fixture-evidence-client"
TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}
UNKNOWN_JOB_ID = "00000000-0000-4000-8000-000000000000"

PRICES_DOCUMENT = {
    "data": [
        {
            "id": "openai/gpt-5",
            "pricing": {
                "prompt": "0.000003",
                "completion": "0.000015",
                "input_cache_read": "0.00000075",
            },
        }
    ]
}

BENCHMARKS_DOCUMENT = {
    "data": [
        {
            "model_permaslug": "openai/gpt-5",
            "source": "artificial-analysis",
            "intelligence_index": 71.0,
        }
    ],
    "meta": {
        "as_of": "2026-09-10",
        "source_url": "https://artificialanalysis.ai/models/gpt-5",
    },
}


class ScriptedTransport:
    """A byte transport under the test's control; it never opens a socket."""

    def __init__(self) -> None:
        self.fail = False
        self.gate: threading.Event | None = None
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError("sensitive upstream transport detail")
        document = BENCHMARKS_DOCUMENT if "benchmark" in url else PRICES_DOCUMENT
        return json.dumps(document).encode("utf-8")


class EvidenceRefreshJobTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.transport = ScriptedTransport()
        self._next_id = 100

    def build(self, **overrides):
        runtime = build_engine_server(
            state_root=self.root / "state",
            artifact_root=self.root / "artifact",
            socket_root=self.root / "socket",
            source_root=repo_root(),
            legacy_agents_dir=self.root / "legacy",
            default_connection_id="550e8400-e29b-41d4-a716-446655440002",
            enable_application_state=True,
            evidence_transport=self.transport,
            **overrides,
        )
        runtime.server.start()
        self.addCleanup(runtime.server.stop)
        return runtime

    def session(self, runtime):
        descriptor = load_rendezvous_file(runtime.rendezvous_path)
        credential = runtime.enrollment.credential_path.read_text().strip()
        connection = UnixSocketEngineClient(descriptor.socket_path).session()
        return connection, descriptor, credential

    def authenticate(self, session, descriptor, credential) -> None:
        response = self.call(
            session,
            "engine.v1.hello",
            {
                "client_name": CLIENT_NAME,
                "offered_api": {"major": 1, "minor": 0},
                "authentication": {
                    "engine_instance_id": descriptor.engine_instance_id,
                    "instance_nonce": descriptor.instance_nonce,
                    "credential": credential,
                },
            },
        )
        self.assertTrue(response["result"]["authenticated"])

    def call(self, session, method, params=None):
        self._next_id += 1
        return session.call(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": method,
                "params": {} if params is None else params,
            }
        )

    def result(self, session, method, params=None):
        response = self.call(session, method, params)
        self.assertIn("result", response, response)
        return response["result"]

    def await_terminal(self, session, job_id, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.result(session, "engine.v1.jobs.get", {"job_id": job_id})
            validate_schema_ref(
                "contracts/engine.v1/methods/jobs.get.result.schema.json", state
            )
            if state["state"] in TERMINAL_STATES:
                return state
            time.sleep(0.01)
        self.fail(f"job {job_id} never reached a terminal state")

    # --- prices ------------------------------------------------------------

    def test_prices_are_empty_and_cached_until_a_refresh_job_finishes(self) -> None:
        runtime = self.build()
        connection, descriptor, credential = self.session(runtime)
        with connection as session:
            self.authenticate(session, descriptor, credential)

            empty = self.result(session, "engine.v1.prices.query")
            validate_schema_ref(
                "contracts/engine.v1/methods/prices.query.result.schema.json", empty
            )
            self.assertEqual(empty["records"], [])
            # A read is always a cache read, and it says why it is empty rather
            # than fetching to fill itself in.
            self.assertTrue(empty["cached"])
            self.assertTrue(empty["snapshot"]["stale"])
            self.assertEqual(empty["snapshot"]["last_refresh_error"], "never_refreshed")
            self.assertEqual(self.transport.calls, [])

            started = self.result(
                session, "engine.v1.prices.refresh", {"idempotency_key": "refresh-1"}
            )
            validate_schema_ref(
                "contracts/engine.v1/methods/prices.refresh.result.schema.json", started
            )
            self.assertEqual(started["job_kind"], JOB_KIND_PRICES_REFRESH)
            self.assertTrue(started["explicit_network"])

            terminal = self.await_terminal(session, started["job_id"])
            self.assertEqual(terminal["state"], "completed")
            self.assertEqual(terminal["output"], {"kind": "prices", "record_count": 1})

            priced = self.result(session, "engine.v1.prices.query")
            validate_schema_ref(
                "contracts/engine.v1/methods/prices.query.result.schema.json", priced
            )
            self.assertEqual(len(priced["records"]), 1)
            record = priced["records"][0]
            self.assertEqual(record["provider_model_id"], "openai/gpt-5")
            # A unit_price prices one single token, not a million of them, so
            # the source's "0.000003" is carried through unscaled and a
            # consumer that wants a per-million figure multiplies on display.
            self.assertEqual(record["unit_prices"]["input_tokens"], 3e-06)
            self.assertEqual(record["unit_prices"]["output_tokens"], 1.5e-05)
            self.assertEqual(record["unit_prices"]["cached_tokens"], 7.5e-07)
            self.assertEqual(record["provenance"]["source_id"], "ai.openrouter")
            self.assertFalse(record["provenance"]["stale"])
            self.assertFalse(priced["snapshot"]["stale"])
            self.assertEqual(len(self.transport.calls), 1)

    def test_a_failing_refresh_keeps_the_last_good_prices_and_names_the_failure(self) -> None:
        runtime = self.build()
        connection, descriptor, credential = self.session(runtime)
        with connection as session:
            self.authenticate(session, descriptor, credential)
            good = self.result(
                session, "engine.v1.prices.refresh", {"idempotency_key": "good"}
            )
            self.await_terminal(session, good["job_id"])

            self.transport.fail = True
            bad = self.result(
                session, "engine.v1.prices.refresh", {"idempotency_key": "bad"}
            )
            failed = self.await_terminal(session, bad["job_id"])
            self.assertEqual(failed["state"], "failed")

            after = self.result(session, "engine.v1.prices.query")
            validate_schema_ref(
                "contracts/engine.v1/methods/prices.query.result.schema.json", after
            )
            # The previous snapshot is still readable; the failure is reported
            # beside it as a fixed code, never as an upstream message.
            self.assertEqual(len(after["records"]), 1)
            self.assertTrue(after["snapshot"]["stale"])
            self.assertEqual(after["snapshot"]["last_refresh_error"], "fetch_failed")
            self.assertNotIn("sensitive upstream transport detail", json.dumps(after))

            # A caller that would rather see nothing than something stale can
            # ask for that, and gets fewer records rather than an old price.
            fresh_only = self.result(
                session, "engine.v1.prices.query", {"include_stale": False}
            )
            self.assertEqual(fresh_only["records"], [])
            self.assertTrue(fresh_only["snapshot"]["stale"])

    def test_repeating_an_idempotency_key_returns_the_same_running_job(self) -> None:
        runtime = self.build()
        connection, descriptor, credential = self.session(runtime)
        gate = threading.Event()
        self.transport.gate = gate
        self.addCleanup(gate.set)
        with connection as session:
            self.authenticate(session, descriptor, credential)
            first = self.result(
                session, "engine.v1.prices.refresh", {"idempotency_key": "one-fetch"}
            )
            running = self.result(
                session, "engine.v1.jobs.get", {"job_id": first["job_id"]}
            )
            self.assertEqual(running["state"], "running")

            second = self.result(
                session, "engine.v1.prices.refresh", {"idempotency_key": "one-fetch"}
            )
            self.assertEqual(second["job_id"], first["job_id"])

            gate.set()
            self.await_terminal(session, first["job_id"])
            # One key, one fetch: the repeat never reached the source.
            self.assertEqual(len(self.transport.calls), 1)

            # Once the job is terminal the key is free, and a retry is real
            # work rather than a pointer at a job that already finished.
            third = self.result(
                session, "engine.v1.prices.refresh", {"idempotency_key": "one-fetch"}
            )
            self.assertNotEqual(third["job_id"], first["job_id"])
            self.await_terminal(session, third["job_id"])
            self.assertEqual(len(self.transport.calls), 2)

    # --- benchmarks --------------------------------------------------------

    def test_benchmarks_follow_the_same_refresh_then_read_path(self) -> None:
        runtime = self.build()
        connection, descriptor, credential = self.session(runtime)
        with connection as session:
            self.authenticate(session, descriptor, credential)

            empty = self.result(session, "engine.v1.benchmarks.query")
            validate_schema_ref(
                "contracts/engine.v1/methods/benchmarks.query.result.schema.json", empty
            )
            self.assertEqual(empty["benchmarks"], [])
            self.assertTrue(empty["cached"])
            self.assertTrue(empty["snapshot"]["stale"])

            started = self.result(
                session, "engine.v1.benchmarks.refresh", {"idempotency_key": "b-1"}
            )
            validate_schema_ref(
                "contracts/engine.v1/methods/benchmarks.refresh.result.schema.json",
                started,
            )
            self.assertEqual(started["job_kind"], JOB_KIND_BENCHMARKS_REFRESH)
            terminal = self.await_terminal(session, started["job_id"])
            self.assertEqual(terminal["state"], "completed")

            scored = self.result(session, "engine.v1.benchmarks.query")
            validate_schema_ref(
                "contracts/engine.v1/methods/benchmarks.query.result.schema.json", scored
            )
            self.assertEqual(len(scored["benchmarks"]), 1)
            row = scored["benchmarks"][0]
            self.assertEqual(row["source_model_ref"], "openai/gpt-5")
            # The feed maps nothing to a registered model, and that row stays
            # visible under the identifier its own feed uses.
            self.assertIsNone(row["provider_model_id"])
            self.assertEqual(row["metric"], "intelligence_index")
            self.assertFalse(row["provenance"]["stale"])

            by_feed_identifier = self.result(
                session, "engine.v1.benchmarks.query", {"model_id": "openai/gpt-5"}
            )
            self.assertEqual(len(by_feed_identifier["benchmarks"]), 1)
            unmatched = self.result(
                session, "engine.v1.benchmarks.query", {"model_id": "vendor/absent"}
            )
            self.assertEqual(unmatched["benchmarks"], [])

    def test_a_failing_benchmark_refresh_keeps_the_last_good_snapshot(self) -> None:
        runtime = self.build()
        connection, descriptor, credential = self.session(runtime)
        with connection as session:
            self.authenticate(session, descriptor, credential)
            good = self.result(
                session, "engine.v1.benchmarks.refresh", {"idempotency_key": "b-good"}
            )
            self.await_terminal(session, good["job_id"])

            self.transport.fail = True
            bad = self.result(
                session, "engine.v1.benchmarks.refresh", {"idempotency_key": "b-bad"}
            )
            self.assertEqual(self.await_terminal(session, bad["job_id"])["state"], "failed")

            after = self.result(session, "engine.v1.benchmarks.query")
            self.assertEqual(len(after["benchmarks"]), 1)
            self.assertTrue(after["snapshot"]["stale"])
            self.assertEqual(after["snapshot"]["last_refresh_error"], "fetch_failed")
            self.assertNotIn("sensitive upstream transport detail", json.dumps(after))

    # --- availability and authorization ------------------------------------

    def test_evidence_and_jobs_are_discoverable_without_runs_or_extensions(self) -> None:
        runtime = self.build()
        connection, descriptor, credential = self.session(runtime)
        with connection as session:
            self.authenticate(session, descriptor, credential)
            operations = self.result(session, "engine.v1.operations.list")
            rows = {row["operation_id"]: row for row in operations["operations"]}
            for operation_id, effect in (
                ("engine.v1.prices.query", "read"),
                ("engine.v1.benchmarks.query", "read"),
                ("engine.v1.prices.refresh", "write"),
                ("engine.v1.benchmarks.refresh", "write"),
                ("engine.v1.jobs.get", "read"),
                ("engine.v1.jobs.cancel", "write"),
            ):
                self.assertIn(operation_id, rows)
                self.assertEqual(rows[operation_id]["effect"], effect)
            # No run has happened, so usage stays absent; evidence does not
            # borrow its availability.
            self.assertNotIn("engine.v1.usage.query", rows)
            self.assertNotIn("engine.v1.usage.summary", rows)

    def test_evidence_operations_require_authentication(self) -> None:
        runtime = self.build()
        connection, descriptor, credential = self.session(runtime)
        with connection as session:
            for method, params in (
                ("engine.v1.prices.query", {}),
                ("engine.v1.benchmarks.query", {}),
                ("engine.v1.prices.refresh", {"idempotency_key": "k"}),
                ("engine.v1.jobs.get", {"job_id": UNKNOWN_JOB_ID}),
            ):
                with self.subTest(method=method):
                    response = self.call(session, method, params)
                    self.assertEqual(
                        response["error"]["data"]["code"], "capability_denied"
                    )
            self.assertEqual(self.transport.calls, [])

    def test_a_refresh_job_can_be_cancelled_and_an_unknown_one_is_not_found(self) -> None:
        runtime = self.build()
        connection, descriptor, credential = self.session(runtime)
        gate = threading.Event()
        self.transport.gate = gate
        self.addCleanup(gate.set)
        with connection as session:
            self.authenticate(session, descriptor, credential)
            started = self.result(
                session, "engine.v1.prices.refresh", {"idempotency_key": "cancel-me"}
            )
            accepted = self.result(
                session,
                "engine.v1.jobs.cancel",
                {"job_id": started["job_id"], "idempotency_key": "cancel-key"},
            )
            self.assertEqual(accepted, {"accepted": True})
            gate.set()
            self.await_terminal(session, started["job_id"])

            missing = self.call(
                session, "engine.v1.jobs.get", {"job_id": UNKNOWN_JOB_ID}
            )
            self.assertEqual(missing["error"]["data"]["code"], "not_found")

    def test_a_restart_interrupts_a_refresh_job_and_never_replays_it(self) -> None:
        gate = threading.Event()
        self.transport.gate = gate
        self.addCleanup(gate.set)
        first = self.build()
        connection, descriptor, credential = self.session(first)
        with connection as session:
            self.authenticate(session, descriptor, credential)
            started = self.result(
                session, "engine.v1.prices.refresh", {"idempotency_key": "interrupted"}
            )
            job_id = started["job_id"]
        first.server.stop()

        # The gate stays shut across the restart, so the previous process's
        # worker is still parked when recovery runs and cannot race it to a
        # terminal state.
        restarted = self.build()
        connection, descriptor, credential = self.session(restarted)
        with connection as session:
            self.authenticate(session, descriptor, credential)
            state = self.result(session, "engine.v1.jobs.get", {"job_id": job_id})
            self.assertEqual(state["state"], "interrupted")
            gate.set()
            # Nothing was re-queued on the caller's behalf: the refresh only
            # happens again because the caller asks again.
            fresh = self.result(
                session, "engine.v1.prices.refresh", {"idempotency_key": "interrupted"}
            )
            self.assertNotEqual(fresh["job_id"], job_id)
            self.assertEqual(
                self.await_terminal(session, fresh["job_id"])["state"], "completed"
            )
