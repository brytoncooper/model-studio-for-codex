"""In-memory plugin event broker over settled PluginAuthority.

Wire shapes mirror contracts/plugin.v1/broker events.publish/subscribe/ack/
unsubscribe. Authenticated activations arrive via the supervisor private
channel (method arguments), never from request documents.

Producer deduplication policy (bounded): per (activation, topic) the broker
retains only a high-water producer sequence plus the SHA256 digest of the
latest canonical payload. A publish with the exact latest sequence and digest
replays as accepted without refanout. Any other publish whose sequence is not
strictly greater than the high-water mark conflicts and never refanouts. This
bounds history to one small entry per producer/topic.

Pull preflights the entire returned batch (original plus current subscribe
authorization and current schema) before moving any envelope, so a denied
batch fails atomically with queues untouched. Each envelope carries the
subscribe grant triple and classification captured at admission; delivery
requires both the original and the current grant and always reports the
original classification. Strict payload checks reject object cycles and
nesting beyond a fixed depth instead of recursing without bound.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field

from ..plugin_authority import (
    ActivationIdentity,
    AuthorityDeniedError,
    PluginAuthority,
)
from .errors import (
    BrokerAckRangeError,
    BrokerDeniedError,
    BrokerInvalidRequestError,
    BrokerPayloadInvalidError,
    BrokerSequenceConflictError,
    BrokerSubscriptionTerminatedError,
    BrokerUnknownSubscriptionError,
    BrokerUnknownTopicError,
)
from .ports import EventDescriptor, EventDescriptorResolver

MAX_TOPICS_PER_SUBSCRIPTION = 32
MAX_EVENTS_PER_SUBSCRIPTION = 256
MAX_BYTES_PER_SUBSCRIPTION = 1_048_576


_MAX_JSON_DEPTH = 100


def _strict_check(payload: object) -> None:
    stack: list[tuple[object, int, bool]] = [(payload, 0, False)]
    active: set[int] = set()
    while stack:
        value, depth, exiting = stack.pop()
        if value is None or type(value) is str or type(value) is bool:
            continue
        if type(value) is int:
            continue
        if type(value) is float:
            if not math.isfinite(value):
                raise BrokerPayloadInvalidError()
            continue
        if type(value) is list:
            if exiting:
                active.discard(id(value))
                continue
            if depth >= _MAX_JSON_DEPTH or id(value) in active:
                raise BrokerPayloadInvalidError()
            active.add(id(value))
            stack.append((value, depth, True))
            for item in value:
                stack.append((item, depth + 1, False))
            continue
        if type(value) is dict:
            if exiting:
                active.discard(id(value))
                continue
            if depth >= _MAX_JSON_DEPTH or id(value) in active:
                raise BrokerPayloadInvalidError()
            active.add(id(value))
            stack.append((value, depth, True))
            for key, item in value.items():
                if type(key) is not str:
                    raise BrokerPayloadInvalidError()
                stack.append((item, depth + 1, False))
            continue
        raise BrokerPayloadInvalidError()


def _canonical(payload: object) -> bytes:
    _strict_check(payload)
    try:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise BrokerPayloadInvalidError()


@dataclass(slots=True)
class _Envelope:
    broker_seq: int
    topic: str
    canonical: bytes
    size: int
    classification: str
    subscribe_effect: str
    subscribe_resource_scope: str
    subscribe_grant: str


@dataclass(slots=True)
class _Subscription:
    subscription_id: str
    invocation_id: str
    activation: ActivationIdentity
    topics: tuple[str, ...]
    descriptors: dict[str, EventDescriptor]
    queue: deque = field(default_factory=deque)
    queued_bytes: int = 0
    unacked: deque = field(default_factory=deque)
    unacked_bytes: int = 0
    next_seq: int = 1
    delivered_seq: int = 0
    acked_seq: int = 0
    terminated: bool = False


class PluginEventBroker:
    """Supervisor-side broker; pull/drain only, no sockets or callbacks."""

    def __init__(
        self, *,
        authority: PluginAuthority,
        resolver: EventDescriptorResolver,
        subscription_id_factory: object = None,
    ) -> None:
        if not isinstance(authority, PluginAuthority):
            raise BrokerInvalidRequestError()
        self._authority = authority
        self._resolver = resolver
        self._new_subscription_id = subscription_id_factory or (lambda: str(uuid.uuid4()))
        self._lock = threading.RLock()
        self._subscriptions: dict[str, _Subscription] = {}
        self._produced: dict[tuple[str, str], tuple[int, str]] = {}

    def _resolve(self, topic: object) -> EventDescriptor:
        if type(topic) is not str or not topic:
            raise BrokerInvalidRequestError()
        descriptor = self._resolver.resolve(topic)
        if not isinstance(descriptor, EventDescriptor) or descriptor.topic != topic:
            raise BrokerUnknownTopicError()
        if type(descriptor.owner_plugin) is not str or not descriptor.owner_plugin:
            raise BrokerUnknownTopicError()
        if descriptor.classification not in ("metadata", "content"):
            raise BrokerUnknownTopicError()
        for value in (
            descriptor.publish_effect, descriptor.publish_resource_scope,
            descriptor.publish_grant, descriptor.subscribe_effect,
            descriptor.subscribe_resource_scope, descriptor.subscribe_grant,
        ):
            if type(value) is not str or not value:
                raise BrokerUnknownTopicError()
        if descriptor.validate_payload is not None and not callable(descriptor.validate_payload):
            raise BrokerUnknownTopicError()
        return descriptor

    def _current(self, topic: str) -> EventDescriptor:
        try:
            return self._resolve(topic)
        except (BrokerUnknownTopicError, BrokerInvalidRequestError):
            raise BrokerDeniedError()

    @staticmethod
    def _check_sequence(sequence: object) -> int:
        if type(sequence) is not int or sequence < 0:
            raise BrokerInvalidRequestError()
        return sequence

    def _authorize_publish(
        self, handle: str, activation: ActivationIdentity, descriptor: EventDescriptor
    ) -> None:
        try:
            context = self._authority.authorize(
                handle, activation, effect=descriptor.publish_effect,
                resource_scope=descriptor.publish_resource_scope,
                capability_grant=descriptor.publish_grant,
            )
        except AuthorityDeniedError:
            raise BrokerDeniedError()
        if context.activation.plugin_id != descriptor.owner_plugin:
            raise BrokerDeniedError()

    def _reauthorize_triple(
        self, invocation_id: str, activation: ActivationIdentity, *, effect: str,
        resource_scope: str, capability_grant: str,
    ) -> None:
        try:
            self._authority.reauthorize_captured(
                invocation_id, activation, effect=effect,
                resource_scope=resource_scope, capability_grant=capability_grant,
            )
        except AuthorityDeniedError:
            raise BrokerDeniedError()

    def _reauthorize(
        self, invocation_id: str, activation: ActivationIdentity, descriptor: EventDescriptor,
        *, subscribe: bool,
    ) -> None:
        if subscribe:
            self._reauthorize_triple(
                invocation_id, activation, effect=descriptor.subscribe_effect,
                resource_scope=descriptor.subscribe_resource_scope,
                capability_grant=descriptor.subscribe_grant,
            )
        else:
            self._reauthorize_triple(
                invocation_id, activation, effect=descriptor.publish_effect,
                resource_scope=descriptor.publish_resource_scope,
                capability_grant=descriptor.publish_grant,
            )

    def publish(
        self, handle: str, authenticated_activation: ActivationIdentity, *,
        topic: str, payload: object, sequence: int,
    ) -> dict[str, bool]:
        descriptor = self._resolve(topic)
        producer_seq = self._check_sequence(sequence)
        self._authorize_publish(handle, authenticated_activation, descriptor)
        canonical = _canonical(payload)
        try:
            valid = descriptor.validate_payload is None or bool(descriptor.validate_payload(payload))
        except BrokerPayloadInvalidError:
            raise
        except Exception:
            raise BrokerPayloadInvalidError()
        if not valid:
            raise BrokerPayloadInvalidError()
        digest = hashlib.sha256(canonical).hexdigest()
        envelope_size = len(canonical)
        key = (authenticated_activation.activation_id, topic)
        with self._lock:
            entry = self._produced.get(key)
            if entry is not None:
                last_seq, last_digest = entry
                if producer_seq == last_seq and digest == last_digest:
                    return {"accepted": True}
                if producer_seq <= last_seq:
                    raise BrokerSequenceConflictError()
            for sub in self._subscriptions.values():
                if sub.terminated or topic not in sub.descriptors:
                    continue
                used = len(sub.queue) + len(sub.unacked)
                used_bytes = sub.queued_bytes + sub.unacked_bytes
                if used >= MAX_EVENTS_PER_SUBSCRIPTION \
                        or used_bytes + envelope_size > MAX_BYTES_PER_SUBSCRIPTION:
                    sub.terminated = True
                    sub.queue.clear()
                    sub.queued_bytes = 0
                    continue
                sub.queue.append(_Envelope(
                    broker_seq=sub.next_seq, topic=topic, canonical=canonical,
                    size=envelope_size, classification=descriptor.classification,
                    subscribe_effect=descriptor.subscribe_effect,
                    subscribe_resource_scope=descriptor.subscribe_resource_scope,
                    subscribe_grant=descriptor.subscribe_grant,
                ))
                sub.queued_bytes += envelope_size
                sub.next_seq += 1
            self._produced[key] = (producer_seq, digest)
        return {"accepted": True}

    def subscribe(
        self, handle: str, authenticated_activation: ActivationIdentity, *,
        topics: list[str],
    ) -> dict[str, str]:
        if type(topics) is not list or not 1 <= len(topics) <= MAX_TOPICS_PER_SUBSCRIPTION:
            raise BrokerInvalidRequestError()
        descriptors: dict[str, EventDescriptor] = {}
        for topic in topics:
            descriptor = self._resolve(topic)
            descriptors.setdefault(topic, descriptor)
        try:
            context = self._authority.capture(handle, authenticated_activation)
        except AuthorityDeniedError:
            raise BrokerDeniedError()
        invocation_id = context.invocation_id
        for descriptor in descriptors.values():
            try:
                self._authority.reauthorize_captured(
                    invocation_id, authenticated_activation,
                    effect=descriptor.subscribe_effect,
                    resource_scope=descriptor.subscribe_resource_scope,
                    capability_grant=descriptor.subscribe_grant,
                )
            except AuthorityDeniedError:
                raise BrokerDeniedError()
        subscription_id = self._new_subscription_id()
        if type(subscription_id) is not str or not subscription_id:
            raise BrokerInvalidRequestError()
        with self._lock:
            if subscription_id in self._subscriptions:
                raise BrokerInvalidRequestError()
            self._subscriptions[subscription_id] = _Subscription(
                subscription_id=subscription_id, invocation_id=invocation_id,
                activation=authenticated_activation, topics=tuple(descriptors),
                descriptors=descriptors,
            )
        return {"subscription_id": subscription_id}

    def _get(self, subscription_id: object) -> _Subscription:
        if type(subscription_id) is not str or not subscription_id:
            raise BrokerInvalidRequestError()
        with self._lock:
            sub = self._subscriptions.get(subscription_id)
        if sub is None:
            raise BrokerUnknownSubscriptionError()
        return sub

    def pull(
        self, handle: str, authenticated_activation: ActivationIdentity, *,
        subscription_id: str, limit: int = 64,
    ) -> list[dict[str, object]]:
        if type(limit) is not int or not 1 <= limit <= MAX_EVENTS_PER_SUBSCRIPTION:
            raise BrokerInvalidRequestError()
        if type(handle) is not str or not handle:
            raise BrokerInvalidRequestError()
        sub = self._get(subscription_id)
        with self._lock:
            if sub.terminated:
                raise BrokerSubscriptionTerminatedError()
            if sub.activation != authenticated_activation:
                raise BrokerDeniedError()
            batch = list(sub.queue)[:limit]
            staged: list[tuple[_Envelope, object]] = []
            for envelope in batch:
                current = self._current(envelope.topic)
                self._reauthorize_triple(
                    sub.invocation_id, authenticated_activation,
                    effect=envelope.subscribe_effect,
                    resource_scope=envelope.subscribe_resource_scope,
                    capability_grant=envelope.subscribe_grant,
                )
                self._reauthorize(
                    sub.invocation_id, authenticated_activation, current,
                    subscribe=True,
                )
                payload = json.loads(envelope.canonical)
                if current.validate_payload is not None:
                    try:
                        valid = bool(current.validate_payload(payload))
                    except BrokerDeniedError:
                        raise
                    except Exception:
                        raise BrokerDeniedError()
                    if not valid:
                        raise BrokerDeniedError()
                staged.append((envelope, payload))
            out: list[dict[str, object]] = []
            for envelope, payload in staged:
                sub.queue.popleft()
                sub.queued_bytes -= envelope.size
                sub.unacked.append(envelope)
                sub.unacked_bytes += envelope.size
                sub.delivered_seq = envelope.broker_seq
                out.append({
                    "sequence": envelope.broker_seq,
                    "topic": envelope.topic,
                    "payload": payload,
                    "classification": envelope.classification,
                })
            return out

    def drain(
        self, handle: str, authenticated_activation: ActivationIdentity, *,
        subscription_id: str,
    ) -> list[dict[str, object]]:
        return self.pull(
            handle, authenticated_activation, subscription_id=subscription_id,
            limit=MAX_EVENTS_PER_SUBSCRIPTION,
        )

    def ack(
        self, handle: str, authenticated_activation: ActivationIdentity, *,
        subscription_id: str, sequence: int,
    ) -> dict[str, int]:
        target = self._check_sequence(sequence)
        if type(handle) is not str or not handle:
            raise BrokerInvalidRequestError()
        if target == 0:
            raise BrokerInvalidRequestError()
        sub = self._get(subscription_id)
        with self._lock:
            if sub.terminated:
                raise BrokerSubscriptionTerminatedError()
            if sub.activation != authenticated_activation:
                raise BrokerDeniedError()
            if target > sub.delivered_seq:
                raise BrokerAckRangeError()
            if target <= sub.acked_seq:
                currents = [self._current(topic) for topic in sub.topics]
                for current in currents:
                    self._reauthorize(
                        sub.invocation_id, authenticated_activation, current, subscribe=True,
                    )
                for original in sub.descriptors.values():
                    self._reauthorize_triple(
                        sub.invocation_id, authenticated_activation,
                        effect=original.subscribe_effect,
                        resource_scope=original.subscribe_resource_scope,
                        capability_grant=original.subscribe_grant,
                    )
                return {"credit": 0}
            doomed = [e for e in sub.unacked if e.broker_seq <= target]
            if len(doomed) != target - sub.acked_seq:
                raise BrokerAckRangeError()
            currents = [self._current(topic) for topic in sub.topics]
            for current in currents:
                self._reauthorize(
                    sub.invocation_id, authenticated_activation, current, subscribe=True,
                )
            for envelope in doomed:
                current = self._current(envelope.topic)
                self._reauthorize_triple(
                    sub.invocation_id, authenticated_activation,
                    effect=envelope.subscribe_effect,
                    resource_scope=envelope.subscribe_resource_scope,
                    capability_grant=envelope.subscribe_grant,
                )
                self._reauthorize(
                    sub.invocation_id, authenticated_activation, current, subscribe=True,
                )
            while sub.unacked and sub.unacked[0].broker_seq <= target:
                released = sub.unacked.popleft()
                sub.unacked_bytes -= released.size
            credit = target - sub.acked_seq
            sub.acked_seq = target
            return {"credit": credit}

    def unsubscribe(
        self, handle: str, authenticated_activation: ActivationIdentity, *,
        subscription_id: str,
    ) -> dict[str, bool]:
        if type(handle) is not str or not handle:
            raise BrokerInvalidRequestError()
        sub = self._get(subscription_id)
        with self._lock:
            if sub.activation != authenticated_activation:
                raise BrokerDeniedError()
            current = self._current(sub.topics[0])
            self._reauthorize(
                sub.invocation_id, authenticated_activation, current, subscribe=True,
            )
            del self._subscriptions[subscription_id]
        return {"unsubscribed": True}
