"""Arithmetic and validation of the worker-health records.

These records carry no behavior, so everything here is a pure value check: the
ledger never mutates itself, every clock reading is passed in, and nothing
touches a process.
"""
import unittest

from model_deck.plugins.process_runtime.health import (
    MAX_RESTART_ATTEMPTS,
    WORKER_LOSS_CODES,
    RestartLedger,
    RestartPolicy,
    WorkerHealth,
    WorkerHealthState,
    WorkerLossCode,
    WorkerLossEvent,
    WorkerLossListener,
    backoff_for_attempt,
)


def make_policy(**overrides: object) -> RestartPolicy:
    fields: dict[str, object] = {
        "max_attempts": 3,
        "initial_backoff_s": 1.0,
        "multiplier": 2.0,
        "max_backoff_s": 8.0,
        "reset_after_healthy_s": 30.0,
    }
    fields.update(overrides)
    return RestartPolicy(**fields)  # type: ignore[arg-type]


class RestartPolicyValidationTests(unittest.TestCase):
    def test_default_shaped_policy_is_accepted(self) -> None:
        policy = make_policy()
        self.assertEqual(policy.max_attempts, 3)
        self.assertEqual(policy.multiplier, 2.0)

    def test_never_restart_policy_is_legal(self) -> None:
        self.assertEqual(make_policy(max_attempts=0).max_attempts, 0)

    def test_each_field_is_validated(self) -> None:
        rejected = [
            {"max_attempts": -1},
            {"max_attempts": MAX_RESTART_ATTEMPTS + 1},
            {"max_attempts": 1.0},
            {"max_attempts": True},
            {"initial_backoff_s": 0.0},
            {"initial_backoff_s": -1.0},
            {"initial_backoff_s": float("nan")},
            {"multiplier": 0.5},
            {"multiplier": float("inf")},
            {"max_backoff_s": 0.5},
            {"reset_after_healthy_s": -1.0},
        ]
        for overrides in rejected:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    make_policy(**overrides)

    def test_multiplier_of_one_is_a_flat_backoff(self) -> None:
        policy = make_policy(multiplier=1.0)
        self.assertEqual(backoff_for_attempt(policy, 1), 1.0)
        self.assertEqual(backoff_for_attempt(policy, 9), 1.0)


class BackoffTests(unittest.TestCase):
    def test_backoff_grows_exponentially_then_stops_at_the_cap(self) -> None:
        policy = make_policy()
        observed = [backoff_for_attempt(policy, n) for n in range(1, 7)]
        self.assertEqual(observed, [1.0, 2.0, 4.0, 8.0, 8.0, 8.0])

    def test_backoff_never_overflows_for_a_large_attempt_count(self) -> None:
        policy = make_policy(multiplier=10.0, max_backoff_s=60.0)
        self.assertEqual(backoff_for_attempt(policy, MAX_RESTART_ATTEMPTS), 60.0)

    def test_attempt_is_one_based(self) -> None:
        policy = make_policy()
        for attempt in (0, -1, 1.0):
            with self.subTest(attempt=attempt):
                with self.assertRaises(ValueError):
                    backoff_for_attempt(policy, attempt)  # type: ignore[arg-type]


