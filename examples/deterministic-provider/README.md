# Standalone deterministic provider

This example implements the frozen `provider.execution/v1` protocol in a real
Python subprocess, using only the standard library. Its manifest contributes
one custom provider with tools. It requires `python -I plugin.py` and refuses to
start if `model_deck` is importable. The hello capability
`fixture.isolated-imports` confirms this check ran in the child.

The registered model ID chooses a deterministic scenario: `text` emits text and
completes; `tool` requests the first advertised tool, accepts its result and
completes; `wait` stays active until confirmed cancellation; `cancel-unconfirmed`
accepts cancellation without confirming termination. Failure fixtures
`malformed`, `foreign-handle` and `crash` deliberately violate the protocol or
exit so the supervisor can prove interruption behavior. These are test modes,
not user-facing model offerings.

Every worker event starts at sequence zero with matching inner/outer sequences.
ACK requests return one credit. No remote services, model calls, credentials,
filesystem discovery or automatic retry/resume are used. Standard input/output
are its only work channels; activation and run identities remain in memory.
The fixture is not a production provider, credential broker or OS sandbox.

The integration suite creates a ZIP from this package, validates the manifest,
inspects and stages the archive, starts the extracted entrypoint with `-I`,
performs hello/activate, and supplies the public provider channel to
`ExternalProviderExecution`. SQLite registrations, connections and captured
routes drive real run use cases. No plugin engine internals are imported by
the child, and no installed app is involved.

From the Architecture `python/` directory, with isolated test dependencies:

```sh
PYTHONPATH=src python -B -m unittest tests.plugins.test_external_provider_integration
```

This proves a local external deterministic execution path. External credential
authorization and brokerage remain pending; success here is not proof of live
provider access, billing, signed installation or app/model-picker attachment.
