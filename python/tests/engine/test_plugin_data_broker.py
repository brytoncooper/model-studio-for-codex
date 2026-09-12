import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from model_deck.adapters.storage.sqlite_plugin_data import SQLitePluginDataRepository
from model_deck.engine.plugin_authority import (
    ActivationIdentity, ActivationState, AuthorityDeniedError, OperationAuthority,
    OriginState, PluginAuthority,
)
from model_deck.engine.plugin_data.ports import PluginDataQuota
from model_deck.engine.plugin_data.service import (
    BrokerDataDeniedError, BrokerDataGuardError, BrokerDataInvalidRequestError, BrokerDataNotFoundError,
    BrokerDataPayloadInvalidError, BrokerDataQuotaExceededError, BrokerDataRevisionConflictError, PluginDataBroker,
)

READ = ("read", "data.private", "data.access")
WRITE = ("write", "data.private", "data.access")
GRANTS = {"get": READ, "list": READ, "put": WRITE, "delete": WRITE}


class MemoryContexts:
    def __init__(self):
        self.contexts = {}

    def put(self, context):
        if context.invocation_id in self.contexts:
            raise ValueError("collision")
        self.contexts[context.invocation_id] = context

    def get(self, invocation_id):
        return self.contexts.get(invocation_id)


class FakeState:
    def __init__(self, activation_state, origin_state, operation_state):
        self.activation_state = activation_state
        self.origin_state = origin_state
        self.operation_state = operation_state

    def activation(self, identity):
        return self.activation_state

    def origin(self, principal_id):
        return self.origin_state

    def operation(self, operation_id):
        return self.operation_state


