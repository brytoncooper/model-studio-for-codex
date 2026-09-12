import unittest
from dataclasses import FrozenInstanceError

from model_deck.integrations.hosts.codex.projection_policy import (
    ProjectionAction,
    ProjectionDecision,
    ProjectionReason,
    plan_projection,
)

# Bare lowercase 64-character hex sha256-shaped digests for branch coverage.
CURRENT = "a" * 64
PREVIOUS = "b" * 64
DESIRED = "c" * 64
OTHER = "d" * 64


def _plan_upsert(
    *,
    current=None,
    previous=None,
    desired=DESIRED,
    marker=True,
):
    return plan_projection(
        operation="upsert",
        current_sha256=current,
        previous_owned_sha256=previous,
        desired_sha256=desired,
        managed_marker_matches=marker,
    )


def _plan_remove(*, current=None, previous=None, marker=True):
    return plan_projection(
        operation="remove",
        current_sha256=current,
        previous_owned_sha256=previous,
        desired_sha256=None,
        managed_marker_matches=marker,
    )


def _expected(action, expected_sha256, reason):
    return ProjectionDecision(
        action=action, expected_sha256=expected_sha256, reason=reason
    )


class PlanProjectionUpsertTests(unittest.TestCase):
    """Table-driven coverage of every upsert branch."""

    def test_upsert_branches(self):
        cases = (
            # name, kwargs, expected decision
            (
                "absent target creates with no precondition",
                {"current": None},
                _expected(
                    ProjectionAction.CREATE, None, ProjectionReason.CREATE_ABSENT
                ),
            ),
            (
                "owned steady state is a noop",
                {"current": DESIRED, "previous": DESIRED},
                _expected(
                    ProjectionAction.NOOP, None, ProjectionReason.NOOP_ALREADY_DESIRED
                ),
            ),
            (
                "owned stale target is replaced with current precondition",
                {"current": CURRENT, "previous": CURRENT},
                _expected(
                    ProjectionAction.REPLACE, CURRENT, ProjectionReason.REPLACE_OWNED_STALE
                ),
            ),
            (
                "crash after write before receipt is a noop",
                {"current": DESIRED, "previous": PREVIOUS},
                _expected(
                    ProjectionAction.NOOP, None, ProjectionReason.NOOP_ALREADY_DESIRED
                ),
            ),
            (
                "foreign edit of owned file conflicts",
                {"current": OTHER, "previous": PREVIOUS},
                _expected(
                    ProjectionAction.CONFLICT, None, ProjectionReason.CONFLICT_FOREIGN_EDIT
                ),
            ),
            (
                "existing without managed marker conflicts",
                {"current": CURRENT, "previous": CURRENT, "marker": False},
                _expected(
                    ProjectionAction.CONFLICT,
                    None,
                    ProjectionReason.CONFLICT_UNMANAGED_EXISTING,
                ),
            ),
            (
                "existing with marker but no ownership evidence conflicts",
                {"current": CURRENT, "previous": None},
                _expected(
                    ProjectionAction.CONFLICT,
                    None,
                    ProjectionReason.CONFLICT_MISSING_OWNERSHIP,
                ),
            ),
        )
        for name, kwargs, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(_plan_upsert(**kwargs), expected)

    def test_upsert_all_actions_reachable(self):
        actions = {
            _plan_upsert(current=None).action,
            _plan_upsert(current=CURRENT, previous=CURRENT).action,
            _plan_upsert(current=DESIRED, previous=DESIRED).action,
            _plan_upsert(current=CURRENT, previous=PREVIOUS).action,
            _plan_upsert(current=CURRENT, previous=CURRENT, marker=False).action,
        }
        self.assertEqual(
            actions,
            {
                ProjectionAction.CREATE,
                ProjectionAction.REPLACE,
                ProjectionAction.NOOP,
                ProjectionAction.CONFLICT,
            },
        )


