"""Actual SQLite snapshot enumeration, independent of usage-query consumers."""
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.storage.sqlite_session_run_repository import SQLiteSessionRunRepository
from model_deck.engine.runs.ports import ActiveRunState, AppendApplicationEventCommand, ClaimDispatchCommand
from model_deck.engine.runs.usage_events import (
    CommittedUsageCursor, CommittedUsageCursorError, CommittedUsageEventReader,
    CommittedUsageReadError, MAX_COMMITTED_USAGE_PAGE_BYTES,
)
from tests.engine.test_sqlite_session_run_repository import (
    _repo, _create_session, _start_command, RUN_ID, SESSION_ID,
)


class CommittedUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='md-usage-reader-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.repo = _repo(self.temp.name)
        _create_session(self.repo)
        self.repo.admit(_start_command())
        self.repo.claim_dispatch(ClaimDispatchCommand(run_id=RUN_ID, dispatch_token=RUN_ID))

    def append(self, payload, *, repo=None, kind='usage.observed'):
        return (repo or self.repo).append_application_event(AppendApplicationEventCommand(
            run_id=RUN_ID, expected_state=ActiveRunState.RUNNING,
            new_state=ActiveRunState.RUNNING, kind=kind, payload=payload)).event

    def collect(self, repo, first=None, limit=256):
        page = first or repo.read_committed_usage_events(limit=limit)
        high_water = page.high_water
        events = list(page.events)
        while page.next_cursor is not None:
            page = repo.read_committed_usage_events(cursor=page.next_cursor, limit=limit)
            self.assertEqual(page.high_water, high_water)
            events.extend(page.events)
        return events

    def test_pagination_filters_and_preserves_full_identity_payload(self):
        expected = []
        for i in range(260):
            if i % 40 == 0:
                self.append({'delta': 'not durable usage'}, kind='content.delta')
            expected.append(self.append({'index': i, 'nested': [True, None, {'text': 'é'}]}))
        first = self.repo.read_committed_usage_events()
        self.assertEqual(len(first.events), 256)
        actual = self.collect(self.repo, first)
        self.assertEqual([(e.run_id, e.sequence, e.payload) for e in actual],
                         [(e.run_id, e.sequence, e.payload) for e in expected])
        self.assertTrue(all(e.session_id == SESSION_ID and e.event_schema_version == 1 for e in actual))
        self.assertEqual(actual[0].observed_at, expected[0].observed_at)
        actual[0].payload['nested'].append('mutation')
        self.assertEqual(actual[0].payload, expected[0].payload)
        self.assertIsInstance(self.repo, CommittedUsageEventReader)

    def test_concurrent_later_insert_deferred_and_restart_starts_fresh(self):
        for i in range(4):
            self.append({'index': i})
        first = self.repo.read_committed_usage_events(limit=2)
        restarted = SQLiteSessionRunRepository(self.path)
        failures = []
        def insert():
            try:
                self.append({'index': 4}, repo=restarted)
            except Exception as error:
                failures.append(error)
        thread = threading.Thread(target=insert)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual([e.payload['index'] for e in self.collect(self.repo, first, 2)], list(range(4)))
        with self.assertRaises(CommittedUsageCursorError):
            restarted.read_committed_usage_events(cursor=first.next_cursor)
        self.assertEqual([e.payload['index'] for e in self.collect(restarted)], list(range(5)))

    def test_invalid_cursor_and_bounds_rejected_before_connect(self):
        self.append({'x': 1})
        self.append({'x': 2})
        cursor = self.repo.read_committed_usage_events(limit=1).next_cursor
        original = self.repo._connect_factory
        self.repo._connect_factory = lambda _: self.fail('invalid input opened database')
        try:
            for limit in (False, 0, -1, 257, 1.5, '1'):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    self.repo.read_committed_usage_events(limit=limit)
            for bad in ('bad', CommittedUsageCursor('bad'), CommittedUsageCursor('A' + cursor.token[1:]), CommittedUsageCursor(3)):
                with self.subTest(cursor=bad), self.assertRaises(CommittedUsageCursorError):
                    self.repo.read_committed_usage_events(cursor=bad)
        finally:
            self.repo._connect_factory = original

    def test_byte_bound_splits_without_skipping(self):
        for i in range(3):
            self.append({'index': i, 'text': 'é' * 100000})
        first = self.repo.read_committed_usage_events()
        self.assertEqual(len(first.events), 1)
        events = self.collect(self.repo, first)
        self.assertEqual([e.payload['index'] for e in events], [0, 1, 2])
        self.assertTrue(all(len(e.payload_json.encode()) <= MAX_COMMITTED_USAGE_PAGE_BYTES for e in events))

    def test_oversized_or_corrupt_payload_fails_instead_of_skipping(self):
        event = self.append({'text': 'x' * MAX_COMMITTED_USAGE_PAGE_BYTES})
        with self.assertRaises(CommittedUsageReadError):
            self.repo.read_committed_usage_events()
        for payload in ('{', '{"x":NaN}', '{"x":1e999}', None):
            with sqlite3.connect(self.path) as connection:
                connection.execute('UPDATE run_application_events SET payload_json=? WHERE run_id=? AND sequence=?',
                                   (payload, RUN_ID, event.sequence))
            with self.subTest(payload=payload), self.assertRaises(CommittedUsageReadError):
                self.repo.read_committed_usage_events()

    def test_blob_json_payload_is_rejected_with_safe_error(self):
        event = self.append({'x': 1})
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                'UPDATE run_application_events SET payload_json=? WHERE run_id=? AND sequence=?',
                (b'{"secret_sentinel":1}', RUN_ID, event.sequence))
        with self.assertRaises(CommittedUsageReadError) as caught:
            self.repo.read_committed_usage_events()
        self.assertEqual(str(caught.exception), 'committed usage payload is not JSON text')
        self.assertNotIn('secret_sentinel', str(caught.exception))

    def test_empty_snapshot_is_complete(self):
        page = self.repo.read_committed_usage_events()
        self.assertEqual(page.events, ())
        self.assertIsNone(page.next_cursor)


if __name__ == '__main__':
    unittest.main()
