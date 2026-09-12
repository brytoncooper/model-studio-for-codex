# ModelDeckPlatform

Window reservation, companion geometry, and Accessibility host tracking without UI chrome.

## Ownership

B04.geometry owns `Geometry/` and `ModelDeckPlatformTests/Geometry/`.

## Public surface

- `CompanionGeometry` — coordinate conversion and host visibility gating.
- `WindowReservation`, `TrackingGeneration`, `WindowReservationRecovery`, `HostWindowTracker` — sidebar width reservation and cleanup.

## Pitfalls

- Links ApplicationServices; do not import this target from Presentation-only modules.
- `HostWindowTracker` performs AX writes only after opt-in and trust checks in the app delegate.
