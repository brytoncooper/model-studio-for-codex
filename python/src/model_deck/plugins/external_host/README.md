# External extension host

`ExternalExtensionHost` is composed only by the outer application bootstrap.
Its constructor requires a `HostDependencies` bundle containing the lifecycle,
job, data-store, lock, and lease factories. This keeps concrete platform and
storage adapters outside the plugin-runtime layer; callers should use
`model_deck.bootstrap.build_engine_server` or provide the same factories from
their own composition root.

This package is the smallest composition root for generic packed process extensions. It owns a private state root containing the SQLite lifecycle, catalog, authority, job, versioned-data, and invocation-idempotency records. The immutable artifact store may use a distinct absolute `artifact_root`; by default it is `artifacts/` inside the state root.

`ExternalExtensionHost` installs validated archives, enables and disables them through `ExtensionLifecycleService`, exposes only enabled operation/panel contributions, and invokes through the lifecycle's `ServingActivation`. Python artifacts launch from their staged digest directory with `-I -B`; broker methods come only from approved manifest permissions.

Data is retained on disable. Reconstructing a host on the same root re-admits enabled records into a fresh process/activation identity. A completed invocation key replays its stored output; changed input conflicts, and a pending key fails closed without executing again.

The current boundary supports Python process entrypoints and `storage.own`. It does not recover pending lifecycle or invocation work automatically, update an installed extension to a new artifact, or implement non-process runtimes.