class RestartLedgerTests(unittest.TestCase):
    def test_a_fresh_ledger_permits_an_immediate_attempt(self) -> None:
        ledger = RestartLedger()
        self.assertEqual(ledger.attempts, 0)
        self.assertIsNone(ledger.last_failure_code)
        self.assertFalse(ledger.gave_up)
        self.assertTrue(ledger.can_attempt(0.0))

    def test_recording_a_failure_leaves_the_original_untouched(self) -> None:
        policy = make_policy()
        original = RestartLedger()
        updated = original.record_failure(policy, 100.0, WorkerLossCode.MALFORMED_EOF)
        self.assertEqual(original.attempts, 0)
        self.assertIsNone(original.last_failure_code)
        self.assertEqual(updated.attempts, 1)
        self.assertEqual(updated.last_failure_code, WorkerLossCode.MALFORMED_EOF)

    def test_each_failure_schedules_the_next_attempt_further_out(self) -> None:
        policy = make_policy()
        ledger = RestartLedger()
        scheduled = []
        for failure in range(3):
            ledger = ledger.record_failure(policy, 100.0, WorkerLossCode.EXITED)
            scheduled.append(ledger.next_allowed_at_monotonic)
        self.assertEqual(scheduled, [101.0, 102.0, 104.0])
        self.assertEqual(ledger.attempts, 3)
        self.assertFalse(ledger.gave_up)

    def test_backoff_stops_growing_at_the_policy_cap(self) -> None:
        policy = make_policy(max_attempts=6)
        ledger = RestartLedger()
        scheduled = []
        for _ in range(6):
            ledger = ledger.record_failure(policy, 0.0, WorkerLossCode.TIMEOUT)
            scheduled.append(ledger.next_allowed_at_monotonic)
        self.assertEqual(scheduled, [1.0, 2.0, 4.0, 8.0, 8.0, 8.0])

    def test_attempts_are_bounded_by_the_policy(self) -> None:
        policy = make_policy(max_attempts=3)
        ledger = RestartLedger()
        for _ in range(3):
            ledger = ledger.record_failure(policy, 0.0, WorkerLossCode.KILLED)
        self.assertFalse(ledger.gave_up)
        self.assertTrue(ledger.can_attempt(1000.0))
        ledger = ledger.record_failure(policy, 0.0, WorkerLossCode.KILLED)
        self.assertTrue(ledger.gave_up)
        self.assertEqual(ledger.attempts, 4)
        self.assertFalse(ledger.can_attempt(1000.0))

    def test_a_never_restart_policy_gives_up_on_the_first_failure(self) -> None:
        policy = make_policy(max_attempts=0)
        ledger = RestartLedger().record_failure(policy, 0.0, WorkerLossCode.EXITED)
        self.assertTrue(ledger.gave_up)
        self.assertFalse(ledger.can_attempt(1e9))

    def test_gave_up_latches_against_further_failures(self) -> None:
        policy = make_policy(max_attempts=1)
        ledger = RestartLedger()
        for _ in range(2):
            ledger = ledger.record_failure(policy, 0.0, WorkerLossCode.UNRESPONSIVE)
        self.assertTrue(ledger.gave_up)
        for now in (5.0, 50.0, 500.0):
            repeated = ledger.record_failure(policy, now, WorkerLossCode.EXITED)
            self.assertIs(repeated, ledger)
        self.assertEqual(ledger.attempts, 2)
        self.assertEqual(ledger.last_failure_code, WorkerLossCode.UNRESPONSIVE)

    def test_can_attempt_waits_for_the_scheduled_moment(self) -> None:
        policy = make_policy()
        ledger = RestartLedger().record_failure(policy, 100.0, WorkerLossCode.TIMEOUT)
        self.assertEqual(ledger.next_allowed_at_monotonic, 101.0)
        self.assertFalse(ledger.can_attempt(100.9))
        self.assertTrue(ledger.can_attempt(101.0))
        self.assertTrue(ledger.can_attempt(200.0))

    def test_an_omitted_failure_code_keeps_the_recorded_one(self) -> None:
        policy = make_policy()
        ledger = RestartLedger().record_failure(policy, 0.0, WorkerLossCode.MALFORMED_EOF)
        ledger = ledger.record_failure(policy, 1.0)
        self.assertEqual(ledger.last_failure_code, WorkerLossCode.MALFORMED_EOF)
        self.assertEqual(ledger.attempts, 2)

    def test_a_sustained_healthy_run_clears_the_history(self) -> None:
        policy = make_policy(reset_after_healthy_s=30.0)
        ledger = RestartLedger().record_failure(policy, 100.0, WorkerLossCode.EXITED)
        self.assertEqual(ledger.next_allowed_at_monotonic, 101.0)
        still_counting = ledger.record_healthy(policy, 120.0)
        self.assertIs(still_counting, ledger)
        self.assertEqual(still_counting.attempts, 1)
        reset = ledger.record_healthy(policy, 131.0)
        self.assertEqual(reset.attempts, 0)
        self.assertIsNone(reset.last_failure_code)
        self.assertFalse(reset.gave_up)
        self.assertTrue(reset.can_attempt(131.0))

    def test_a_sustained_healthy_run_clears_the_gave_up_latch(self) -> None:
        policy = make_policy(max_attempts=1, reset_after_healthy_s=30.0)
        ledger = RestartLedger()
        for _ in range(2):
            ledger = ledger.record_failure(policy, 100.0, WorkerLossCode.KILLED)
        self.assertTrue(ledger.gave_up)
        self.assertIs(ledger.record_healthy(policy, 120.0), ledger)
        self.assertFalse(ledger.record_healthy(policy, 1000.0).gave_up)

    def test_a_clean_ledger_is_unchanged_by_a_healthy_report(self) -> None:
        policy = make_policy()
        clean = RestartLedger()
        self.assertIs(clean.record_healthy(policy, 5.0), clean)

    def test_ledger_fields_are_validated(self) -> None:
        rejected: list[dict[str, object]] = [
            {"attempts": -1},
            {"attempts": 1.0},
            {"next_allowed_at_monotonic": float("inf")},
            {"last_failure_code": "exited"},
            {"gave_up": 1},
        ]
        for overrides in rejected:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    RestartLedger(**overrides)  # type: ignore[arg-type]

    def test_ledger_methods_reject_a_non_policy_and_a_bad_clock(self) -> None:
        policy = make_policy()
        ledger = RestartLedger()
        with self.assertRaises(ValueError):
            ledger.record_failure(object(), 0.0)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ledger.record_healthy(object(), 0.0)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ledger.record_failure(policy, float("nan"))
        with self.assertRaises(ValueError):
            ledger.can_attempt(float("inf"))


