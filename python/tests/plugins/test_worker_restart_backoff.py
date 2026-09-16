"""Bounded restarts: backoff, giving up visibly, and the ways back to healthy.

These run the same real crashable child as `test_worker_loss_supervision`, with
a restart policy tuned down so the backoff is observable without making the
suite slow.
"""
from __future__ import annotations

import time
import unittest

from model_deck.plugins.activation_lifecycle.restart import (
    DEFAULT_RESTART_POLICY,
    SUPERVISION_DEGRADED,
    SUPERVISION_HEALTHY,
    SUPERVISION_RESTARTING,
)
from model_deck.plugins.process_runtime.health import (
    RestartPolicy,
    WorkerHealthState,
    WorkerLossCode,
    backoff_for_attempt,
)
from tests.plugins.test_worker_loss_supervision import (
    EXTENSION_ID,
    PRINCIPAL,
    WorkerSupervisionTestCase,
)

GIVE_UP_AFTER_TWO = RestartPolicy(
    max_attempts=2,
    initial_backoff_s=0.05,
    multiplier=2.0,
    max_backoff_s=0.2,
    reset_after_healthy_s=60.0,
)
SLOW_FIRST_BACKOFF = RestartPolicy(
    max_attempts=3,
    initial_backoff_s=1.0,
    multiplier=2.0,
    max_backoff_s=8.0,
    reset_after_healthy_s=60.0,
)
FORGIVE_IMMEDIATELY = RestartPolicy(
    max_attempts=3,
    initial_backoff_s=0.05,
    multiplier=2.0,
    max_backoff_s=0.2,
    reset_after_healthy_s=0.0,
)


class RestartPolicyDefaultsTests(unittest.TestCase):
    def test_shipped_defaults_are_the_documented_ones(self) -> None:
        self.assertEqual(DEFAULT_RESTART_POLICY.max_attempts, 3)
        self.assertEqual(DEFAULT_RESTART_POLICY.initial_backoff_s, 0.5)
        self.assertEqual(DEFAULT_RESTART_POLICY.multiplier, 2.0)
        self.assertEqual(DEFAULT_RESTART_POLICY.max_backoff_s, 8.0)
        self.assertEqual(DEFAULT_RESTART_POLICY.reset_after_healthy_s, 60.0)
        self.assertEqual(
            [backoff_for_attempt(DEFAULT_RESTART_POLICY, n) for n in (1, 2, 3, 9)],
            [0.5, 1.0, 2.0, 8.0],
        )


