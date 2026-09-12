import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from model_deck.engine.plugin_authority import (
    ActivationIdentity, ActivationState, OperationAuthority, OriginState,
    PluginAuthority,
)
from model_deck.engine.plugin_events import (
    BrokerAckRangeError,
    BrokerDeniedError,
    BrokerInvalidRequestError,
    BrokerPayloadInvalidError,
    BrokerSequenceConflictError,
    BrokerSubscriptionTerminatedError,
    BrokerUnknownSubscriptionError,
    BrokerUnknownTopicError,
    EventDescriptor,
    PluginEventBroker,
)

PERMISSIONS = dict(
    effects=frozenset({"notify"}),
    resource_scopes=frozenset({"decisions.content", "session.metadata"}),
    capability_grants=frozenset({
        "decisions.publish", "decisions.subscribe",
        "session.publish", "session.subscribe",
    }),
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


class DictState:
    def __init__(self, activations):
        self.activations = activations
        self.origin_state = OriginState("origin", "engine", "broker", **PERMISSIONS,
                                        expires_at=datetime(2026, 9, 12, 12, 5, tzinfo=timezone.utc),
                                        revocation_generation=3)
        self.operation_state = OperationAuthority("broker.ops", **PERMISSIONS)

    def activation(self, identity):
        return self.activations.get(identity.activation_id)

    def origin(self, principal_id):
        return self.origin_state

    def operation(self, operation_id):
        return self.operation_state


def is_choice(payload):
    return isinstance(payload, dict) and isinstance(payload.get("choice"), str)


class DictResolver:
    def __init__(self, descriptors):
        self.descriptors = descriptors

    def resolve(self, topic):
        return self.descriptors.get(topic)


class PluginEventBrokerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        self.deadline = self.now + timedelta(minutes=5)
        self.decider = ActivationIdentity("engine", "broker", "activation-decider",
                                          "com.example.decider", "1.0.0")
        self.viewer = ActivationIdentity("engine", "broker", "activation-viewer",
                                         "com.example.viewer", "1.0.0")
        self.state = DictState({
            self.decider.activation_id: ActivationState(self.decider, **PERMISSIONS,
                                                       expires_at=self.deadline,
                                                       revocation_generation=2),
            self.viewer.activation_id: ActivationState(self.viewer, **PERMISSIONS,
                                                      expires_at=self.deadline,
                                                      revocation_generation=2),
        })
        self.authority = PluginAuthority(
            engine_instance_id="engine", audience="broker", state=self.state,
            contexts=MemoryContexts(), clock=lambda: self.now)
        self.decider_handle = self.authority.issue(
            self.decider, "origin", "broker.ops", expires_at=self.deadline)
        self.viewer_handle = self.authority.issue(
            self.viewer, "origin", "broker.ops", expires_at=self.deadline)
        self.resolver = DictResolver({
            "decisions.root": EventDescriptor(
                topic="decisions.root", owner_plugin="com.example.decider",
                validate_payload=is_choice, publish_effect="notify",
                publish_resource_scope="decisions.content",
                publish_grant="decisions.publish", subscribe_effect="notify",
                subscribe_resource_scope="decisions.content",
                subscribe_grant="decisions.subscribe", classification="content"),
            "session.heartbeat": EventDescriptor(
                topic="session.heartbeat", owner_plugin="com.example.decider",
                validate_payload=None, publish_effect="notify",
                publish_resource_scope="session.metadata",
                publish_grant="session.publish", subscribe_effect="notify",
                subscribe_resource_scope="session.metadata",
                subscribe_grant="session.subscribe", classification="metadata"),
        })
        self.broker = PluginEventBroker(authority=self.authority, resolver=self.resolver)

    def subscribe_decider(self, topics):
        return self.broker.subscribe(
            self.decider_handle, self.decider, topics=topics)["subscription_id"]

    def test_round_trip_pull_ack_unsubscribe(self):
        sub = self.subscribe_decider(["decisions.root"])
        self.assertEqual(self.broker.publish(
            self.decider_handle, self.decider, topic="decisions.root",
            payload={"choice": "a"}, sequence=7), {"accepted": True})
        delivered = self.broker.pull(
            self.decider_handle, self.decider, subscription_id=sub)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["sequence"], 1)
        self.assertEqual(delivered[0]["topic"], "decisions.root")
        self.assertEqual(delivered[0]["payload"], {"choice": "a"})
        self.assertEqual(delivered[0]["classification"], "content")
        self.assertEqual(self.broker.ack(
            self.decider_handle, self.decider, subscription_id=sub,
            sequence=1), {"credit": 1})
        self.assertEqual(self.broker.unsubscribe(
            self.decider_handle, self.decider, subscription_id=sub),
            {"unsubscribed": True})

    def test_sequences_are_monotonic_across_topics(self):
        sub = self.subscribe_decider(["decisions.root", "session.heartbeat"])
        self.broker.publish(self.decider_handle, self.decider, topic="session.heartbeat",
                            payload=None, sequence=1)
        self.broker.publish(self.decider_handle, self.decider, topic="decisions.root",
                            payload={"choice": "b"}, sequence=1)
        delivered = self.broker.drain(self.decider_handle, self.decider,
                                      subscription_id=sub)
        self.assertEqual([item["sequence"] for item in delivered], [1, 2])
        self.assertEqual([item["topic"] for item in delivered],
                         ["session.heartbeat", "decisions.root"])
        self.assertIsNone(delivered[0]["payload"])

    def test_revocation_denies_before_delivery(self):
        sub = self.subscribe_decider(["decisions.root"])
        self.broker.publish(self.decider_handle, self.decider, topic="decisions.root",
                            payload={"choice": "a"}, sequence=1)
        original = self.state.activations[self.decider.activation_id]
        self.state.activations[self.decider.activation_id] = replace(
            original, revocation_generation=original.revocation_generation + 1)
        try:
            with self.assertRaises(BrokerDeniedError):
                self.broker.pull(self.decider_handle, self.decider,
                                 subscription_id=sub)
        finally:
            self.state.activations[self.decider.activation_id] = original

    def test_revocation_denies_before_ack(self):
        sub = self.subscribe_decider(["decisions.root"])
        self.broker.publish(self.decider_handle, self.decider, topic="decisions.root",
                            payload={"choice": "a"}, sequence=1)
        self.broker.pull(self.decider_handle, self.decider, subscription_id=sub)
        original = self.state.activations[self.decider.activation_id]
        self.state.activations[self.decider.activation_id] = replace(
            original, revocation_generation=original.revocation_generation + 1)
        try:
            with self.assertRaises(BrokerDeniedError):
                self.broker.ack(self.decider_handle, self.decider,
                                subscription_id=sub, sequence=1)
        finally:
            self.state.activations[self.decider.activation_id] = original

    def test_wrong_plugin_cannot_publish(self):
        sub = self.subscribe_decider(["decisions.root"])
        with self.assertRaises(BrokerDeniedError):
            self.broker.publish(self.viewer_handle, self.viewer,
                                topic="decisions.root",
                                payload={"choice": "a"}, sequence=1)
        self.assertEqual(self.broker.pull(
            self.decider_handle, self.decider, subscription_id=sub), [])

    def test_unknown_topic_and_wildcard_rejected(self):
        with self.assertRaises(BrokerUnknownTopicError):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="decisions.missing",
                                payload={"choice": "a"}, sequence=1)
        with self.assertRaises(BrokerUnknownTopicError):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="decisions.*", payload={"choice": "a"},
                                sequence=1)
        with self.assertRaises(BrokerUnknownTopicError):
            self.broker.subscribe(self.decider_handle, self.decider,
                                 topics=["decisions.*"])

    def test_content_schema_rejects_metadata_shape_violations(self):
        with self.assertRaises(BrokerPayloadInvalidError):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="decisions.root", payload={"nope": 1},
                                sequence=1)
        with self.assertRaises(BrokerPayloadInvalidError):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="decisions.root", payload=object(),
                                sequence=2)

    def test_producer_sequence_dedup_replay_and_conflict(self):
        sub = self.subscribe_decider(["decisions.root"])
        payload = {"choice": "a"}
        for _ in range(2):
            self.assertEqual(self.broker.publish(
                self.decider_handle, self.decider, topic="decisions.root",
                payload=payload, sequence=3), {"accepted": True})
        with self.assertRaises(BrokerSequenceConflictError):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="decisions.root", payload={"choice": "b"},
                                sequence=3)
        delivered = self.broker.drain(self.decider_handle, self.decider,
                                      subscription_id=sub)
        self.assertEqual(len(delivered), 1)

    def test_slow_consumer_terminated_independently(self):
        slow = self.subscribe_decider(["decisions.root"])
        healthy = self.subscribe_decider(["decisions.root"])
        for sequence in range(200):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="decisions.root",
                                payload={"choice": "a"}, sequence=sequence)
        self.assertEqual(len(self.broker.drain(
            self.decider_handle, self.decider, subscription_id=healthy)), 200)
        self.assertEqual(self.broker.ack(
            self.decider_handle, self.decider, subscription_id=healthy,
            sequence=200), {"credit": 200})
        for sequence in range(200, 257):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="decisions.root",
                                payload={"choice": "a"}, sequence=sequence)
        with self.assertRaises(BrokerSubscriptionTerminatedError):
            self.broker.pull(self.decider_handle, self.decider,
                             subscription_id=slow)
        delivered = self.broker.drain(self.decider_handle, self.decider,
                                      subscription_id=healthy)
        self.assertEqual(len(delivered), 57)

    def test_ack_cannot_reach_undelivered_future(self):
        sub = self.subscribe_decider(["decisions.root"])
        self.broker.publish(self.decider_handle, self.decider, topic="decisions.root",
                            payload={"choice": "a"}, sequence=1)
        with self.assertRaises(BrokerAckRangeError):
            self.broker.ack(self.decider_handle, self.decider,
                            subscription_id=sub, sequence=1)
        self.broker.publish(self.decider_handle, self.decider, topic="decisions.root",
                            payload={"choice": "b"}, sequence=2)
        delivered = self.broker.pull(self.decider_handle, self.decider,
                                     subscription_id=sub)
        self.assertEqual(len(delivered), 2)
        self.assertEqual(self.broker.ack(
            self.decider_handle, self.decider, subscription_id=sub,
            sequence=2), {"credit": 2})
        self.assertEqual(self.broker.ack(
            self.decider_handle, self.decider, subscription_id=sub,
            sequence=2), {"credit": 0})

    def test_request_bounds_and_unknown_subscription(self):
        with self.assertRaises(BrokerInvalidRequestError):
            self.broker.subscribe(self.decider_handle, self.decider, topics=[])
        with self.assertRaises(BrokerInvalidRequestError):
            self.broker.subscribe(self.decider_handle, self.decider,
                                 topics=["decisions.root"] * 33)
        with self.assertRaises(BrokerUnknownSubscriptionError):
            self.broker.pull(self.decider_handle, self.decider,
                             subscription_id="missing")


    def test_null_rejected_where_schema_disallows(self):
        with self.assertRaises(BrokerPayloadInvalidError):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="decisions.root", payload=None, sequence=1)

    def test_strict_json_rejects_nan_tuple_and_int_keys(self):
        with self.assertRaises(BrokerPayloadInvalidError):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="session.heartbeat",
                                payload=float("nan"), sequence=1)
        with self.assertRaises(BrokerPayloadInvalidError):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="session.heartbeat",
                                payload=("a", "b"), sequence=2)
        with self.assertRaises(BrokerPayloadInvalidError):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="session.heartbeat",
                                payload={1: "a"}, sequence=3)

    def test_credit_window_counts_delivered_until_ack(self):
        sub = self.subscribe_decider(["decisions.root"])
        for sequence in range(256):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="decisions.root",
                                payload={"choice": "a"}, sequence=sequence)
        self.assertEqual(len(self.broker.drain(
            self.decider_handle, self.decider, subscription_id=sub)), 256)
        self.broker.publish(self.decider_handle, self.decider,
                            topic="decisions.root", payload={"choice": "a"},
                            sequence=256)
        with self.assertRaises(BrokerSubscriptionTerminatedError):
            self.broker.pull(self.decider_handle, self.decider,
                             subscription_id=sub)

    def test_cumulative_ack_releases_window_once(self):
        sub = self.subscribe_decider(["decisions.root"])
        for sequence in range(3):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="decisions.root",
                                payload={"choice": "a"}, sequence=sequence)
        self.assertEqual(len(self.broker.pull(
            self.decider_handle, self.decider, subscription_id=sub,
            limit=3)), 3)
        self.assertEqual(self.broker.ack(
            self.decider_handle, self.decider, subscription_id=sub,
            sequence=2), {"credit": 2})
        self.assertEqual(self.broker.ack(
            self.decider_handle, self.decider, subscription_id=sub,
            sequence=3), {"credit": 1})
        self.assertEqual(self.broker.ack(
            self.decider_handle, self.decider, subscription_id=sub,
            sequence=3), {"credit": 0})

    def test_repeat_ack_reauthorizes_before_zero_return(self):
        sub = self.subscribe_decider(["decisions.root"])
        self.broker.publish(self.decider_handle, self.decider,
                            topic="decisions.root", payload={"choice": "a"},
                            sequence=1)
        self.broker.pull(self.decider_handle, self.decider,
                         subscription_id=sub)
        self.assertEqual(self.broker.ack(
            self.decider_handle, self.decider, subscription_id=sub,
            sequence=1), {"credit": 1})
        original = self.state.activations[self.decider.activation_id]
        from dataclasses import replace as _replace
        self.state.activations[self.decider.activation_id] = _replace(
            original, revocation_generation=original.revocation_generation + 1)
        try:
            with self.assertRaises(BrokerDeniedError):
                self.broker.ack(self.decider_handle, self.decider,
                                subscription_id=sub, sequence=1)
        finally:
            self.state.activations[self.decider.activation_id] = original

    def test_removed_descriptor_denies_delivery_and_ack(self):
        sub = self.subscribe_decider(["decisions.root"])
        self.broker.publish(self.decider_handle, self.decider,
                            topic="decisions.root", payload={"choice": "a"},
                            sequence=1)
        removed = dict(self.resolver.descriptors)
        del self.resolver.descriptors["decisions.root"]
        try:
            with self.assertRaises(BrokerDeniedError):
                self.broker.pull(self.decider_handle, self.decider,
                                 subscription_id=sub)
        finally:
            self.resolver.descriptors.update(removed)
        self.broker.pull(self.decider_handle, self.decider,
                         subscription_id=sub)
        narrowed = dict(self.resolver.descriptors)
        del self.resolver.descriptors["decisions.root"]
        try:
            with self.assertRaises(BrokerDeniedError):
                self.broker.ack(self.decider_handle, self.decider,
                                subscription_id=sub, sequence=1)
        finally:
            self.resolver.descriptors.update(narrowed)

    def test_older_producer_sequence_conflicts_without_refanout(self):
        sub = self.subscribe_decider(["decisions.root"])
        self.broker.publish(self.decider_handle, self.decider,
                            topic="decisions.root", payload={"choice": "a"},
                            sequence=5)
        self.broker.publish(self.decider_handle, self.decider,
                            topic="decisions.root", payload={"choice": "b"},
                            sequence=6)
        with self.assertRaises(BrokerSequenceConflictError):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="decisions.root",
                                payload={"choice": "a"}, sequence=5)
        delivered = self.broker.drain(self.decider_handle, self.decider,
                                      subscription_id=sub)
        self.assertEqual(len(delivered), 2)

    def test_produced_state_is_bounded_digest_only(self):
        sub = self.subscribe_decider(["decisions.root"])
        self.broker.publish(self.decider_handle, self.decider,
                            topic="decisions.root", payload={"choice": "a"},
                            sequence=1)
        key = (self.decider.activation_id, "decisions.root")
        entry = self.broker._produced[key]
        self.assertEqual(entry[0], 1)
        self.assertEqual(len(entry), 2)
        self.assertNotIn({"choice": "a"}, entry)
        self.assertTrue(all(type(part) is not bytes for part in entry[1:]))

    def test_pull_denied_batch_leaves_queue_intact(self):
        sub = self.subscribe_decider(["decisions.root", "session.heartbeat"])
        self.broker.publish(self.decider_handle, self.decider,
                            topic="decisions.root", payload={"choice": "a"},
                            sequence=1)
        self.broker.publish(self.decider_handle, self.decider,
                            topic="session.heartbeat", payload=None, sequence=1)
        original = self.resolver.descriptors["session.heartbeat"]
        self.resolver.descriptors["session.heartbeat"] = replace(
            original, subscribe_grant="session.subscribe.missing")
        try:
            with self.assertRaises(BrokerDeniedError):
                self.broker.pull(self.decider_handle, self.decider,
                                 subscription_id=sub)
        finally:
            self.resolver.descriptors["session.heartbeat"] = original
        delivered = self.broker.pull(self.decider_handle, self.decider,
                                     subscription_id=sub)
        self.assertEqual([item["topic"] for item in delivered],
                         ["decisions.root", "session.heartbeat"])
        self.assertEqual([item["sequence"] for item in delivered], [1, 2])

    def test_queued_content_keeps_original_grant_and_label(self):
        sub = self.subscribe_decider(["decisions.root"])
        self.broker.publish(self.decider_handle, self.decider,
                            topic="decisions.root", payload={"choice": "a"},
                            sequence=1)
        downgraded = replace(self.resolver.descriptors["decisions.root"],
                             classification="metadata",
                             subscribe_resource_scope="session.metadata",
                             subscribe_grant="session.subscribe")
        self.resolver.descriptors["decisions.root"] = downgraded
        try:
            delivered = self.broker.pull(self.decider_handle, self.decider,
                                         subscription_id=sub)
            self.assertEqual(delivered[0]["classification"], "content")
        finally:
            self.resolver.descriptors["decisions.root"] = replace(
                downgraded, classification="content",
                subscribe_resource_scope="decisions.content",
                subscribe_grant="decisions.subscribe")
        self.assertEqual(self.broker.ack(
            self.decider_handle, self.decider, subscription_id=sub,
            sequence=1), {"credit": 1})
        self.broker.publish(self.decider_handle, self.decider,
                            topic="decisions.root", payload={"choice": "b"},
                            sequence=2)
        self.resolver.descriptors["decisions.root"] = downgraded
        original_state = self.state.activations[self.decider.activation_id]
        self.state.activations[self.decider.activation_id] = replace(
            original_state, capability_grants=frozenset({
                "decisions.publish", "session.publish", "session.subscribe",
            }))
        try:
            with self.assertRaises(BrokerDeniedError):
                self.broker.pull(self.decider_handle, self.decider,
                                 subscription_id=sub)
        finally:
            self.state.activations[self.decider.activation_id] = original_state
            self.resolver.descriptors["decisions.root"] = replace(
                downgraded, classification="content",
                subscribe_resource_scope="decisions.content",
                subscribe_grant="decisions.subscribe")
        delivered = self.broker.pull(self.decider_handle, self.decider,
                                     subscription_id=sub)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["payload"], {"choice": "b"})
        self.assertEqual(delivered[0]["classification"], "content")

    def test_cyclic_shared_and_deep_payloads(self):
        cycle_list: list = []
        cycle_list.append(cycle_list)
        with self.assertRaises(BrokerPayloadInvalidError):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="session.heartbeat", payload=cycle_list,
                                sequence=1)
        cycle_dict: dict = {}
        cycle_dict["self"] = cycle_dict
        with self.assertRaises(BrokerPayloadInvalidError):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="session.heartbeat", payload=cycle_dict,
                                sequence=2)
        deep: list = []
        cursor = deep
        for _ in range(200):
            child: list = []
            cursor.append(child)
            cursor = child
        with self.assertRaises(BrokerPayloadInvalidError):
            self.broker.publish(self.decider_handle, self.decider,
                                topic="session.heartbeat", payload=deep,
                                sequence=3)
        shared = [1, 2]
        self.assertEqual(self.broker.publish(
            self.decider_handle, self.decider, topic="session.heartbeat",
            payload={"a": shared, "b": shared}, sequence=4),
            {"accepted": True})



    def test_ack_after_original_grant_revoked_and_narrowed_denies_without_credit(self):
        sub = self.subscribe_decider(["decisions.root"])
        self.broker.publish(self.decider_handle, self.decider, topic="decisions.root",
                            payload={"choice": "a"}, sequence=1)
        pulled = self.broker.pull(self.decider_handle, self.decider,
                                  subscription_id=sub)
        self.assertEqual(len(pulled), 1)
        held = self.resolver.descriptors["decisions.root"]
        original_state = self.state.activations[self.decider.activation_id]
        narrowed = replace(
            held, validate_payload=None, subscribe_effect="notify",
            subscribe_resource_scope="session.metadata",
            subscribe_grant="session.subscribe", classification="metadata")
        revoked = replace(
            original_state,
            capability_grants=frozenset(g for g in original_state.capability_grants
                                        if g != "decisions.subscribe"))
        self.resolver.descriptors["decisions.root"] = narrowed
        self.state.activations[self.decider.activation_id] = revoked
        try:
            with self.assertRaises(BrokerDeniedError):
                self.broker.ack(self.decider_handle, self.decider,
                                subscription_id=sub, sequence=1)
            with self.assertRaises(BrokerDeniedError):
                self.broker.ack(self.decider_handle, self.decider,
                                subscription_id=sub, sequence=1)
        finally:
            self.state.activations[self.decider.activation_id] = original_state
        self.assertEqual(
            self.broker.ack(self.decider_handle, self.decider,
                            subscription_id=sub, sequence=1), {"credit": 1})
        self.assertEqual(
            self.broker.ack(self.decider_handle, self.decider,
                            subscription_id=sub, sequence=1), {"credit": 0})
        self.resolver.descriptors["decisions.root"] = held


if __name__ == "__main__":
    unittest.main()
