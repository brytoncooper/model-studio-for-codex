# External extension host

`ExternalExtensionHost` is composed by the outer bootstrap through
`HostDependencies`, keeping platform and storage adapters outside this layer.
It owns private SQLite lifecycle, catalog, authority, job, data, and
invocation-idempotency records plus an immutable digest-addressed artifact
store.

It inspects, installs, updates, enables, disables, removes, and invokes packed
Python extensions. Discovery resolves the lifecycle-selected artifact. Broker
methods and invocation authority derive only from selected `approved_scopes`;
manifest permissions alone never grant access. Updates retain only the
intersection of old approved and newly requested scopes. Disable/remove retain
data and artifact records.

Startup recovers pending lifecycle operations under the exclusive lease before
re-admitting settled enabled records. It first revokes any durable identity
from the prior engine epoch, so recovery can register a fresh non-serving
candidate without reviving an old token. `RESOLUTION_REQUIRED` remains pending
and non-serving. Pending invocations and jobs are never replayed automatically;
data conflicts require explicit resolution.

Focused verification:

```sh
PYTHONPATH=python:python/src /tmp/md-b18-venv/bin/python -B -m pytest -q \
  python/tests/plugins/test_external_extension_host.py \
  python/tests/engine/test_notebook_update_acceptance.py \
  python/tests/engine/test_notebook_update_interruption.py
```

The current boundary supports local Python process entrypoints. It does not
provide a permission-renewal/presentation UI, automatic replay of interrupted
plugin work, or non-process runtimes.