class PlanProjectionRemoveTests(unittest.TestCase):
    """Table-driven coverage of every remove branch."""

    def test_remove_branches(self):
        cases = (
            (
                "absent remove target is a noop",
                {"current": None},
                _expected(
                    ProjectionAction.NOOP, None, ProjectionReason.NOOP_REMOVE_ABSENT
                ),
            ),
            (
                "owned unchanged target is deleted with current precondition",
                {"current": CURRENT, "previous": CURRENT},
                _expected(ProjectionAction.DELETE, CURRENT, ProjectionReason.DELETE_OWNED),
            ),
            (
                "remove without managed marker conflicts",
                {"current": CURRENT, "previous": CURRENT, "marker": False},
                _expected(
                    ProjectionAction.CONFLICT,
                    None,
                    ProjectionReason.CONFLICT_UNMANAGED_EXISTING,
                ),
            ),
            (
                "remove with marker but no ownership evidence conflicts",
                {"current": CURRENT, "previous": None},
                _expected(
                    ProjectionAction.CONFLICT,
                    None,
                    ProjectionReason.CONFLICT_MISSING_OWNERSHIP,
                ),
            ),
            (
                "remove of edited owned file conflicts",
                {"current": CURRENT, "previous": PREVIOUS},
                _expected(
                    ProjectionAction.CONFLICT, None, ProjectionReason.CONFLICT_FOREIGN_EDIT
                ),
            ),
        )
        for name, kwargs, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(_plan_remove(**kwargs), expected)

    def test_remove_never_deletes_unknown_ownership(self):
        decision = _plan_remove(current=CURRENT, previous=None, marker=True)
        self.assertIs(decision.action, ProjectionAction.CONFLICT)
        self.assertIsNone(decision.expected_sha256)


class ForeignIdenticalBytesTests(unittest.TestCase):
    """Identical bytes must not imply ownership without marker + receipt."""

    def test_foreign_identical_bytes_upsert_conflicts(self):
        decision = _plan_upsert(current=DESIRED, previous=None, marker=False)
        self.assertIs(decision.action, ProjectionAction.CONFLICT)
        self.assertEqual(decision.reason, ProjectionReason.CONFLICT_UNMANAGED_EXISTING)
        self.assertIsNone(decision.expected_sha256)

    def test_marker_only_identical_bytes_upsert_conflicts(self):
        decision = _plan_upsert(current=DESIRED, previous=None, marker=True)
        self.assertIs(decision.action, ProjectionAction.CONFLICT)
        self.assertEqual(decision.reason, ProjectionReason.CONFLICT_MISSING_OWNERSHIP)

    def test_foreign_identical_bytes_remove_conflicts(self):
        decision = _plan_remove(current=CURRENT, previous=None, marker=False)
        self.assertIs(decision.action, ProjectionAction.CONFLICT)
        self.assertIsNone(decision.expected_sha256)

    def test_noop_is_not_reachable_without_ownership(self):
        for marker in (True, False):
            for previous in (None, PREVIOUS):
                # previous != current and no receipt match -> never NOOP.
                decision = _plan_upsert(
                    current=DESIRED, previous=previous, marker=marker
                )
                if previous is None or not marker:
                    self.assertIs(decision.action, ProjectionAction.CONFLICT)


class IdempotentRetryTests(unittest.TestCase):
    """Repeated planning over identical inputs is stable and convergent."""

    def test_repeated_upsert_plan_is_identical(self):
        first = _plan_upsert(current=CURRENT, previous=PREVIOUS)
        second = _plan_upsert(current=CURRENT, previous=PREVIOUS)
        self.assertEqual(first, second)
        self.assertIs(first.action, ProjectionAction.CONFLICT)

    def test_converged_upsert_replans_to_noop(self):
        before = _plan_upsert(current=CURRENT, previous=CURRENT)
        self.assertIs(before.action, ProjectionAction.REPLACE)
        # Simulate the apply having landed: current is now the desired bytes and
        # the receipt was recorded.
        after = _plan_upsert(current=DESIRED, previous=DESIRED)
        self.assertIs(after.action, ProjectionAction.NOOP)

    def test_retry_after_crash_replans_to_noop(self):
        # First attempt: file still stale.
        first = _plan_upsert(current=PREVIOUS, previous=PREVIOUS)
        self.assertIs(first.action, ProjectionAction.REPLACE)
        # Write landed but receipt did not: retry converges to NOOP.
        retry = _plan_upsert(current=DESIRED, previous=PREVIOUS)
        self.assertIs(retry.action, ProjectionAction.NOOP)

    def test_repeated_remove_plan_is_identical(self):
        first = _plan_remove(current=CURRENT, previous=CURRENT)
        second = _plan_remove(current=CURRENT, previous=CURRENT)
        self.assertEqual(first, second)
        self.assertIs(first.action, ProjectionAction.DELETE)

    def test_remove_after_delete_replans_to_noop(self):
        during = _plan_remove(current=CURRENT, previous=CURRENT)
        self.assertIs(during.action, ProjectionAction.DELETE)
        # Simulate the delete having landed.
        after = _plan_remove(current=None, previous=CURRENT)
        self.assertIs(after.action, ProjectionAction.NOOP)
        self.assertEqual(after.reason, ProjectionReason.NOOP_REMOVE_ABSENT)


