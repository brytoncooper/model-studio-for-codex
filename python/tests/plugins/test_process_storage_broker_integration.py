"""Real process invocation composed with the trusted plugin-data broker."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from model_deck.adapters.storage.sqlite_plugin_data import SQLitePluginDataRepository
from model_deck.engine.plugin_authority import (
    ActivationIdentity,
    ActivationState,
    OperationAuthority,
    OriginState,
    PluginAuthority,
)
from model_deck.engine.plugin_data import PluginDataNotFoundError
from model_deck.engine.plugin_data.service import PluginDataBroker
from model_deck.engine.plugin_data.wire import PluginDataWireAdapter
from model_deck.plugins.lifecycle_session import LifecycleSession
from model_deck.plugins.process_runtime import ProcessRuntime, ProcessRuntimeConfig
from model_deck.plugins.process_runtime.errors import ProcessRuntimeError


PLUGIN_ID = "org.example.storage"
PLUGIN_VERSION = "1.0.0"
ACTIVATION_ID = "11111111-2222-4333-8444-555555555555"
ORIGIN_ID = "local.integration.test"
OPERATION_ID = "org.example.storage.write-read"
STORAGE_GET = "plugin.v1.broker.storage.get"
STORAGE_PUT = "plugin.v1.broker.storage.put"
BROKER_METHODS = (STORAGE_GET, STORAGE_PUT)
READ_GRANT = ("read", "data.private", "data.access")
WRITE_GRANT = ("write", "data.private", "data.access")
BROKER_GRANTS = {
    "get": READ_GRANT,
    "list": READ_GRANT,
    "put": WRITE_GRANT,
    "delete": WRITE_GRANT,
}


CHILD_CODE = f"""
import importlib.util
import json
import sys

PLUGIN_ID = {PLUGIN_ID!r}
ACTIVATION_ID = {ACTIVATION_ID!r}

if not sys.flags.isolated or importlib.util.find_spec("model_deck") is not None:
    raise RuntimeError("fixture requires isolated standard-library imports")

def send(payload):
    sys.stdout.write(json.dumps(payload, separators=(\",\", \":\")) + \"\\n\")
    sys.stdout.flush()

for line in sys.stdin:
    request = json.loads(line)
    method = request.get(\"method\", \"\")
    request_id = request.get(\"id\")
    if method == \"plugin.v1.lifecycle.hello\":
        send({{\"jsonrpc\": \"2.0\", \"id\": request_id, \"result\": {{
            \"plugin_id\": PLUGIN_ID,
            \"plugin_version\": {PLUGIN_VERSION!r},
            \"capabilities\": [],
        }}}})
        continue
    if method == \"plugin.v1.lifecycle.activate\":
        send({{\"jsonrpc\": \"2.0\", \"id\": request_id, \"result\": {{
            \"activation_id\": ACTIVATION_ID,
            \"invocation_handle_prefix\": \"storage:\",
        }}}})
        continue
    if method != \"plugin.v1.invoke\":
        raise RuntimeError(\"unexpected method\")

    invocation = request[\"params\"]
    broker_context = invocation[\"broker_context\"]
    invocation_input = invocation[\"input\"]
    namespace = (
        \"org.example.forged\"
        if invocation_input.get(\"forge_namespace\")
        else PLUGIN_ID
    )
    put_request_id = 7001
    send({{
        \"jsonrpc\": \"2.0\",
        \"id\": put_request_id,
        \"method\": \"plugin.v1.broker.storage.put\",
        \"params\": {{
            \"invocation_handle\": broker_context[\"invocation_handle\"],
            \"namespace\": namespace,
            \"key\": invocation_input[\"key\"],
            \"value\": invocation_input[\"value\"],
        }},
    }})
    put_response = json.loads(sys.stdin.readline())

    get_request_id = 7002
    send({{
        \"jsonrpc\": \"2.0\",
        \"id\": get_request_id,
        \"method\": \"plugin.v1.broker.storage.get\",
        \"params\": {{
            \"invocation_handle\": broker_context[\"invocation_handle\"],
            \"namespace\": namespace,
            \"key\": invocation_input[\"key\"],
        }},
    }})
    get_response = json.loads(sys.stdin.readline())
    send({{
        \"jsonrpc\": \"2.0\",
        \"id\": request_id,
        \"result\": {{
            \"output\": {{\"put\": put_response, \"get\": get_response}},
        }},
    }})