class RestartBackoffTests(WorkerSupervisionTestCase):
    restart_policy = GIVE_UP_AFTER_TWO

    def crash_and_wait_for_loss(self, host, key: str) -> None:
        before = host._activation.worker_supervision(EXTENSION_ID).last_loss
        self.crash_worker(host, key)
        self.wait_until(
            lambda: host._activation.worker_supervision(EXTENSION_ID).last_loss
            not in (None, before),
            what=f"the loss from {key} to be settled",
        )

    def test_first_loss_schedules_a_replacement_with_backoff(self) -> None:
        host = self.open_host(restart_policy=SLOW_FIRST_BACKOFF)
        self.install_and_enable(host)

        self.crash_and_wait_for_loss(host, "crash-backoff")

        report = host.supervision_report(EXTENSION_ID)
        self.assertEqual(report.supervision_status, SUPERVISION_RESTARTING)
        self.assertEqual(report.worker_state, WorkerHealthState.DEAD)
        self.assertEqual(report.restart_attempts, 1)
        self.assertFalse(report.gave_up)
        self.assertIn(report.last_failure_code, {code.value for code in WorkerLossCode})
        self.assertIsNotNone(report.next_attempt_in_s)
        self.assertGreater(report.next_attempt_in_s, 0.0)
        self.assertLessEqual(report.next_attempt_in_s, SLOW_FIRST_BACKOFF.initial_backoff_s)
        self.assertIsNone(host._activation.serving(EXTENSION_ID))

        self.wait_for_serving(host)
        self.assertEqual(
            host.supervision_report(EXTENSION_ID).supervision_status,
            SUPERVISION_HEALTHY,
        )

    def test_three_consecutive_crashes_give_up_visibly(self) -> None:
        host = self.open_host()
        self.install_and_enable(host)

        self.crash_and_wait_for_loss(host, "crash-one")
        self.wait_for_serving(host)
        self.crash_and_wait_for_loss(host, "crash-two")
        self.wait_for_serving(host)
        self.crash_and_wait_for_loss(host, "crash-three")

        report = self.wait_until(
            lambda: host.supervision_report(EXTENSION_ID).gave_up
            and host.supervision_report(EXTENSION_ID),
            what="the supervisor to give up",
        )
        self.assertTrue(report.gave_up)
        self.assertEqual(report.supervision_status, SUPERVISION_DEGRADED)
        self.assertEqual(report.worker_state, WorkerHealthState.DEAD)
        self.assertEqual(report.restart_attempts, 3)
        self.assertIsNotNone(report.last_failure_code)
        self.assertIsNone(report.next_attempt_in_s)

        # Having given up means no more children: this must still be true
        # well past the longest backoff the policy allows.
        time.sleep(GIVE_UP_AFTER_TWO.max_backoff_s * 3)
        self.assertIsNone(host._activation.serving(EXTENSION_ID))
        self.assertTrue(host.supervision_report(EXTENSION_ID).gave_up)

    def test_disable_and_enable_clears_a_degraded_extension(self) -> None:
        host = self.open_host()
        self.install_and_enable(host)
        self.crash_and_wait_for_loss(host, "clear-one")
        self.wait_for_serving(host)
        self.crash_and_wait_for_loss(host, "clear-two")
        self.wait_for_serving(host)
        self.crash_and_wait_for_loss(host, "clear-three")
        self.wait_until(
            lambda: host.supervision_report(EXTENSION_ID).gave_up,
            what="the supervisor to give up",
        )

        record = host.get_extension(EXTENSION_ID)
        disabled = host.disable(
            EXTENSION_ID,
            principal=PRINCIPAL,
            idempotency_key="disable-degraded",
            expected_revision=record.revision,
        )
        host.enable(
            EXTENSION_ID,
            principal=PRINCIPAL,
            idempotency_key="enable-degraded",
            expected_revision=disabled.record.revision,
        )

        report = host.supervision_report(EXTENSION_ID)
        self.assertFalse(report.gave_up)
        self.assertEqual(report.restart_attempts, 0)
        self.assertIsNone(report.last_failure_code)
        self.assertEqual(report.supervision_status, SUPERVISION_HEALTHY)
        self.assertIsNotNone(host._activation.serving(EXTENSION_ID))

    def test_healthy_run_forgives_the_earlier_failures(self) -> None:
        host = self.open_host(restart_policy=FORGIVE_IMMEDIATELY)
        self.install_and_enable(host)

        self.crash_and_wait_for_loss(host, "forgive-one")
        self.wait_for_serving(host)

        report = self.wait_until(
            lambda: host.supervision_report(EXTENSION_ID).restart_attempts == 0
            and host.supervision_report(EXTENSION_ID),
            what="a healthy run to reset the ledger",
        )
        self.assertEqual(report.restart_attempts, 0)
        self.assertFalse(report.gave_up)
        self.assertIsNone(report.last_failure_code)
        self.assertEqual(report.supervision_status, SUPERVISION_HEALTHY)

    def test_restarts_never_resume_the_interrupted_jobs(self) -> None:
        host = self.open_host()
        self.install_and_enable(host)
        first_job = self.start_unfinished_job(host, "export-restart-one")

        self.crash_and_wait_for_loss(host, "resume-check-one")
        self.wait_for_serving(host)
        second_job = self.start_unfinished_job(host, "export-restart-two")
        self.crash_and_wait_for_loss(host, "resume-check-two")
        self.wait_for_serving(host)

        for job_id in (first_job, second_job):
            snapshot = host.job_get({"job_id": job_id}, principal=PRINCIPAL)
            self.assertEqual(snapshot["state"], "interrupted")
            self.assertEqual(snapshot.get("resume_count", 0), 0)


if __name__ == "__main__":
    unittest.main()
