"""Acceptance tests for the B18 lifecycle session slice.

These tests exercise the pure in-memory state machine only. They build
``plugin.v1`` lifecycle payloads inline, validate both directions through
the frozen schemas, and never spawn processes or perform network or
filesystem calls beyond importing the bundled contract validator.
"""
from __future__ import annotations

import copy
import unittest
import uuid

from model_deck.plugins.lifecycle_session import (
    LifecycleSession,
    SessionError,
    SessionErrorCode,
    SessionState,
)

PLUGIN_ID = "org.example.notebook"
PLUGIN_VERSION = "1.0.0"
TOKEN = "activation-token-opaque-value"
METHODS = ("plugin.v1.broker.storage.get", "plugin.v1.broker.jobs.create")


def _make_session(**overrides):
    params = {
        "expected_plugin_id": PLUGIN_ID,
        "expected_plugin_version": PLUGIN_VERSION,
        "offered_api_major": 1,
        "offered_api_minor": 0,
        "activation_token": TOKEN,
        "allowed_broker_methods": METHODS,
        "config_revision": 3,
    }
    params.update(overrides)
    return LifecycleSession(**params)


def _hello_result(**overrides):
    result = {"plugin_id": PLUGIN_ID, "plugin_version": PLUGIN_VERSION}
    result.update(overrides)
    return result


def _activation_result(**overrides):
    result = {
        "activation_id": str(uuid.uuid4()),
        "invocation_handle_prefix": "act-7:",
    }
    result.update(overrides)
    return result


def _to_active(session=None):
    session = session or _make_session()
    session.prepare_hello_request("nonce-001")
    session.accept_hello_result(_hello_result())
    request = session.prepare_activation_request()
    session.accept_activation_result(_activation_result())
    return session, request


class HappyPathTests(unittest.TestCase):
    def test_full_lifecycle_to_inactive(self) -> None:
        session = _make_session()
        self.assertEqual(session.state, SessionState.CREATED)
        hello = session.prepare_hello_request("nonce-001")
        self.assertEqual(
            hello,
            {"offered_api": {"major": 1, "minor": 0}, "nonce": "nonce-001"},
        )
        session.accept_hello_result(
            _hello_result(capabilities=["storage.own"])
        )
        self.assertEqual(session.state, SessionState.HELLO_VERIFIED)
        activation = session.prepare_activation_request()
        self.assertEqual(activation["activation_token"], TOKEN)
        self.assertEqual(activation["allowed_broker_methods"], list(METHODS))
        self.assertEqual(activation["config_revision"], 3)
        activation_id = str(uuid.uuid4())
        session.accept_activation_result(
            _activation_result(activation_id=activation_id)
        )
        self.assertEqual(session.state, SessionState.ACTIVE)
        self.assertEqual(session.activation_id, activation_id)
        first = session.admit_call("org.example.notebook.notes.create")
        second = session.admit_call("org.example.notebook.notes.create")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("act-7:"))
        self.assertEqual(session.outstanding, (first, second))
        session.resolve_call(first)
        self.assertEqual(session.outstanding, (second,))
        session.cancel_call(second)
        self.assertEqual(session.outstanding, ())
        drain = session.prepare_drain_request(10000)
        self.assertEqual(drain, {"deadline_ms": 10000})
        self.assertEqual(session.state, SessionState.DRAINING)
        session.accept_drain_result({"drained": True})
        self.assertEqual(session.state, SessionState.DRAINING)
        session.deactivate()
        self.assertEqual(session.state, SessionState.INACTIVE)

    def test_activation_without_config_revision_omits_field(self) -> None:
        session = _make_session(config_revision=None)
        session.prepare_hello_request("nonce-002")
        session.accept_hello_result(_hello_result())
        request = session.prepare_activation_request()
        self.assertNotIn("config_revision", request)

    def test_cancel_request_shape(self) -> None:
        session, _ = _to_active()
        job_id = str(uuid.uuid4())
        self.assertEqual(
            session.prepare_cancel_request(job_id), {"job_id": job_id}
        )


