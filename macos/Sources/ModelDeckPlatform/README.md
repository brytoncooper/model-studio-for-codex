# ModelDeckPlatform

Window reservation, companion geometry, and Accessibility host tracking without UI chrome.

## Ownership

B04.geometry owns `Geometry/` and `ModelDeckPlatformTests/Geometry/`.

## Public surface

- `CompanionGeometry` — coordinate conversion and host visibility gating.
- `WindowReservation`, `TrackingGeneration`, `WindowReservationRecovery`, `HostWindowTracker` — sidebar width reservation and cleanup.
- `UnixSocketEngineTransport` — `EngineTransport` over a `SOCK_STREAM` unix socket (`Transport/`).

## Pitfalls

- Links ApplicationServices; do not import this target from Presentation-only modules.
- `HostWindowTracker` performs AX writes only after opt-in and trust checks in the app delegate.
- `UnixSocketEngineTransport.close()` runs a two-phase drain: it clears `socketFD`, bumps the
  connection epoch and shuts the socket down, then waits for every in-flight read/write to
  return before closing the descriptor. `open()` blocks while a drain is in progress so a
  reconnect never installs a new socket over a descriptor another thread is still closing.
- The `testingOn…` hooks (`testingOnInFlightIOEntered`, `testingOnWaitingForCloseDrain`,
  `testingOnCloseDrainStarted`) exist only so tests can pin this ordering deterministically
  rather than racing on thread start-up; `testingOnCloseDrainStarted` runs unlocked with the
  drain already marked, so a test that parks there holds the drain open. Production code must
  leave them nil.
- Still not done: the transport is blocking and single-connection. It does not reconnect on
  its own, has no read/write timeouts, and callers must drive `open()`/`close()` themselves.
