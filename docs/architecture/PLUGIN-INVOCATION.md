# External plugin invocation contract

An external operation is invoked with `plugin.v1.invoke`. Its parameters carry
the registered `operation_id`, the validated operation `input`, and a required
`broker_context`. The supervisor creates this context for one admitted call.
The plugin may copy only its opaque `invocation_handle` into nested
`plugin.v1.broker.*` request parameters.

The handle is a bearer reference to authority retained by the supervisor. It is
not authority by itself and it is not minted by the plugin. For every broker
request, the supervisor resolves the handle through its trusted context store,
reauthorizes it against current origin, operation, activation, expiry, scopes,
effects, grants, and revocation state, and independently derives the activation
identity from the private plugin channel. No identity, context, role, principal,
scope, effect, grant, or generation supplied by plugin JSON or other user input
is trusted as authority.

The `broker_context` fields let the worker correlate its call and use its opaque
handle. They do not replace transport authentication. A handle stolen from or
replayed on another activation must be denied. Worker-created invocation labels
or prefixes are bookkeeping only and cannot authorize a broker call.

`engine.v1.ui.panel.get` returns `{ "panel": <ui.panel.v1 tree> }`. The engine
resolves the requested active contribution and validates the returned tree
before a client renders it. Contribution discovery remains the responsibility
of `engine.v1.ui.contributions.list`; `ui.panel.get` returns the renderable panel
snapshot rather than another descriptor or schema pointer.

These JSON Schemas define wire shapes only. They do not implement generic
operation routing, process supervision, bidirectional broker request handling,
authority issuance, panel resolution, semantic panel validation, or rendering.
Those integrations must preserve the authentication and validation rules above
before external operations or panels are advertised as available.