class IdentityTests(unittest.TestCase):
    def test_wrong_plugin_id_fails_session(self) -> None:
        session = _make_session()
        session.prepare_hello_request("nonce-010")
        with self.assertRaises(SessionError) as ctx:
            session.accept_hello_result(_hello_result(plugin_id="org.evil.other"))
        self.assertEqual(ctx.exception.code, SessionErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_wrong_plugin_version_fails_session(self) -> None:
        session = _make_session()
        session.prepare_hello_request("nonce-011")
        with self.assertRaises(SessionError) as ctx:
            session.accept_hello_result(_hello_result(plugin_version="9.9.9"))
        self.assertEqual(ctx.exception.code, SessionErrorCode.IDENTITY_MISMATCH)
        self.assertEqual(session.state, SessionState.FAILED)

    def test_malformed_hello_result_fails_session(self) -> None:
        session = _make_session()
        session.prepare_hello_request("nonce-012")
        with self.assertRaises(SessionError) as ctx:
            session.accept_hello_result({"plugin_id": PLUGIN_ID})
        self.assertEqual(ctx.exception.code, SessionErrorCode.SCHEMA_INVALID)
        self.assertEqual(session.state, SessionState.FAILED)

    def test_malformed_activation_result_fails_session(self) -> None:
        session = _make_session()
        session.prepare_hello_request("nonce-013")
        session.accept_hello_result(_hello_result())
        session.prepare_activation_request()
        with self.assertRaises(SessionError):
            session.accept_activation_result({"activation_id": "not-a-uuid"})
        self.assertEqual(session.state, SessionState.FAILED)


class OrderingTests(unittest.TestCase):
    def test_accept_hello_before_prepare_is_out_of_order(self) -> None:
        session = _make_session()
        with self.assertRaises(SessionError) as ctx:
            session.accept_hello_result(_hello_result())
        self.assertEqual(ctx.exception.code, SessionErrorCode.OUT_OF_ORDER)
        self.assertEqual(session.state, SessionState.CREATED)

    def test_activate_before_hello_verified_is_out_of_order(self) -> None:
        session = _make_session()
        with self.assertRaises(SessionError) as ctx:
            session.prepare_activation_request()
        self.assertEqual(ctx.exception.code, SessionErrorCode.OUT_OF_ORDER)
        self.assertEqual(session.state, SessionState.CREATED)

    def test_hello_replay_rejected(self) -> None:
        session = _make_session()
        session.prepare_hello_request("nonce-020")
        with self.assertRaises(SessionError) as ctx:
            session.prepare_hello_request("nonce-021")
        self.assertEqual(ctx.exception.code, SessionErrorCode.REPLAY)

    def test_second_hello_result_rejected(self) -> None:
        session = _make_session()
        session.prepare_hello_request("nonce-022")
        session.accept_hello_result(_hello_result())
        with self.assertRaises(SessionError) as ctx:
            session.accept_hello_result(_hello_result())
        self.assertEqual(ctx.exception.code, SessionErrorCode.OUT_OF_ORDER)
        self.assertEqual(session.state, SessionState.HELLO_VERIFIED)

    def test_admit_before_active_rejected(self) -> None:
        session = _make_session()
        session.prepare_hello_request("nonce-023")
        session.accept_hello_result(_hello_result())
        with self.assertRaises(SessionError) as ctx:
            session.admit_call("org.example.notebook.notes.create")
        self.assertEqual(ctx.exception.code, SessionErrorCode.OUT_OF_ORDER)

    def test_drain_stops_new_admissions(self) -> None:
        session, _ = _to_active()
        session.prepare_drain_request(5000)
        with self.assertRaises(SessionError) as ctx:
            session.admit_call("org.example.notebook.notes.create")
        self.assertEqual(ctx.exception.code, SessionErrorCode.OUT_OF_ORDER)
        self.assertEqual(session.state, SessionState.DRAINING)

    def test_terminal_state_rejects_work(self) -> None:
        session, _ = _to_active()
        session.prepare_drain_request(5000)
        session.deactivate()
        with self.assertRaises(SessionError) as ctx:
            session.admit_call("org.example.notebook.notes.create")
        self.assertEqual(ctx.exception.code, SessionErrorCode.TERMINAL)

    def test_unknown_handle_rejected(self) -> None:
        session, _ = _to_active()
        with self.assertRaises(SessionError) as ctx:
            session.resolve_call("act-7:00009999")
        self.assertEqual(ctx.exception.code, SessionErrorCode.UNKNOWN_HANDLE)


class HygieneTests(unittest.TestCase):
    def test_token_absent_from_repr_and_errors(self) -> None:
        session = _make_session()
        self.assertNotIn(TOKEN, repr(session))
        session.prepare_hello_request("nonce-030")
        with self.assertRaises(SessionError) as ctx:
            session.accept_hello_result(_hello_result(plugin_id="org.evil.x"))
        self.assertNotIn(TOKEN, str(ctx.exception))
        failure = session.failure
        assert failure is not None
        self.assertNotIn(TOKEN, str(failure))

    def test_request_outputs_are_detached(self) -> None:
        session = _make_session()
        hello = session.prepare_hello_request("nonce-031")
        hello["nonce"] = "mutated"
        hello["offered_api"]["major"] = 99
        session.accept_hello_result(_hello_result())
        activation = session.prepare_activation_request()
        self.assertEqual(activation["activation_token"], TOKEN)
        first = copy.deepcopy(activation)
        activation["allowed_broker_methods"].append("injected")
        second = session.prepare_activation_request()
        self.assertEqual(first, second)

    def test_clock_is_stored_not_called(self) -> None:
        calls: list = []

        def _clock() -> float:
            calls.append(1)
            return 1.0

        session = _make_session(clock=_clock)
        session.prepare_hello_request("nonce-032")
        self.assertEqual(calls, [])


class ReviewRepairTests(unittest.TestCase):
    def test_oversize_token_rejected_at_constructor_without_leak(self) -> None:
        token = "T" * 513
        with self.assertRaises(SessionError) as ctx:
            _make_session(activation_token=token)
        self.assertEqual(ctx.exception.code, SessionErrorCode.SCHEMA_INVALID)
        self.assertNotIn(token, str(ctx.exception))
        self.assertNotIn(token, repr(ctx.exception))

    def test_activation_result_without_prepare_rejected(self) -> None:
        session = _make_session()
        session.prepare_hello_request("nonce-101")
        session.accept_hello_result(_hello_result())
        with self.assertRaises(SessionError) as ctx:
            session.accept_activation_result(_activation_result())
        self.assertEqual(ctx.exception.code, SessionErrorCode.OUT_OF_ORDER)
        self.assertEqual(session.state, SessionState.HELLO_VERIFIED)

    def test_cancel_request_rejected_before_active(self) -> None:
        session = _make_session()
        with self.assertRaises(SessionError) as ctx:
            session.prepare_cancel_request("job-1")
        self.assertEqual(ctx.exception.code, SessionErrorCode.OUT_OF_ORDER)
        self.assertEqual(session.state, SessionState.CREATED)

    def test_resolve_rejected_before_active(self) -> None:
        session = _make_session()
        with self.assertRaises(SessionError) as ctx:
            session.resolve_call("act-7:00000001")
        self.assertEqual(ctx.exception.code, SessionErrorCode.OUT_OF_ORDER)

    def test_fail_does_not_persist_caller_detail(self) -> None:
        session, _ = _to_active()
        failure = session.fail("caller saw " + TOKEN)
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertNotIn(TOKEN, str(failure))
        assert session.failure is not None
        self.assertNotIn(TOKEN, str(session.failure))
        self.assertEqual(session.failure.detail, "caller-reported failure")


class SchemaBoundarySanitizationTests(unittest.TestCase):
    def test_hello_result_evil_plugin_version_no_leak(self) -> None:
        evil = {"plugin_id": PLUGIN_ID, "plugin_version": {"echo": TOKEN}}
        session = _make_session()
        session.prepare_hello_request("nonce-evil-1")
        with self.assertRaises(SessionError) as ctx:
            session.accept_hello_result(evil)
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn(TOKEN, repr(ctx.exception))
        assert session.failure is not None
        self.assertNotIn(TOKEN, str(session.failure))
        self.assertNotIn(TOKEN, repr(session.failure))

    def test_activation_result_evil_activation_id_no_leak(self) -> None:
        session = _make_session()
        session.prepare_hello_request("nonce-evil-2")
        session.accept_hello_result(_hello_result())
        session.prepare_activation_request()
        with self.assertRaises(SessionError) as ctx:
            session.accept_activation_result(
                {"activation_id": {"echo": TOKEN}, "invocation_handle_prefix": "x:"}
            )
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn(TOKEN, repr(ctx.exception))

    def test_drain_result_evil_drained_no_leak(self) -> None:
        session, _ = _to_active()
        session.prepare_drain_request(500)
        with self.assertRaises(SessionError) as ctx:
            session.accept_drain_result({"drained": {"echo": TOKEN}})
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn(TOKEN, repr(ctx.exception))

    def test_cancel_request_evil_job_id_no_leak(self) -> None:
        session, _ = _to_active()
        with self.assertRaises(SessionError) as ctx:
            session.prepare_cancel_request({"echo": TOKEN})
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn(TOKEN, repr(ctx.exception))

    def test_drain_request_evil_deadline_no_leak(self) -> None:
        session, _ = _to_active()
        with self.assertRaises(SessionError) as ctx:
            session.prepare_drain_request({"echo": TOKEN})
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn(TOKEN, repr(ctx.exception))
        self.assertEqual(session.state, SessionState.ACTIVE)

    def test_chained_cause_carries_no_token(self) -> None:
        import traceback
        session = _make_session()
        session.prepare_hello_request("nonce-evil-3")
        with self.assertRaises(SessionError) as ctx:
            session.accept_hello_result(
                {"plugin_id": PLUGIN_ID, "plugin_version": {"echo": TOKEN}}
            )
        self.assertIsNone(ctx.exception.__cause__)
        chained = "".join(
            traceback.format_exception(
                type(ctx.exception), ctx.exception, ctx.exception.__traceback__)
        )
        self.assertNotIn(TOKEN, chained)

if __name__ == "__main__":
    unittest.main()
