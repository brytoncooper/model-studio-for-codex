# Extension engine lease

[`extension_lease.py`](extension_lease.py) adapts the process-wide macOS
[`FileInstanceLock`](instance_lock.py) to the extension lifecycle service's
`HeldExclusiveEngineLease` boundary. It proves that lifecycle work is using the
explicitly configured repository while this process still owns the engine lock.

## Construction and ownership

Construct `ExtensionEngineLease(instance_lock, repository)` after the process
acquires its `FileInstanceLock`, then share it with the lifecycle services for
that repository. Repository binding uses object identity. The adapter never
discovers a database path or accepts a second repository based on a matching
filename.

The actual `FileInstanceLock` object identifies a shared, synchronized set of
executing operation IDs. Entering `execution_owner(repository, operation_id)`
rejects an operation already running through another service wrapper, including
through a separate `ExtensionEngineLease` constructed over that same lock
object. Different operations may execute concurrently. Ownership is removed on
normal return and on exceptions.

## Lock and process invariants

`FileInstanceLock.assert_held()` is the public held-state check. The extension
adapter does not inspect its file handle. `assert_held_for` and context entry fail
when the lock was never acquired, has been released, or the repository object is
not the one bound at construction.

Both lock and extension lease record the creating process. A copy inherited by
`fork` is rejected. Releasing an inherited `FileInstanceLock` closes only the
child's descriptor and does not unlock the parent's lease. A fresh child lock
still competes through the operating system's `flock` behavior.

Repeated `FileInstanceLock.acquire` by its owning process is idempotent and keeps
the original descriptor. Repeated release is safe. A failed competing acquire
closes only its temporary descriptor.

## Tests and limitations

Run the focused tests from `python/`:

```sh
PYTHONPATH=src /tmp/md-b18-venv/bin/python -m unittest \
  tests.engine.test_extension_engine_lease -v
```

The tests use temporary lock and SQLite paths. They cover acquire/release,
double-acquire descriptor safety, explicit repository binding, released-lock
rejection, concurrent operation ownership across one or multiple lease wrappers,
fork rejection, and a real child process competing for the parent lock.

This adapter does not acquire the startup lock, choose repository paths, recover
lifecycle operations, or coordinate different `FileInstanceLock` objects. Even
when two lock objects name the same path, composition must use the one acquired
lock object for all lease wrappers and keep it held for the entire engine
lifetime.