class WorkerHealthRecordTests(unittest.TestCase):
    def test_a_starting_worker_has_no_heartbeat_yet(self) -> None:
        health = WorkerHealth(WorkerHealthState.STARTING, None, 0)
        self.assertEqual(health.state.value, "starting")
        self.assertIsNone(health.last_heartbeat_monotonic)

    def test_health_states_are_exactly_the_four_named_ones(self) -> None:
        self.assertEqual(
            [state.value for state in WorkerHealthState],
            ["starting", "healthy", "unresponsive", "dead"],
        )

    def test_health_fields_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            WorkerHealth("healthy", 1.0, 0)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            WorkerHealth(WorkerHealthState.HEALTHY, 1.0, -1)
        with self.assertRaises(ValueError):
            WorkerHealth(WorkerHealthState.HEALTHY, float("nan"), 0)


class WorkerLossEventTests(unittest.TestCase):
    def test_loss_codes_are_exactly_the_five_named_ones(self) -> None:
        self.assertEqual(
            [code.value for code in WorkerLossCode],
            ["malformed_eof", "timeout", "unresponsive", "exited", "killed"],
        )
        self.assertEqual(
            WORKER_LOSS_CODES,
            {"malformed_eof", "timeout", "unresponsive", "exited", "killed"},
        )

    def test_a_loss_before_activation_names_no_activation_and_no_exit_code(self) -> None:
        event = WorkerLossEvent(None, WorkerLossCode.MALFORMED_EOF, 12.5)
        self.assertIsNone(event.activation_id)
        self.assertIsNone(event.exit_code)
        self.assertEqual(event.failure_code, WorkerLossCode.MALFORMED_EOF)

    def test_a_known_code_string_is_accepted_and_normalized(self) -> None:
        event = WorkerLossEvent("act-1", "timeout", 1.0, 143)  # type: ignore[arg-type]
        self.assertIsInstance(event.failure_code, WorkerLossCode)
        self.assertEqual(event.failure_code, WorkerLossCode.TIMEOUT)
        self.assertEqual(event.exit_code, 143)

    def test_event_fields_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            WorkerLossEvent("", WorkerLossCode.EXITED, 1.0)
        with self.assertRaises(ValueError):
            WorkerLossEvent("act-1", "crashed", 1.0)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            WorkerLossEvent("act-1", WorkerLossCode.EXITED, float("inf"))
        with self.assertRaises(ValueError):
            WorkerLossEvent("act-1", WorkerLossCode.EXITED, 1.0, "1")  # type: ignore[arg-type]


class WorkerLossListenerTests(unittest.TestCase):
    def test_any_object_with_on_worker_lost_satisfies_the_protocol(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.events: list[WorkerLossEvent] = []

            def on_worker_lost(self, event: WorkerLossEvent) -> None:
                self.events.append(event)

        recorder = Recorder()
        self.assertIsInstance(recorder, WorkerLossListener)
        recorder.on_worker_lost(WorkerLossEvent(None, WorkerLossCode.KILLED, 1.0, -9))
        self.assertEqual(recorder.events[0].exit_code, -9)

    def test_an_object_without_the_method_does_not_satisfy_the_protocol(self) -> None:
        self.assertNotIsInstance(object(), WorkerLossListener)


if __name__ == "__main__":
    unittest.main()
