import threading
import unittest
from typing import Any

from model_deck.adapters.events.live_replay import LiveRunEventReplay, encoded_event_bytes
from model_deck.engine.runs.ports import (
    RUN_LIVE_REPLAY_MAX_BYTES,
    RUN_LIVE_REPLAY_MAX_DURATION_SECONDS,
    SUBSCRIBER_QUEUE_MAX_BYTES,
    SUBSCRIBER_QUEUE_MAX_EVENTS,
    ApplicationRunEvent,
    EventReplayOutcome,
)

RUN_ID = "550e8400-e29b-41d4-a716-446655440010"
SESSION_ID = "550e8400-e29b-41d4-a716-446655440011"


def _event(sequence: int, payload: Any = None, run_id: str = RUN_ID) -> ApplicationRunEvent:
    return ApplicationRunEvent(
        kind="content.delta",
        run_id=run_id,
        session_id=SESSION_ID,
        sequence=sequence,
        event_schema_version=1,
        observed_at="2026-01-01T00:00:00Z",
        payload=payload,
    )


class LiveRunEventReplayTests(unittest.TestCase):
    def test_publish_after_subscribe_read_ack_flow(self) -> None:
        clock = [0.0]
        replay = LiveRunEventReplay(clock=lambda: clock[0], id_factory=lambda: "sub-1")
        handle, page = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=2)
        self.assertEqual(page.outcome, EventReplayOutcome.DELIVERED)
        self.assertEqual(page.events, ())
        replay.publish_application_event(_event(1, {"text": "a"}))
        page = replay.read_available(handle)
        self.assertEqual(len(page.events), 1)
        self.assertEqual(page.events[0].sequence, 1)
        self.assertEqual(page.credit_remaining, 1)
        ack = replay.ack(handle, 1, 2)
        self.assertEqual(ack.outcome, EventReplayOutcome.DELIVERED)
        self.assertEqual(ack.credit_remaining, 3)

    def test_credit_exhaustion_then_ack_replenish(self) -> None:
        replay = LiveRunEventReplay(id_factory=lambda: "sub-a")
        handle, _ = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=1)
        replay.publish_application_event(_event(1))
        replay.publish_application_event(_event(2))
        first = replay.read_available(handle)
        self.assertEqual(len(first.events), 1)
        self.assertEqual(first.credit_remaining, 0)
        second = replay.read_available(handle)
        self.assertEqual(second.events, ())
        ack = replay.ack(handle, 1, 2)
        self.assertEqual(ack.credit_remaining, 2)
        third = replay.read_available(handle)
        self.assertEqual(len(third.events), 1)
        self.assertEqual(third.events[0].sequence, 2)

    def test_duplicate_ack_does_not_inflate_credit(self) -> None:
        replay = LiveRunEventReplay(id_factory=lambda: "sub-dup")
        handle, _ = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=1)
        for sequence in range(1, 5):
            replay.publish_application_event(_event(sequence))
        replay.read_available(handle)
        first = replay.ack(handle, 1, 1)
        self.assertEqual(first.credit_remaining, 1)
        replay.read_available(handle)
        duplicate = replay.ack(handle, 1, 1)
        self.assertEqual(duplicate.credit_remaining, 0)

    def test_first_event_after_restart_may_continue_durable_sequence(self) -> None:
        replay = LiveRunEventReplay()
        replay.publish_application_event(_event(4))
        handle, page = replay.subscribe(RUN_ID, after_sequence=3, grant_credit=1)
        self.assertEqual(tuple(event.sequence for event in page.events), (4,))

    def test_independent_slow_reader(self) -> None:
        replay = LiveRunEventReplay()
        slow_handle, _ = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=10)
        fast_handle, _ = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=10)
        big = "x" * (SUBSCRIBER_QUEUE_MAX_BYTES // 2)
        replay.publish_application_event(_event(1, {"blob": big}))
        replay.read_available(fast_handle)
        replay.publish_application_event(_event(2, {"blob": big}))
        fast_page = replay.read_available(fast_handle)
        self.assertEqual(fast_page.outcome, EventReplayOutcome.DELIVERED)
        self.assertEqual(len(fast_page.events), 1)
        self.assertEqual(fast_page.events[0].sequence, 2)
        slow_page = replay.read_available(slow_handle)
        self.assertEqual(slow_page.outcome, EventReplayOutcome.SLOW_READER)
        self.assertEqual(len(slow_page.events), 1)
        self.assertEqual(slow_page.events[0].sequence, 1)

    def test_subscriber_event_cap_marks_slow_reader(self) -> None:
        replay = LiveRunEventReplay()
        handle, _ = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=256)
        for seq in range(1, SUBSCRIBER_QUEUE_MAX_EVENTS + 2):
            replay.publish_application_event(_event(seq, {"n": seq}))
        page = replay.read_available(handle)
        self.assertEqual(page.outcome, EventReplayOutcome.SLOW_READER)

    def test_subscriber_byte_cap_marks_slow_reader(self) -> None:
        replay = LiveRunEventReplay()
        handle, _ = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=256)
        chunk = "a" * 600_000
        replay.publish_application_event(_event(1, {"blob": chunk}))
        replay.publish_application_event(_event(2, {"blob": chunk}))
        page = replay.read_available(handle)
        self.assertEqual(page.outcome, EventReplayOutcome.SLOW_READER)

    def test_run_buffer_byte_eviction_resume_unavailable(self) -> None:
        clock = [0.0]
        replay = LiveRunEventReplay(clock=lambda: clock[0])
        big = "b" * 700_000
        for seq in range(1, 14):
            replay.publish_application_event(_event(seq, {"blob": big}))
        handle, page = replay.subscribe(RUN_ID, after_sequence=1, grant_credit=10)
        self.assertEqual(page.outcome, EventReplayOutcome.RESUME_UNAVAILABLE)

    def test_run_buffer_time_eviction_resume_unavailable(self) -> None:
        clock = [0.0]
        replay = LiveRunEventReplay(clock=lambda: clock[0])
        replay.publish_application_event(_event(1))
        clock[0] = RUN_LIVE_REPLAY_MAX_DURATION_SECONDS + 1
        replay.publish_application_event(_event(2))
        handle, page = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=10)
        self.assertEqual(page.outcome, EventReplayOutcome.RESUME_UNAVAILABLE)

    def test_idle_run_buffer_expires_before_subscribe(self) -> None:
        clock = [0.0]
        replay = LiveRunEventReplay(clock=lambda: clock[0])
        replay.publish_application_event(_event(1))
        clock[0] = RUN_LIVE_REPLAY_MAX_DURATION_SECONDS + 1
        handle, page = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=10)
        self.assertEqual(page.outcome, EventReplayOutcome.RESUME_UNAVAILABLE)

    def test_fresh_resume_within_buffer(self) -> None:
        replay = LiveRunEventReplay()
        for seq in range(1, 4):
            replay.publish_application_event(_event(seq))
        handle, page = replay.subscribe(RUN_ID, after_sequence=1, grant_credit=10)
        self.assertEqual(page.outcome, EventReplayOutcome.DELIVERED)
        self.assertEqual(tuple(event.sequence for event in page.events), (2, 3))

    def test_unsubscribe_removes_subscription(self) -> None:
        replay = LiveRunEventReplay(id_factory=lambda: "sub-x")
        handle, _ = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=2)
        replay.unsubscribe(handle)
        with self.assertRaises(KeyError):
            replay.read_available(handle)

    def test_publish_rejects_non_monotonic_sequence(self) -> None:
        replay = LiveRunEventReplay()
        replay.publish_application_event(_event(1))
        with self.assertRaises(ValueError):
            replay.publish_application_event(_event(1))
        with self.assertRaises(ValueError):
            replay.publish_application_event(_event(0))

    def test_publish_rejects_sequence_gap(self) -> None:
        replay = LiveRunEventReplay()
        replay.publish_application_event(_event(1))
        with self.assertRaises(ValueError):
            replay.publish_application_event(_event(3))

    def test_new_instance_starts_empty(self) -> None:

        first = LiveRunEventReplay()
        first.publish_application_event(_event(1))
        second = LiveRunEventReplay()
        handle, page = second.subscribe(RUN_ID, after_sequence=0, grant_credit=3)
        self.assertEqual(page.events, ())

    def test_encoded_event_bytes_rejects_nonfinite(self) -> None:
        event = _event(1, float("nan"))
        with self.assertRaises(ValueError):
            encoded_event_bytes(event)

    def test_publish_detaches_caller_owned_nested_payload(self) -> None:
        replay = LiveRunEventReplay()
        payload = {"content": {"text": "small"}}
        replay.publish_application_event(_event(1, payload))
        payload["content"]["text"] = "x" * (SUBSCRIBER_QUEUE_MAX_BYTES * 2)

        _, page = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=1)

        self.assertEqual(page.outcome, EventReplayOutcome.DELIVERED)
        self.assertEqual(page.events[0].payload["content"]["text"], "small")
        self.assertLess(page.bytes_delivered, SUBSCRIBER_QUEUE_MAX_BYTES)

    def test_delivered_payload_mutation_does_not_affect_other_subscribers(self) -> None:
        replay = LiveRunEventReplay()
        replay.publish_application_event(_event(1, {"content": {"text": "small"}}))
        _, first = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=1)
        first.events[0].payload["content"]["text"] = "changed"

        _, second = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=1)

        self.assertEqual(second.events[0].payload["content"]["text"], "small")

    def test_grant_credit_range(self) -> None:
        replay = LiveRunEventReplay()
        with self.assertRaises(ValueError):
            replay.subscribe(RUN_ID, after_sequence=0, grant_credit=0)
        with self.assertRaises(ValueError):
            replay.subscribe(RUN_ID, after_sequence=0, grant_credit=257)

    def test_concurrent_publishers(self) -> None:
        replay = LiveRunEventReplay()
        handle, _ = replay.subscribe(RUN_ID, after_sequence=0, grant_credit=256)
        errors: list[BaseException] = []

        def publish(seq: int) -> None:
            try:
                replay.publish_application_event(_event(seq))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=publish, args=(i,)) for i in range(1, 21)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        page = replay.read_available(handle)
        self.assertEqual(len(page.events), 20)


if __name__ == "__main__":
    unittest.main()
