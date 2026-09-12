# Explicit host settings composition

`build_engine_server` accepts optional `host_settings_document` and
`host_settings_caller` arguments. Supply both or neither. The document implements
the public `SettingsDocumentPort`; the caller is a trusted `CallerContext` with a
nonempty principal and frozen `hosts.settings.read` and `hosts.settings.write`
grants. Composition designates that principal as the local operator and passes
the same caller object to dispatch. Request parameters cannot provide or replace
this authority. Every socket connection must still authenticate normally.

The bootstrap creates `HostSettingsService` with SQLite preview and save receipt
stores sharing `state_root/engine/host-settings.sqlite3`. Root validation runs
before this composition. Preview IDs use random UUID4 values. The ledger is
created lazily on use, independently of the application-state feature flag.
Restarting with the same isolated state root retains preview admissions and
exact settled save receipts, so replay does not write the host file again.
Unsettled receipts retain the existing ledger's uncertain-outcome behavior.

No settings are enabled by default. The bootstrap does not discover host
configuration paths or import the Codex file adapter for this feature. A caller
can explicitly construct `CodexSettingsFile` with fixture/configuration and
backup paths plus a context/specification provider, then inject it through the
generic port. Host-specific parsing, protection, locking, backups and atomic
publication remain in that adapter. Live app wiring and qualification are
separate work; this composition adds no installation or cutover behavior.

Focused verification from `python/`:

```sh
PYTHONPATH=src python -B -m unittest tests.engine.test_host_settings_bootstrap
```

Tests use authenticated real Unix sockets, temporary Codex TOML files, real
SQLite ledgers and isolated engine roots. They cover read/preview/save, backup
bytes, settled replay across restart without another file replacement, preview
admission across restart, stale-content conflicts, authentication, immutable
caller authority and unchanged default composition. File-adapter and generic
settings-service tests own their deeper failure matrices.
