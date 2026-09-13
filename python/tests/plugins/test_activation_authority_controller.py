from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from model_deck.engine.extensions.ports import ExecutableArtifact, SelectedInstallation
from model_deck.engine.plugin_authority import (
    ActivationIdentity,
    ActivationState,
    AuthorityDeniedError,
    OperationAuthority,
    OriginState,
    PluginAuthority,
)
from model_deck.plugins.activation_lifecycle import ActivationAuthorityController
from model_deck.plugins.activation_authority import (
    ActivationAuthorityConflictError,
    SQLiteActivationAuthorityController,
)


NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)
DEADLINE = NOW + timedelta(minutes=10)
ENGINE_ID = "engine-current"
AUDIENCE = "plugin-broker"
PLUGIN_ID = "com.example.notes"
OPERATION_ID = "com.example.notes.create"


class MemoryContexts:
    def __init__(self) -> None:
        self._values = {}

    def put(self, context) -> None:
        if context.invocation_id in self._values:
            raise ValueError("context collision")
        self._values[context.invocation_id] = context

    def get(self, invocation_id):
        return self._values.get(invocation_id)


class TrackingBarrier:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.entries = 0

    @contextmanager
    def hold(self):
        with self._lock:
            self.entries += 1
            yield


def selected(
    *,
    artifact_id: str = "a" * 64,
    data_ref: str = "ref:data.notes",
    grant_generation: int = 3,
    activation_generation: int = 5,
) -> SelectedInstallation:
    return SelectedInstallation(
        ExecutableArtifact(
            artifact_id,
            PLUGIN_ID,
            "1.2.3",
            ("notes.read", "notes.write"),
        ),
        data_ref,
        ("notes.read",),
        grant_generation,
        activation_generation,
    )


def identity(
    activation_id: str,
    *,
    engine_instance_id: str = ENGINE_ID,
) -> ActivationIdentity:
    return ActivationIdentity(
        engine_instance_id,
        AUDIENCE,
        activation_id,
        PLUGIN_ID,
        "1.2.3",
    )


class ActivationAuthorityControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "authority.sqlite"
        self.barrier = TrackingBarrier()
        self.policy_calls: list[tuple[ActivationIdentity, tuple[str, ...]]] = []
        self.origin = OriginState(
            "operator",
            ENGINE_ID,
            AUDIENCE,
            frozenset({"read"}),
            frozenset({"notes.private"}),
            frozenset({"notes.access"}),
            DEADLINE,
            7,
        )
        self.operation = OperationAuthority(
            OPERATION_ID,
            frozenset({"read"}),
            frozenset({"notes.private"}),
            frozenset({"notes.access"}),
        )
        self.controller = self.new_controller()

    def new_controller(
        self,
        *,
        engine_instance_id: str = ENGINE_ID,
    ) -> SQLiteActivationAuthorityController:
        def policy(
            activation: ActivationIdentity,
            approved_scopes: tuple[str, ...],
        ) -> ActivationState:
            self.policy_calls.append((activation, approved_scopes))
            return ActivationState(
                activation,
                frozenset({"read"}),
                frozenset({"notes.private"}),
                frozenset({"notes.access"}),
                DEADLINE,
                99,
                active=True,
            )

        return SQLiteActivationAuthorityController(
            self.db_path,
            engine_instance_id=engine_instance_id,
            audience=AUDIENCE,
            activation_policy=policy,
            origin_reader=lambda principal_id: (
                self.origin if principal_id == self.origin.principal_id else None
            ),
            operation_reader=lambda operation_id: (
                self.operation if operation_id == self.operation.operation_id else None
            ),
            mutation_guard=self.barrier.hold,
        )

    def authority(
        self,
        controller: SQLiteActivationAuthorityController | None = None,
        *,
        engine_instance_id: str = ENGINE_ID,
    ) -> PluginAuthority:
        return PluginAuthority(
            engine_instance_id=engine_instance_id,
            audience=AUDIENCE,
            state=controller or self.controller,
            contexts=MemoryContexts(),
            clock=lambda: NOW,
            invocation_id_factory=lambda: "invocation-one",
            handle_factory=lambda: "handle-one",
        )

    def test_candidate_is_structural_and_non_serving_until_admitted(self) -> None:
        candidate = selected()
        activation = identity("activation-one")

        self.assertIsInstance(self.controller, ActivationAuthorityController)
        self.controller.register_non_serving(activation, candidate)
        state = self.controller.activation(activation)

        self.assertEqual(self.controller.identity_for(candidate), activation)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertFalse(state.active)
        self.assertEqual(state.revocation_generation, 0)
        self.assertEqual(self.policy_calls, [(activation, candidate.approved_scopes)])
        with self.assertRaises(AuthorityDeniedError):
            self.authority().issue(
                activation, "operator", OPERATION_ID, expires_at=DEADLINE
            )

        self.controller.admit(activation, candidate)
        handle = self.authority().issue(
            activation, "operator", OPERATION_ID, expires_at=DEADLINE
        )
        self.assertEqual(handle, "handle-one")

    def test_policy_supplies_permissions_only_not_lifecycle_state(self) -> None:
        candidate = selected()
        activation = identity("activation-policy")
        self.controller.register_non_serving(activation, candidate)
        self.controller.admit(activation, candidate)

        state = self.controller.activation(activation)
        self.assertEqual(
            state,
            ActivationState(
                activation,
                frozenset({"read"}),
                frozenset({"notes.private"}),
                frozenset({"notes.access"}),
                DEADLINE,
                0,
                active=True,
            ),
        )

    def test_revocation_is_monotonic_and_never_revives_same_identity(self) -> None:
        candidate = selected()
        activation = identity("activation-revoked")
        self.controller.register_non_serving(activation, candidate)
        self.controller.admit(activation, candidate)
        authority = self.authority()
        handle = authority.issue(
            activation, "operator", OPERATION_ID, expires_at=DEADLINE
        )

        self.controller.revoke(activation)
        first = self.controller.activation(activation)
        self.controller.revoke(activation)
        second = self.controller.activation(activation)

        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        assert first is not None
        self.assertFalse(first.active)
        self.assertEqual(first.revocation_generation, 1)
        with self.assertRaises(AuthorityDeniedError):
            authority.capture(handle, activation)
        with self.assertRaises(ActivationAuthorityConflictError):
            self.controller.register_non_serving(activation, candidate)
        with self.assertRaises(ActivationAuthorityConflictError):
            self.controller.admit(activation, candidate)

    def test_full_selected_identity_must_match(self) -> None:
        candidate = selected()
        activation = identity("activation-exact")
        self.controller.register_non_serving(activation, candidate)
        alternatives = (
            selected(artifact_id="b" * 64),
            selected(data_ref="ref:data.other"),
            selected(grant_generation=4),
            selected(activation_generation=6),
        )

        for stale in alternatives:
            with self.subTest(stale=stale):
                self.assertIsNone(self.controller.identity_for(stale))
                with self.assertRaises(ActivationAuthorityConflictError):
                    self.controller.admit(activation, stale)

    def test_restart_retains_cleanup_identity_but_drops_serving_authority(self) -> None:
        candidate = selected()
        old_identity = identity("activation-old")
        self.controller.register_non_serving(old_identity, candidate)
        self.controller.admit(old_identity, candidate)

        reopened = self.new_controller(engine_instance_id=ENGINE_ID)

        self.assertEqual(reopened.identity_for(candidate), old_identity)
        restarted_state = reopened.activation(old_identity)
        self.assertIsNotNone(restarted_state)
        assert restarted_state is not None
        self.assertFalse(restarted_state.active)
        with self.assertRaises(AuthorityDeniedError):
            self.authority(reopened).issue(
                old_identity, "operator", OPERATION_ID, expires_at=DEADLINE
            )
        with self.assertRaises(ActivationAuthorityConflictError):
            reopened.admit(old_identity, candidate)

        restarted = self.new_controller(engine_instance_id="engine-restarted")
        self.assertEqual(restarted.identity_for(candidate), old_identity)
        with self.assertRaises(AuthorityDeniedError):
            self.authority(
                restarted, engine_instance_id="engine-restarted"
            ).issue(old_identity, "operator", OPERATION_ID, expires_at=DEADLINE)

        restarted.revoke(old_identity)
        fresh = identity(
            "activation-fresh", engine_instance_id="engine-restarted"
        )
        restarted.register_non_serving(fresh, candidate)
        restarted.admit(fresh, candidate)
        self.origin = replace(self.origin, engine_instance_id="engine-restarted")
        self.assertEqual(
            self.authority(restarted, engine_instance_id="engine-restarted").issue(
                fresh, "operator", OPERATION_ID, expires_at=DEADLINE
            ),
            "handle-one",
        )

    def test_rollback_requires_a_fresh_identity(self) -> None:
        prior = selected()
        forward = selected(
            artifact_id="b" * 64,
            data_ref="ref:data.notes.v2",
            grant_generation=4,
            activation_generation=6,
        )
        old = identity("activation-old")
        candidate = replace(identity("activation-new"), plugin_version="1.2.3")
        rollback = identity("activation-rollback")

        self.controller.register_non_serving(old, prior)
        self.controller.admit(old, prior)
        self.controller.revoke(old)
        self.controller.register_non_serving(candidate, forward)
        self.controller.admit(candidate, forward)
        self.controller.revoke(candidate)

        with self.assertRaises(ActivationAuthorityConflictError):
            self.controller.register_non_serving(old, prior)
        self.controller.register_non_serving(rollback, prior)
        self.controller.admit(rollback, prior)
        self.assertEqual(self.controller.identity_for(prior), rollback)
        self.assertFalse(self.controller.activation(old).active)
        self.assertTrue(self.controller.activation(rollback).active)

    def test_reads_and_mutations_use_the_injected_shared_barrier(self) -> None:
        candidate = selected()
        activation = identity("activation-barrier")
        after_init = self.barrier.entries

        self.controller.register_non_serving(activation, candidate)
        self.controller.identity_for(candidate)
        self.controller.activation(activation)
        self.controller.origin("operator")
        self.controller.operation(OPERATION_ID)
        self.controller.admit(activation, candidate)
        self.controller.revoke(activation)

        self.assertEqual(self.barrier.entries - after_init, 7)

    def test_sqlite_state_contains_no_bearer_token_field_or_value(self) -> None:
        candidate = selected(data_ref="ref:data.token-check")
        activation = identity("activation-token-check")
        self.controller.register_non_serving(activation, candidate)

        connection = sqlite3.connect(self.db_path)
        try:
            schema = "\n".join(
                row[0]
                for row in connection.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
                )
            )
            values = "\n".join(
                str(value)
                for row in connection.execute(
                    "SELECT * FROM plugin_activation_authority"
                )
                for value in row
            )
        finally:
            connection.close()
        self.assertNotIn("token", schema.lower())
        self.assertNotIn("bearer-secret", values)

    def test_connection_is_closed_when_sqlite_setup_fails(self) -> None:
        class FailingConnection:
            row_factory = None

            def __init__(self) -> None:
                self.closed = False

            def execute(self, statement: str) -> None:
                self.asserted_statement = statement
                raise sqlite3.OperationalError("injected setup failure")

            def close(self) -> None:
                self.closed = True

        connection = FailingConnection()
        with patch(
            "model_deck.plugins.activation_authority.controller.sqlite3.connect",
            return_value=connection,
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.new_controller()

        self.assertEqual(connection.asserted_statement, "PRAGMA foreign_keys = ON")
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