"""


class _MemoryContexts:
    def __init__(self) -> None:
        self.contexts = {}

    def put(self, context) -> None:
        if context.invocation_id in self.contexts:
            raise ValueError("context collision")
        self.contexts[context.invocation_id] = context

    def get(self, invocation_id):
        return self.contexts.get(invocation_id)


class _AuthorityState:
    def __init__(self, activation, origin, operation) -> None:
        self.activation_state = activation
        self.origin_state = origin
        self.operation_state = operation

    def activation(self, _identity):
        return self.activation_state

    def origin(self, _principal_id):
        return self.origin_state

    def operation(self, _operation_id):
        return self.operation_state


class ProcessStorageBrokerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(
            prefix="model-deck-process-storage-",
            dir="/tmp",
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        now = datetime(2026, 9, 12, tzinfo=timezone.utc)
        deadline = now + timedelta(minutes=5)
        permissions = {
            "effects": frozenset({"read", "write"}),
            "resource_scopes": frozenset({"data.private"}),
            "capability_grants": frozenset({"data.access"}),
        }
        self.identity = ActivationIdentity(
            "engine-test",
            "broker-test",
            ACTIVATION_ID,
            PLUGIN_ID,
            PLUGIN_VERSION,
        )
        self.state = _AuthorityState(
            ActivationState(
                self.identity,
                **permissions,
                expires_at=deadline,
                revocation_generation=2,
            ),
            OriginState(
                ORIGIN_ID,
                "engine-test",
                "broker-test",
                **permissions,
                expires_at=deadline,
                revocation_generation=3,
            ),
            OperationAuthority(OPERATION_ID, **permissions),
        )
        self.authority = PluginAuthority(
            engine_instance_id="engine-test",
            audience="broker-test",
            state=self.state,
            contexts=_MemoryContexts(),
            clock=lambda: now,
            invocation_id_factory=lambda: "invocation-storage-integration",
            handle_factory=lambda: "trusted:storage-integration",
        )
        self.repository = SQLitePluginDataRepository(self.root / "plugin-data.sqlite3")
        mutation_lock = threading.RLock()

        @contextmanager
        def mutation_guard():
            with mutation_lock:
                yield

        broker = PluginDataBroker(
            authority=self.authority,
            repository=self.repository,
            grants=dict(BROKER_GRANTS),
            mutation_guard=mutation_guard,
        )
        adapter = PluginDataWireAdapter(
            trusted_activation=self.identity,
            broker=broker,
        )
        self.runtime = ProcessRuntime(
            ProcessRuntimeConfig(
                argv=(sys.executable, "-I", "-c", CHILD_CODE),
                package_dir=str(self.root),
                timeout_s=2,
            ),
            allowed_broker_methods=BROKER_METHODS,
            broker_request_handler=adapter,
        )
        self.addCleanup(self.runtime.close)
        watchdog = threading.Timer(10, self.runtime.close)
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)

        self.runtime.spawn()
        lifecycle = LifecycleSession(
            expected_plugin_id=PLUGIN_ID,
            expected_plugin_version=PLUGIN_VERSION,
            offered_api_major=1,
            offered_api_minor=0,
            activation_token="storage-integration-token",
            allowed_broker_methods=BROKER_METHODS,
        )
        self.runtime.run_hello(lifecycle, "storage-integration-nonce")
        self.runtime.run_activation(lifecycle)
        self.channel = self.runtime.invocation_channel()
        self.assertEqual(self.channel.activation_id, self.identity.activation_id)
        self.handle = self.authority.issue(
            self.identity,
            ORIGIN_ID,
            OPERATION_ID,
            expires_at=deadline,
        )

    def _broker_context(self, **overrides):
        context = {
            "activation_id": ACTIVATION_ID,
            "plugin_id": PLUGIN_ID,
            "invocation_handle": self.handle,
            "revocation_generation": 2,
        }
        context.update(overrides)
        return context

    def _invoke(self, key: str, value, **input_overrides):
        invocation_input = {"key": key, "value": value}
        invocation_input.update(input_overrides)
        return self.channel.invoke(
            OPERATION_ID,
            invocation_input,
            self._broker_context(),
            timeout_s=2,
        )

    def _assert_missing(self, key: str, *, namespace: str = PLUGIN_ID) -> None:
        with self.assertRaises(PluginDataNotFoundError):
            self.repository.get(namespace, key)

    def test_child_put_and_get_round_trip_through_real_broker_composition(self) -> None:
        value = {"nested": [1, False, None]}
        result = self._invoke("round-trip", value)

        self.assertEqual(result["output"]["put"]["result"], {"revision": 1})
        self.assertEqual(
            result["output"]["get"]["result"],
            {"value": value, "revision": 1},
        )
        stored = self.repository.get(PLUGIN_ID, "round-trip")
        self.assertEqual(stored.value, value)
        self.assertEqual(stored.revision, 1)

    def test_forged_namespace_after_first_invoke_fails_before_mutation(self) -> None:
        self._invoke("seed", {"allowed": True})

        with self.assertRaises(ProcessRuntimeError) as captured:
            self._invoke("forged", {"blocked": True}, forge_namespace=True)

        self.assertEqual(captured.exception.code, "protocol")
        self._assert_missing("forged")
        self._assert_missing("forged", namespace="org.example.forged")
        self.assertEqual(
            self.repository.get(PLUGIN_ID, "seed").value,
            {"allowed": True},
        )

    def test_revoked_handle_after_first_invoke_fails_before_mutation(self) -> None:
        self._invoke("seed", {"allowed": True})
        self.state.activation_state = replace(
            self.state.activation_state,
            revocation_generation=self.state.activation_state.revocation_generation + 1,
        )

        with self.assertRaises(ProcessRuntimeError) as captured:
            self._invoke("revoked", {"blocked": True})

        self.assertEqual(captured.exception.code, "protocol")
        self._assert_missing("revoked")
        self.assertEqual(
            self.repository.get(PLUGIN_ID, "seed").value,
            {"allowed": True},
        )

    def test_runtime_activation_binding_rejects_context_before_mutation(self) -> None:
        with self.assertRaises(ProcessRuntimeError) as captured:
            self.channel.invoke(
                OPERATION_ID,
                {"key": "wrong-activation", "value": {"blocked": True}},
                self._broker_context(
                    activation_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                ),
                timeout_s=2,
            )

        self.assertEqual(captured.exception.code, "protocol")
        self._assert_missing("wrong-activation")


if __name__ == "__main__":
    unittest.main()
