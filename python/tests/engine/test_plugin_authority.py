from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import unittest

from model_deck.engine.plugin_authority import (
    ActivationIdentity, ActivationState, AuthorityDeniedError, OperationAuthority,
    OriginState, PluginAuthority,
)


class MemoryContexts:
    def __init__(self):
        self.contexts = {}

    def put(self, context):
        if context.invocation_id in self.contexts:
            raise ValueError("context collision")
        self.contexts[context.invocation_id] = context

    def get(self, invocation_id):
        return self.contexts.get(invocation_id)


class CurrentState:
    def activation(self, identity):
        return self.activation_state

    def origin(self, principal_id):
        return self.origin_state

    def operation(self, operation_id):
        return self.operation_state


class PluginAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 12, tzinfo=timezone.utc)
        self.deadline = self.now + timedelta(minutes=5)
        self.identity = ActivationIdentity("engine", "broker", "activation", "com.example.notes", "1.0.0")
        self.state = CurrentState()
        permissions = dict(effects=frozenset({"read", "write"}),
                           resource_scopes=frozenset({"notes.private", "session.metadata"}),
                           capability_grants=frozenset({"notes.access", "session.read"}))
        self.state.activation_state = ActivationState(self.identity, **permissions,
                                                     expires_at=self.deadline, revocation_generation=2)
        self.state.origin_state = OriginState("origin", "engine", "broker", **permissions,
                                             expires_at=self.deadline, revocation_generation=3)
        self.state.operation_state = OperationAuthority("com.example.notes.create", **permissions)
        self.contexts = MemoryContexts()
        self.authority = self.new_authority()
        self.handle = self.authority.issue(self.identity, "origin", "com.example.notes.create", expires_at=self.deadline)

    def new_authority(self):
        return PluginAuthority(engine_instance_id="engine", audience="broker", state=self.state,
                               contexts=self.contexts, clock=lambda: self.now)

    def authorize(self, **overrides):
        arguments = dict(effect="write", resource_scope="notes.private", capability_grant="notes.access",
                         private_namespace="com.example.notes")
        arguments.update(overrides)
        return self.authority.authorize(self.handle, self.identity, **arguments)

    def test_valid_capture_is_immutable_and_scoped(self):
        context = self.authorize()
        self.assertEqual(context.activation, self.identity)
        self.assertEqual(context.expires_at, self.deadline)
        self.assertEqual(context.origin_generation, 3)
        with self.assertRaises(FrozenInstanceError):
            context.operation_id = "other"
        self.assertIsInstance(context.effects, frozenset)

    def test_forged_and_cross_activation_handles_denied(self):
        with self.assertRaises(AuthorityDeniedError):
            self.authority.capture("forged", self.identity)
        for field in ("engine_instance_id", "audience", "activation_id", "plugin_id", "plugin_version"):
            with self.subTest(field=field), self.assertRaises(AuthorityDeniedError):
                self.authority.capture(self.handle, replace(self.identity, **{field: "wrong"}))

    def test_issue_rejects_wrong_instance_audience_and_resolver_identity(self):
        for field in ("engine_instance_id", "audience", "activation_id"):
            with self.subTest(field=field), self.assertRaises(AuthorityDeniedError):
                self.authority.issue(replace(self.identity, **{field: "wrong"}), "origin",
                                     "com.example.notes.create", expires_at=self.deadline)

    def test_every_call_rechecks_both_revocation_generations(self):
        for owner in ("activation_state", "origin_state"):
            original = getattr(self.state, owner)
            setattr(self.state, owner, replace(original, revocation_generation=original.revocation_generation + 1))
            with self.subTest(owner=owner), self.assertRaises(AuthorityDeniedError):
                self.authorize()
            setattr(self.state, owner, original)

    def test_deadline_is_exclusive_and_never_extended_by_renewal(self):
        self.state.activation_state = replace(self.state.activation_state, expires_at=self.deadline + timedelta(days=1))
        self.state.origin_state = replace(self.state.origin_state, expires_at=self.deadline + timedelta(days=1))
        self.now = self.deadline
        with self.assertRaises(AuthorityDeniedError):
            self.authorize()

    def test_live_expiry_or_disable_denies_before_captured_deadline(self):
        for owner in ("activation_state", "origin_state"):
            original = getattr(self.state, owner)
            for change in ({"active": False}, {"expires_at": self.now}):
                setattr(self.state, owner, replace(original, **change))
                with self.subTest(owner=owner, change=change), self.assertRaises(AuthorityDeniedError):
                    self.authorize()
            setattr(self.state, owner, original)

    def test_read_operation_cannot_write_callee_private_data(self):
        self.state.operation_state = replace(self.state.operation_state, effects=frozenset({"read"}))
        self.authorize(effect="read")
        with self.assertRaises(AuthorityDeniedError):
            self.authorize()

    def test_low_privilege_origin_cannot_use_privileged_callee(self):
        self.state.origin_state = replace(self.state.origin_state, effects=frozenset({"read"}),
                                          resource_scopes=frozenset({"session.metadata"}))
        with self.assertRaises(AuthorityDeniedError):
            self.authorize()
        with self.assertRaises(AuthorityDeniedError):
            self.authorize(effect="read", resource_scope="session.content", capability_grant="session.read")

    def test_live_grant_removal_at_any_owner_denies(self):
        for owner in ("activation_state", "origin_state", "operation_state"):
            original = getattr(self.state, owner)
            setattr(self.state, owner, replace(original, capability_grants=frozenset()))
            with self.subTest(owner=owner), self.assertRaises(AuthorityDeniedError):
                self.authorize()
            setattr(self.state, owner, original)

    def test_later_grant_expansion_does_not_upgrade_capture(self):
        self.state.origin_state = replace(self.state.origin_state, effects=frozenset({"read"}))
        self.handle = self.authority.issue(self.identity, "origin", "com.example.notes.create", expires_at=self.deadline)
        self.state.origin_state = replace(self.state.origin_state, effects=frozenset({"read", "write"}))
        with self.assertRaises(AuthorityDeniedError):
            self.authorize()

    def test_exact_scope_and_grant_matching_and_private_namespace(self):
        for changes in ({"resource_scope": "notes.private.child"}, {"resource_scope": "notes.*"},
                        {"resource_scope": ""}, {"capability_grant": "notes"},
                        {"private_namespace": "com.other.notes"}):
            with self.subTest(changes=changes), self.assertRaises(AuthorityDeniedError):
                self.authorize(**changes)

    def test_captured_reauthorization_survives_new_service_not_new_activation(self):
        context = self.authority.capture(self.handle, self.identity)
        restarted = self.new_authority()
        with self.assertRaises(AuthorityDeniedError):
            restarted.capture(self.handle, self.identity)
        arguments = dict(effect="write", resource_scope="notes.private", capability_grant="notes.access")
        self.assertEqual(restarted.reauthorize_captured(context.invocation_id, self.identity, **arguments), context)
        with self.assertRaises(AuthorityDeniedError):
            restarted.reauthorize_captured(context.invocation_id, replace(self.identity, activation_id="new"), **arguments)
        self.state.origin_state = replace(self.state.origin_state, revocation_generation=4)
        with self.assertRaises(AuthorityDeniedError):
            restarted.reauthorize_captured(context.invocation_id, self.identity, **arguments)

    def test_captured_document_cannot_be_submitted_as_authority(self):
        context = self.authority.capture(self.handle, self.identity)
        with self.assertRaises(AuthorityDeniedError):
            self.authority.reauthorize_captured(context, self.identity, effect="write",
                                                resource_scope="notes.private", capability_grant="notes.access")

    def test_missing_state_denies_without_echo(self):
        for owner in ("activation_state", "origin_state", "operation_state"):
            original = getattr(self.state, owner)
            setattr(self.state, owner, None)
            with self.subTest(owner=owner), self.assertRaisesRegex(AuthorityDeniedError, "^plugin authority denied$"):
                self.authorize(resource_scope="secret-marker")
            setattr(self.state, owner, original)

    def test_naive_deadline_and_mutable_grants_rejected(self):
        with self.assertRaises(AuthorityDeniedError):
            self.authority.issue(self.identity, "origin", "com.example.notes.create", expires_at=datetime(2026, 10, 1))
        self.state.operation_state = replace(self.state.operation_state, effects={"read", "write"})
        with self.assertRaises(AuthorityDeniedError):
            self.authorize()


if __name__ == "__main__":
    unittest.main()