class RecordingGuard:
    def __init__(self, log):
        self._log = log

    def __enter__(self):
        self._log.append("enter")
        return None

    def __exit__(self, *args):
        self._log.append("exit")
        return False


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 9, 12, tzinfo=timezone.utc)
        self.deadline = self.now + timedelta(minutes=5)
        self.identity = ActivationIdentity("engine", "broker", "act-1", "com.example.notes", "1.0.0")
        perms = dict(effects=frozenset({"read", "write"}),
                     resource_scopes=frozenset({"data.private"}),
                     capability_grants=frozenset({"data.access"}))
        self.state = FakeState(
            ActivationState(self.identity, **perms, expires_at=self.deadline, revocation_generation=2),
            OriginState("origin", "engine", "broker", **perms, expires_at=self.deadline, revocation_generation=3),
            OperationAuthority("broker.ops", **perms),
        )
        self.contexts = MemoryContexts()
        self.guard_log: list[str] = []
        log = self.guard_log

        @contextmanager
        def guard():
            recorder = RecordingGuard(log)
            with recorder:
                yield

        self.guard_factory = guard
        self.repo = SQLitePluginDataRepository(Path(self.tmp.name) / "data.sqlite")
        self.authority = PluginAuthority(engine_instance_id="engine", audience="broker",
                                         state=self.state, contexts=self.contexts,
                                         clock=lambda: self.now)
        self.broker = PluginDataBroker(authority=self.authority, repository=self.repo,
                                       grants=dict(GRANTS), mutation_guard=self.guard_factory)
        self.handle = self.authority.issue(self.identity, "origin", "broker.ops", expires_at=self.deadline)
        self.ns = "com.example.notes"

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_get_roundtrip_omitted_cas_unconditional(self):
        out = self.broker.put(self.handle, self.identity, namespace=self.ns, key="a", value={"n": 1})
        self.assertEqual(out, {"revision": 1})
        out = self.broker.put(self.handle, self.identity, namespace=self.ns, key="a", value={"n": 2})
        self.assertEqual(out, {"revision": 2})
        got = self.broker.get(self.handle, self.identity, namespace=self.ns, key="a")
        self.assertEqual(got, {"value": {"n": 2}, "revision": 2})

    def test_null_value_valid_and_empty_key_roundtrip(self):
        out = self.broker.put(self.handle, self.identity, namespace=self.ns, key="null-key", value=None)
        self.assertEqual(out, {"revision": 1})
        got = self.broker.get(self.handle, self.identity, namespace=self.ns, key="null-key")
        self.assertIsNone(got["value"])
        out = self.broker.put(self.handle, self.identity, namespace=self.ns, key="", value={"empty": True})
        self.assertEqual(out, {"revision": 1})
        got = self.broker.get(self.handle, self.identity, namespace=self.ns, key="")
        self.assertEqual(got, {"value": {"empty": True}, "revision": 1})
        out = self.broker.delete(self.handle, self.identity, namespace=self.ns, key="")
        self.assertEqual(out, {"deleted": True})
        with self.assertRaises(BrokerDataInvalidRequestError):
            self.broker.put(self.handle, self.identity, namespace=self.ns, key=None, value=1)

    def test_non_json_values_map_to_payload_invalid(self):
        bad_values = [
            {1: "int-key"},
            {"tuple": (1, 2)},
            float("nan"),
            float("inf"),
            {"nested": [{"ok": 1}, {2: 3}]},
            b"bytes",
            {"set": {1, 2}},
        ]
        for bad in bad_values:
            with self.assertRaises(BrokerDataPayloadInvalidError):
                self.broker.put(self.handle, self.identity, namespace=self.ns, key="bad", value=bad)
        cyclic: dict = {}
        cyclic["self"] = cyclic
        with self.assertRaises(BrokerDataPayloadInvalidError):
            self.broker.put(self.handle, self.identity, namespace=self.ns, key="bad", value=cyclic)
        cyclic_list: list = []
        cyclic_list.append(cyclic_list)
        with self.assertRaises(BrokerDataPayloadInvalidError):
            self.broker.put(self.handle, self.identity, namespace=self.ns, key="bad", value=cyclic_list)

    def test_guard_entry_failure_is_fixed_safe_error(self):
        secret = "SECRET-GUARD-TRACE-" + "x" * 16

        @contextmanager
        def failing_guard():
            raise RuntimeError(secret)
            yield

        broker = PluginDataBroker(authority=self.authority, repository=self.repo,
                                  grants=dict(GRANTS), mutation_guard=failing_guard)
        with self.assertRaises(BrokerDataGuardError) as caught:
            broker.get(self.handle, self.identity, namespace=self.ns, key="a")
        self.assertEqual(str(caught.exception), "broker guard failed")
        self.assertNotIn(secret, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_guard_exit_failure_reports_uncertainty_without_retry(self):
        secret = "SECRET-EXIT-TRACE-" + "y" * 16
        calls: list[str] = []
        inner_repo = self.repo

        class CountingRepo:
            def get(self, namespace, key):
                return inner_repo.get(namespace, key)
            def list(self, namespace, prefix="", limit=200):
                return inner_repo.list(namespace, prefix, limit)
            def put(self, namespace, key, value, expected_revision=None):
                calls.append("put")
                return inner_repo.put(namespace, key, value, expected_revision)
            def delete(self, namespace, key, expected_revision=None):
                return inner_repo.delete(namespace, key, expected_revision)

        @contextmanager
        def exit_failing_guard():
            yield
            raise RuntimeError(secret)

        broker = PluginDataBroker(authority=self.authority, repository=CountingRepo(),
                                  grants=dict(GRANTS), mutation_guard=exit_failing_guard)
        with self.assertRaises(BrokerDataGuardError) as caught:
            broker.put(self.handle, self.identity, namespace=self.ns, key="uncertain", value=1)
        self.assertEqual(str(caught.exception), "broker guard failed")
        self.assertNotIn(secret, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(calls, ["put"])

    def test_forged_handle_denied_before_repo(self):
        with self.assertRaises(BrokerDataDeniedError):
            self.broker.put("forged", self.identity, namespace=self.ns, key="a", value=1)
        with self.assertRaises(BrokerDataDeniedError):
            self.broker.get("forged", self.identity, namespace=self.ns, key="a")
        with self.assertRaises(BrokerDataDeniedError):
            self.broker.list("forged", self.identity, namespace=self.ns)
        with self.assertRaises(BrokerDataDeniedError):
            self.broker.delete("forged", self.identity, namespace=self.ns, key="a")

    def test_wrong_activation_denied(self):
        other = replace(self.identity, activation_id="other")
        with self.assertRaises(BrokerDataDeniedError):
            self.broker.get(self.handle, other, namespace=self.ns, key="a")

    def test_readonly_operation_cannot_write(self):
        self.broker.put(self.handle, self.identity, namespace=self.ns, key="seed", value=1)
        self.state.operation_state = replace(self.state.operation_state, effects=frozenset({"read"}))
        with self.assertRaises(BrokerDataDeniedError):
            self.broker.put(self.handle, self.identity, namespace=self.ns, key="seed", value=2)

    def test_cross_plugin_namespace_denied(self):
        with self.assertRaises(BrokerDataDeniedError):
            self.broker.get(self.handle, self.identity, namespace="com.example.other", key="a")
        with self.assertRaises(BrokerDataDeniedError):
            self.broker.put(self.handle, self.identity, namespace="com.example.other", key="a", value=1)
        with self.assertRaises(BrokerDataDeniedError):
            self.broker.list(self.handle, self.identity, namespace="com.example.other")
        with self.assertRaises(BrokerDataDeniedError):
            self.broker.delete(self.handle, self.identity, namespace="com.example.other", key="a")

    def test_revoked_generation_denied(self):
        self.state.activation_state = replace(
            self.state.activation_state,
            revocation_generation=self.state.activation_state.revocation_generation + 1)
        with self.assertRaises(BrokerDataDeniedError):
            self.broker.get(self.handle, self.identity, namespace=self.ns, key="a")

    def test_cas_conflict_and_not_found_mapping(self):
        self.broker.put(self.handle, self.identity, namespace=self.ns, key="k", value=1)
        with self.assertRaises(BrokerDataRevisionConflictError):
            self.broker.put(self.handle, self.identity, namespace=self.ns, key="k", value=2,
                            expected_revision=999)
        with self.assertRaises(BrokerDataNotFoundError):
            self.broker.get(self.handle, self.identity, namespace=self.ns, key="missing")
        out = self.broker.delete(self.handle, self.identity, namespace=self.ns, key="missing")
        self.assertEqual(out, {"deleted": False})

    def test_quota_mapping(self):
        tiny = SQLitePluginDataRepository(Path(self.tmp.name) / "tiny.sqlite",
                                          quota=PluginDataQuota(max_bytes=10**9, max_keys=10**9,
                                                                max_value_bytes=8))
        broker = PluginDataBroker(authority=self.authority, repository=tiny,
                                  grants=dict(GRANTS), mutation_guard=self.guard_factory)
        with self.assertRaises(BrokerDataQuotaExceededError):
            broker.put(self.handle, self.identity, namespace=self.ns, key="big",
                       value="x" * 64)

    def test_list_shape_has_no_cursor(self):
        self.broker.put(self.handle, self.identity, namespace=self.ns, key="b", value=1)
        self.broker.put(self.handle, self.identity, namespace=self.ns, key="a", value=2)
        out = self.broker.list(self.handle, self.identity, namespace=self.ns, prefix="", limit=200)
        self.assertEqual([i["key"] for i in out["items"]], ["a", "b"])
        self.assertNotIn("cursor", out)
        with self.assertRaises(BrokerDataInvalidRequestError):
            self.broker.list(self.handle, self.identity, namespace=self.ns, limit=0)

    def test_guard_covers_op_and_revocation_barrier_uses_same_guard(self):
        self.guard_log.clear()
        self.broker.put(self.handle, self.identity, namespace=self.ns, key="g", value=1)
        self.assertEqual(self.guard_log[:2], ["enter", "exit"])
        self.guard_log.clear()
        with self.broker.revocation_barrier():
            self.state.activation_state = replace(
                self.state.activation_state,
                revocation_generation=self.state.activation_state.revocation_generation + 1)
        self.assertEqual(self.guard_log, ["enter", "exit"])
        with self.assertRaises(BrokerDataDeniedError):
            self.broker.get(self.handle, self.identity, namespace=self.ns, key="g")


if __name__ == "__main__":
    unittest.main()
