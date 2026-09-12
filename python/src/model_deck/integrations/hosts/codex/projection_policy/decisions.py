"""Pure decision planner for B09 host projection reconciliation.

This module is deliberately filesystem-free and side-effect-free. It answers a
single question: given the observed host state (content hashes and whether the
managed marker matches) and the engine's owned/desired state, what should the
caller do next?

Nothing here reads, writes, hashes, or imports concrete storage. The caller
performs the actual mutation and must re-check the precondition atomically at
the mutation boundary -- the ``expected_sha256`` for REPLACE/DELETE, or the path
still being absent for CREATE; see ``README.md`` for the full caller contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal

# Strict lowercase 64-character hex, i.e. a bare ``hashlib.sha256().hexdigest()``.
_SHA256_LOWERCASE_HEX = re.compile(r"[0-9a-f]{64}")

_OPERATIONS = ("upsert", "remove")


class ProjectionAction(Enum):
    """The action the caller should take at the mutation boundary."""

    CREATE = "create"
    REPLACE = "replace"
    DELETE = "delete"
    NOOP = "noop"
    CONFLICT = "conflict"


class ProjectionReason:
    """Stable machine-readable reason codes attached to a decision.

    These are plain strings so they can be persisted and compared without an
    import-time dependency on this module. They are part of the contract.
    """

    CREATE_ABSENT = "create_absent"
    NOOP_ALREADY_DESIRED = "noop_already_desired"
    NOOP_REMOVE_ABSENT = "noop_remove_absent"
    REPLACE_OWNED_STALE = "replace_owned_stale"
    DELETE_OWNED = "delete_owned"
    CONFLICT_UNMANAGED_EXISTING = "conflict_unmanaged_existing"
    CONFLICT_MISSING_OWNERSHIP = "conflict_missing_ownership"
    CONFLICT_FOREIGN_EDIT = "conflict_foreign_edit"


@dataclass(frozen=True, slots=True)
class ProjectionDecision:
    """A deterministic plan for one projection target.

    ``expected_sha256`` is the precondition the caller must verify atomically
    before mutating, and it is the current content hash that was valid when this
    plan was produced. For ``CREATE`` it is ``None``, which means "the path must
    still be absent" and must be checked atomically at the mutation boundary
    like any other precondition. For NOOP and CONFLICT, which perform no write,
    it is also ``None`` and there is no precondition to check.
    """

    action: ProjectionAction
    expected_sha256: str | None
    reason: str


def _validate_sha256(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256_LOWERCASE_HEX.fullmatch(value) is None:
        raise ValueError(
            f"{label} must be None or a lowercase 64-character hex sha256 digest"
        )
    return value


def _validate_operation(operation: object) -> Literal["upsert", "remove"]:
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise ValueError("operation must be exactly 'upsert' or 'remove'")
    return operation  # type: ignore[return-value]


def _validate_managed_marker_matches(value: object) -> bool:
    # ``isinstance(value, bool)`` is strict: it rejects ints such as 1 and 0.
    if not isinstance(value, bool):
        raise ValueError("managed_marker_matches must be a bool")
    return value


def plan_projection(
    *,
    operation: Literal["upsert", "remove"],
    current_sha256: str | None,
    previous_owned_sha256: str | None,
    desired_sha256: str | None,
    managed_marker_matches: bool,
) -> ProjectionDecision:
    """Return the deterministic projection decision for one target.

    ``current_sha256`` is the observed file content hash (``None`` if absent).
    ``previous_owned_sha256`` is the hash the engine last recorded as owned
    (``None`` means no known previous ownership, i.e. absent or unknown).
    ``desired_sha256`` is the content the engine wants for ``upsert`` and must be
    ``None`` for ``remove``. ``managed_marker_matches`` reports whether the file
    carries the engine's managed marker.

    The planner never grants ownership of a foreign file, and existing files are
    only touched when both the managed marker matches and prior ownership is
    evidenced.
    """

    resolved_operation = _validate_operation(operation)
    current = _validate_sha256(current_sha256, label="current_sha256")
    previous = _validate_sha256(previous_owned_sha256, label="previous_owned_sha256")
    desired = _validate_sha256(desired_sha256, label="desired_sha256")
    marker_matches = _validate_managed_marker_matches(managed_marker_matches)

    if resolved_operation == "upsert":
        if desired is None:
            raise ValueError("operation 'upsert' requires desired_sha256")
        return _plan_upsert(
            current=current,
            previous=previous,
            desired=desired,
            marker_matches=marker_matches,
        )
    if desired is not None:
        raise ValueError("operation 'remove' requires desired_sha256 to be None")
    return _plan_remove(current=current, previous=previous, marker_matches=marker_matches)


def _plan_upsert(
    *,
    current: str | None,
    previous: str | None,
    desired: str,
    marker_matches: bool,
) -> ProjectionDecision:
    if current is None:
        return ProjectionDecision(
            action=ProjectionAction.CREATE,
            expected_sha256=None,
            reason=ProjectionReason.CREATE_ABSENT,
        )

    # Existing file: ownership requires a matching managed marker and recorded
    # prior ownership. This is checked before any equality shortcut so that a
    # foreign file whose bytes happen to equal ``desired`` still conflicts.
    if not marker_matches:
        return ProjectionDecision(
            action=ProjectionAction.CONFLICT,
            expected_sha256=None,
            reason=ProjectionReason.CONFLICT_UNMANAGED_EXISTING,
        )
    if previous is None:
        return ProjectionDecision(
            action=ProjectionAction.CONFLICT,
            expected_sha256=None,
            reason=ProjectionReason.CONFLICT_MISSING_OWNERSHIP,
        )

    if current == desired:
        # Covers the steady state (current == previous == desired) and crash
        # recovery where a write landed but the ownership receipt was not
        # recorded (current != previous, current == desired).
        return ProjectionDecision(
            action=ProjectionAction.NOOP,
            expected_sha256=None,
            reason=ProjectionReason.NOOP_ALREADY_DESIRED,
        )
    if current == previous:
        return ProjectionDecision(
            action=ProjectionAction.REPLACE,
            expected_sha256=current,
            reason=ProjectionReason.REPLACE_OWNED_STALE,
        )
    return ProjectionDecision(
        action=ProjectionAction.CONFLICT,
        expected_sha256=None,
        reason=ProjectionReason.CONFLICT_FOREIGN_EDIT,
    )


def _plan_remove(
    *,
    current: str | None,
    previous: str | None,
    marker_matches: bool,
) -> ProjectionDecision:
    if current is None:
        return ProjectionDecision(
            action=ProjectionAction.NOOP,
            expected_sha256=None,
            reason=ProjectionReason.NOOP_REMOVE_ABSENT,
        )
    if not marker_matches:
        return ProjectionDecision(
            action=ProjectionAction.CONFLICT,
            expected_sha256=None,
            reason=ProjectionReason.CONFLICT_UNMANAGED_EXISTING,
        )
    if previous is None:
        return ProjectionDecision(
            action=ProjectionAction.CONFLICT,
            expected_sha256=None,
            reason=ProjectionReason.CONFLICT_MISSING_OWNERSHIP,
        )
    if current != previous:
        return ProjectionDecision(
            action=ProjectionAction.CONFLICT,
            expected_sha256=None,
            reason=ProjectionReason.CONFLICT_FOREIGN_EDIT,
        )
    return ProjectionDecision(
        action=ProjectionAction.DELETE,
        expected_sha256=current,
        reason=ProjectionReason.DELETE_OWNED,
    )
