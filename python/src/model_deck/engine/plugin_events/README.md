# Plugin event broker (B19)

In-memory publish/subscribe broker for plugin activations. Wire shapes mirror
`contracts/plugin.v1/broker` `events.publish`, `events.subscribe`, `events.ack`,
and `events.unsubscribe`; the frozen contract JSON is authoritative on the wire.

## Contract

- Topics are exact registered descriptor event IDs; no wildcards. Unknown topics
  are rejected before any authorization or admission.
- The supervisor injects the `EventDescriptorResolver`. Each descriptor supplies
  the owner plugin, payload validator, publish/subscribe effect names,
  resource scopes, capability grant names, and metadata/content classification.
  The broker invents no default grants.
- Every publish and subscribe is authorized through settled `PluginAuthority`.
  Only the owning plugin may publish a topic.
- Subscriptions store the captured invocation ID and authenticated activation.
  Delivery (`pull`/`drain`), `ack`, and `unsubscribe` reauthorize, so
  revocation or expiry denies before delivery and before acknowledgement.
  `ack` preflights original plus current subscribe constraints for every
  envelope it would release before mutating queue or credit state, so a
  denied `ack` releases no credit; repeat `ack` still reauthorizes.
- Authenticated activations arrive via the supervisor private channel (method
  arguments), never from request documents.
- Subscription sequence numbers are broker-generated and monotonic across topics
  within a subscription, starting at 1. Producer `(activation, topic, sequence)`
  pairs deduplicate: same sequence with the same canonical payload replays as
  accepted without refanout; same sequence with a different payload conflicts.
- Every payload, including `null`, validates against the descriptor schema
  before admission; `null` is valid only where the schema allows it. Payloads
  use strict JSON: `NaN`/`Infinity`, tuples, non-string object keys, and
  non-JSON values are rejected before schema validation.
- Delivery (`pull`/`drain`) and `ack` re-resolve the current topic descriptor
  and reauthorize against it, including repeat acks before any zero-credit
  return; a removed or narrowed descriptor denies.
- `pull`/`drain` preflight the whole returned batch (original plus current
  subscribe authorization and current schema) before moving any envelope, so
  a denied batch fails atomically with queues untouched.
- Each queued envelope keeps the subscribe grant triple and classification
  captured at admission. Delivery requires both the original and the current
  grant, so queued content is never downgraded to metadata after the
  original content grant is revoked, and delivery always reports the
  original classification.
- Strict payload checks reject object cycles and nesting beyond 100 levels
  instead of recursing without bound; shared (non-cyclic) references stay
  valid.
- `ack` is cumulative and cannot acknowledge an undelivered sequence;
  re-acking an already-acked sequence returns zero credit.
- Supervisor pulls via `pull`/`drain`; there are no sockets or callbacks and no
  locks are held across callbacks.
- Error messages are fixed safe strings; they never echo payloads or identities.

## Limits

- At most 32 topics per subscription.
- At most 256 outstanding events and 1 MiB of canonical payload bytes per
  subscription, counting both queued and delivered-but-unacked events.
  `pull` moves events from queued to delivered; cumulative `ack` releases each
  event exactly once. A subscription that has not acked cannot pull past the
  window: a publish beyond it terminates that subscription independently;
  other subscriptions are unaffected.
- Producer deduplication is bounded: per (activation, topic) the broker keeps
  only a high-water producer sequence plus the SHA256 digest of the latest
  canonical payload. An exact latest replay is accepted without refanout;
  any older or divergent sequence conflicts and never refanouts.
- In-memory only: no persistence, no cross-restart delivery, no network.

## Extensions

- Add new topics by registering descriptors in the supervisor resolver.
- Payload validation is any callable; JSON Schema checking lives with the
  supervisor, not the broker.