class EditConflictTests(unittest.TestCase):
    """A hash drift from the recorded receipt must surface as CONFLICT."""

    def test_upsert_edit_conflict(self):
        decision = _plan_upsert(current=OTHER, previous=PREVIOUS)
        self.assertIs(decision.action, ProjectionAction.CONFLICT)
        self.assertEqual(decision.reason, ProjectionReason.CONFLICT_FOREIGN_EDIT)

    def test_remove_edit_conflict(self):
        decision = _plan_remove(current=OTHER, previous=PREVIOUS)
        self.assertIs(decision.action, ProjectionAction.CONFLICT)
        self.assertEqual(decision.reason, ProjectionReason.CONFLICT_FOREIGN_EDIT)


class InputValidationTests(unittest.TestCase):
    """Malformed inputs raise ValueError and never produce a decision."""

    def test_operation_must_be_exact_literal(self):
        for operation in ("UPSERT", "Upsert", "create", "delete", "", " upsert", None, 1, True):
            with self.subTest(operation=operation):
                with self.assertRaises(ValueError):
                    plan_projection(
                        operation=operation,
                        current_sha256=None,
                        previous_owned_sha256=None,
                        desired_sha256=DESIRED,
                        managed_marker_matches=True,
                    )

    def test_upsert_requires_desired_hash(self):
        with self.assertRaises(ValueError):
            _plan_upsert(current=None, desired=None)

    def test_remove_requires_desired_hash_none(self):
        with self.assertRaises(ValueError):
            plan_projection(
                operation="remove",
                current_sha256=None,
                previous_owned_sha256=None,
                desired_sha256=DESIRED,
                managed_marker_matches=True,
            )

    def test_managed_marker_must_be_strict_bool(self):
        for marker in (1, 0, "true", "", None):
            with self.subTest(marker=marker):
                with self.assertRaises(ValueError):
                    plan_projection(
                        operation="upsert",
                        current_sha256=None,
                        previous_owned_sha256=None,
                        desired_sha256=DESIRED,
                        managed_marker_matches=marker,
                    )

    def test_malformed_hashes_raise(self):
        malformed = (
            "a" * 63,
            "a" * 65,
            "A" * 64,
            "g" * 64,
            "",
            "not-a-hash",
            "a" * 63 + "Z",
            "0x" + "a" * 62,
        )
        for value in malformed:
            for field in ("current_sha256", "previous_owned_sha256", "desired_sha256"):
                with self.subTest(value=value, field=field):
                    kwargs = {
                        "operation": "upsert",
                        "current_sha256": None,
                        "previous_owned_sha256": None,
                        "desired_sha256": DESIRED,
                        "managed_marker_matches": True,
                    }
                    kwargs[field] = value
                    with self.assertRaises(ValueError):
                        plan_projection(**kwargs)

    def test_non_string_hash_raises(self):
        for value in (b"a" * 64, 123, 1.0, True, ["a" * 64]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _plan_upsert(current=value)

    def test_none_hashes_are_accepted_where_allowed(self):
        decision = _plan_upsert(current=None, previous=None, desired=DESIRED)
        self.assertIs(decision.action, ProjectionAction.CREATE)
        self.assertIsNone(decision.expected_sha256)


class DecisionShapeTests(unittest.TestCase):
    """The decision value is a frozen, well-formed plan."""

    def test_decision_is_frozen(self):
        decision = _plan_upsert(current=None)
        with self.assertRaises(FrozenInstanceError):
            decision.action = ProjectionAction.CONFLICT  # type: ignore[misc]

    def test_expected_sha256_only_set_for_write_preconditions(self):
        for decision in (
            _plan_upsert(current=None),
            _plan_upsert(current=DESIRED, previous=DESIRED),
            _plan_upsert(current=CURRENT, previous=PREVIOUS),
            _plan_remove(current=None),
        ):
            with self.subTest(reason=decision.reason):
                self.assertIsNone(decision.expected_sha256)

    def test_preconditions_echo_current_hash(self):
        replace = _plan_upsert(current=CURRENT, previous=CURRENT)
        self.assertEqual(replace.expected_sha256, CURRENT)
        delete = _plan_remove(current=CURRENT, previous=CURRENT)
        self.assertEqual(delete.expected_sha256, CURRENT)

    def test_reason_is_plain_string(self):
        decision = _plan_upsert(current=None)
        self.assertIsInstance(decision.reason, str)
        self.assertEqual(decision.reason, "create_absent")

    def test_action_enum_members_complete(self):
        self.assertEqual(
            {action.name for action in ProjectionAction},
            {"CREATE", "REPLACE", "DELETE", "NOOP", "CONFLICT"},
        )


if __name__ == "__main__":
    unittest.main()
