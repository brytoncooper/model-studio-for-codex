from __future__ import annotations

import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.storage.sqlite_plugin_data import SQLitePluginDataRepository
from model_deck.engine.plugin_authority import (
    ActivationIdentity,
    ActivationState,
    OperationAuthority,
    OriginState,
    PluginAuthority,
)
from model_deck.engine.plugin_data.service import (
    BrokerDataDeniedError,
    PluginDataBroker,
)
from model_deck.engine.plugin_data.wire import (
    PluginDataWireActivationError,
    PluginDataWireAdapter,
    PluginDataWireRequestError,
    PluginDataWireResultError,
)


READ = ("read", "data.private", "data.access")
WRITE = ("write", "data.private", "data.access")
GRANTS = {"get": READ, "list": READ, "put": WRITE, "delete": WRITE}
NAMESPACE = "com.example.notes"


class MemoryContexts:
    def __init__(self) -> None:
        self.contexts = {}

    def put(self, context) -> None:
        if context.invocation_id in self.contexts:
            raise ValueError("collision")
        self.contexts[context.invocation_id] = context

    def get(self, invocation_id):
        return self.contexts.get(invocation_id)


class AuthorityState:
    def __init__(self, activation, origin, operation) -> None:
        self.activation_state = activation
        self.origin_state = origin
        self.operation_state = operation

    def activation(self, identity):
        return self.activation_state

    def origin(self, principal_id):
        return self.origin_state

    def operation(self, operation_id):
        return self.operation_state


class RecordingSQLiteRepository:
    def __init__(self, path: Path) -> None:
        self.inner = SQLitePluginDataRepository(path)
        self.calls: list[tuple] = []

    def get(self, namespace, key):
        self.calls.append(("get", namespace, key))
        return self.inner.get(namespace, key)

    def list(self, namespace, prefix="", limit=200):
        self.calls.append(("list", namespace, prefix, limit))
        return self.inner.list(namespace, prefix, limit)

    def put(self, namespace, key, value, expected_revision=None):
        self.calls.append(("put", namespace, key, value, expected_revision))
        return self.inner.put(namespace, key, value, expected_revision)

    def delete(self, namespace, key, expected_revision=None):
        self.calls.append(("delete", namespace, key, expected_revision))
        return self.inner.delete(namespace, key, expected_revision)


class InvalidResultBroker(PluginDataBroker):
    def get(self, handle, authenticated_activation, *, namespace, key):
        return {"value": "invalid", "revision": -1}


class PluginDataWireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        now = datetime(2026, 9, 12, tzinfo=timezone.utc)
        deadline = now + timedelta(minutes=5)
        self.identity = ActivationIdentity(
            "engine",
            "broker",
            "activation-1",
            NAMESPACE,
            "1.0.0",
        )
        permissions = {
            "effects": frozenset({"read", "write"}),
            "resource_scopes": frozenset({"data.private"}),
            "capability_grants": frozenset({"data.access"}),
        }
        self.state = AuthorityState(
            ActivationState(
                self.identity,
                **permissions,
                expires_at=deadline,
                revocation_generation=2,
            ),
            OriginState(
                "origin",
                "engine",
                "broker",
                **permissions,
                expires_at=deadline,
                revocation_generation=3,
            ),
            OperationAuthority("broker.ops", **permissions),
        )
        self.authority = PluginAuthority(
            engine_instance_id="engine",
            audience="broker",
            state=self.state,
            contexts=MemoryContexts(),
            clock=lambda: now,
            invocation_id_factory=lambda: "invocation-1",
            handle_factory=lambda: "handle-1",
        )
        self.handle = self.authority.issue(
            self.identity,
            "origin",
            "broker.ops",
            expires_at=deadline,
        )
        self.guard_log: list[str] = []

        @contextmanager
        def guard():
            self.guard_log.append("enter")
            try:
                yield
            finally:
                self.guard_log.append("exit")

        self.guard = guard
        self.repository = RecordingSQLiteRepository(
            Path(self.temporary_directory.name) / "plugin-data.sqlite3"
        )
        self.broker = PluginDataBroker(
            authority=self.authority,
            repository=self.repository,
            grants=dict(GRANTS),
            mutation_guard=self.guard,
        )
        self.adapter = PluginDataWireAdapter(
            trusted_activation=self.identity,
            broker=self.broker,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def call(self, method: str, params: dict):
        return self.adapter(
            self.identity.activation_id,
            f"plugin.v1.broker.storage.{method}",
            params,
        )

    def test_four_methods_round_trip_with_exact_fields_and_defaults(self) -> None:
        value = {"nested": [1, False, None]}
        created = self.call(
            "put",
            {
                "invocation_handle": self.handle,
                "namespace": NAMESPACE,
                "key": "alpha",
                "value": value,
            },
        )
        self.assertEqual(created, {"revision": 1})
        self.assertEqual(
            self.call(
                "get",
                {
                    "invocation_handle": self.handle,
                    "namespace": NAMESPACE,
                    "key": "alpha",
                },
            ),
            {"value": value, "revision": 1},
        )
        self.assertEqual(
            self.call(
                "list",
                {
                    "invocation_handle": self.handle,
                    "namespace": NAMESPACE,
                },
            ),
            {"items": [{"key": "alpha", "revision": 1}]},
        )
        self.assertEqual(
            self.call(
                "put",
                {
                    "invocation_handle": self.handle,
                    "namespace": NAMESPACE,
                    "key": "alpha",
                    "value": "updated",
                    "expected_revision": 1,
                },
            ),
            {"revision": 2},
        )
        self.assertEqual(
            self.call(
                "delete",
                {
                    "invocation_handle": self.handle,
                    "namespace": NAMESPACE,
                    "key": "alpha",
                    "expected_revision": 2,
                },
            ),
            {"deleted": True},
        )
        self.assertIs(self.repository.calls[0][3], value)
        self.assertEqual(
            self.repository.calls,
            [
                ("put", NAMESPACE, "alpha", value, None),
                ("get", NAMESPACE, "alpha"),
                ("list", NAMESPACE, "", 200),
                ("put", NAMESPACE, "alpha", "updated", 1),
                ("delete", NAMESPACE, "alpha", 2),
            ],
        )
        self.assertEqual(
            self.guard_log,
            ["enter", "exit"] * 5,
        )

    def test_forged_runtime_activation_is_rejected_before_broker_access(self) -> None:
        with self.assertRaises(PluginDataWireActivationError):
            self.adapter(
                "forged-activation",
                "plugin.v1.broker.storage.put",
                {
                    "invocation_handle": self.handle,
                    "namespace": NAMESPACE,
                    "key": "blocked",
                    "value": 1,
                },
            )
        self.assertEqual(self.guard_log, [])
        self.assertEqual(self.repository.calls, [])

    def test_forged_namespace_preserves_broker_denial_and_has_no_store_effect(self) -> None:
        with self.assertRaises(BrokerDataDeniedError):
            self.call(
                "put",
                {
                    "invocation_handle": self.handle,
                    "namespace": "com.example.other",
                    "key": "blocked",
                    "value": 1,
                },
            )
        self.assertEqual(self.repository.calls, [])
        self.assertEqual(self.guard_log, ["enter", "exit"])

    def test_revoked_handle_preserves_broker_denial_and_has_no_store_effect(self) -> None:
        self.state.activation_state = replace(
            self.state.activation_state,
            revocation_generation=self.state.activation_state.revocation_generation + 1,
        )
        with self.assertRaises(BrokerDataDeniedError):
            self.call(
                "put",
                {
                    "invocation_handle": self.handle,
                    "namespace": NAMESPACE,
                    "key": "blocked",
                    "value": 1,
                },
            )
        self.assertEqual(self.repository.calls, [])

    def test_invalid_method_and_params_fail_with_fixed_error_before_broker(self) -> None:
        invalid_calls = (
            (
                "plugin.v1.broker.storage.drop_all",
                {"invocation_handle": self.handle, "namespace": NAMESPACE},
            ),
            (
                "plugin.v1.broker.storage.put",
                {
                    "invocation_handle": self.handle,
                    "namespace": NAMESPACE,
                    "key": "blocked",
                    "value": 1,
                    "authenticated_activation_id": "forged",
                },
            ),
            (
                "plugin.v1.broker.storage.delete",
                {
                    "invocation_handle": self.handle,
                    "namespace": NAMESPACE,
                    "key": "blocked",
                    "expected_revision": False,
                },
            ),
        )
        for method, params in invalid_calls:
            with self.subTest(method=method, params=params):
                with self.assertRaises(PluginDataWireRequestError) as caught:
                    self.adapter(self.identity.activation_id, method, params)
                self.assertEqual(
                    str(caught.exception),
                    "plugin data wire request invalid",
                )
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
        self.assertEqual(self.guard_log, [])
        self.assertEqual(self.repository.calls, [])

    def test_invalid_broker_result_fails_fixed_result_schema_validation(self) -> None:
        invalid_broker = InvalidResultBroker(
            authority=self.authority,
            repository=self.repository,
            grants=dict(GRANTS),
            mutation_guard=self.guard,
        )
        adapter = PluginDataWireAdapter(
            trusted_activation=self.identity,
            broker=invalid_broker,
        )
        with self.assertRaises(PluginDataWireResultError) as caught:
            adapter(
                self.identity.activation_id,
                "plugin.v1.broker.storage.get",
                {
                    "invocation_handle": self.handle,
                    "namespace": NAMESPACE,
                    "key": "alpha",
                },
            )
        self.assertEqual(
            str(caught.exception),
            "plugin data wire result invalid",
        )
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)


if __name__ == "__main__":
    unittest.main()
