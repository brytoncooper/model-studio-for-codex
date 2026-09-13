"""Injected Cursor process fixtures: no SDK, subprocess or live credentials."""
import copy
import queue
import threading
import time
import unittest

from model_deck.engine.runs.ports import ProviderCancelTerminationStatus, SubmitToolResultProviderOutcome, ToolDefinition
from model_deck.integrations.providers.cursor import CursorProcessRuntime, CursorProviderRunHandle, CursorStartRequest, PreparedCursorRun

REQUEST = CursorStartRequest('550e8400-e29b-41d4-a716-446655440011',
    '550e8400-e29b-41d4-a716-446655440013', '550e8400-e29b-41d4-a716-446655440015',
    'model', tools=(ToolDefinition('host.search', {}, True),))


class FakeProcess:
    def __init__(self):
        self.events = queue.Queue()
        self.results = []
        self.closed = threading.Event()
        self.close_release = threading.Event()
        self.close_release.set()
        self.close_count = 0

    def tool_result(self, call_id, output):
        self.results.append((call_id, copy.deepcopy(output)))

    def close(self):
        self.close_count += 1
        self.close_release.wait(2)
        self.closed.set()


class ProcessAdapterTests(unittest.TestCase):
    def build(self, normalize=None, on_event=None):
        process = FakeProcess()
        payload = {'model':'exact/model', 'api_key':'secret-test-value', 'tools':[{'name':'alias'}],
                   'message':{'text':'literal \\n text'}, 'reasoning':{'effort':'high'}, 'service_tier':'priority'}
        received, events = [], queue.Queue()
        def factory(value):
            received.append(copy.deepcopy(value))
            return process
        runtime = CursorProcessRuntime(process_factory=factory,
            prepare_payload=lambda request: PreparedCursorRun(payload, {'alias':'host.search'}),
            normalize_usage=normalize or (lambda request, observations, final: ()))
        session = runtime.start(REQUEST, on_event or events.put)
        self.addCleanup(process.close_release.set)
        self.addCleanup(session.close)
        return process, session, events, payload, received

    def test_payload_preserved_and_sdk_event_mapping(self):
        process, session, events, payload, received = self.build()
        self.assertEqual(received, [payload])
        payload['reasoning']['effort'] = 'low'
        self.assertEqual(received[0]['reasoning'], {'effort':'high'})
        for event in ({'type':'started','agent_id':'sdk-agent'}, {'type':'text','text':'hello'},
                      {'type':'thinking','text':'reason'}, {'type':'done','status':'finished'}):
            process.events.put(event)
        observed = [events.get(timeout=1) for _ in range(4)]
        self.assertEqual([event.kind for event in observed], ['run.started','content.delta','content.delta','run.completed'])
        self.assertEqual(observed[2].payload, {'channel':'reasoning','delta':'reason'})
        self.assertTrue(process.closed.wait(1))
        session.close()
        self.assertEqual(process.close_count, 1)

    def test_alias_and_tool_result_forwarded_once(self):
        process, session, events, _, _ = self.build()
        process.events.put({'type':'started'})
        process.events.put({'type':'tool_call','call_id':'c1','name':'alias','arguments':{'q':'x'}})
        events.get(timeout=1)
        tool = events.get(timeout=1)
        self.assertEqual(tool.payload['tool_call']['tool_name'], 'host.search')
        self.assertEqual(session.submit_tool_result('c1', {'ok':True}).outcome, SubmitToolResultProviderOutcome.ACCEPTED)
        self.assertEqual(session.submit_tool_result('c1', {}).outcome, SubmitToolResultProviderOutcome.REJECTED)
        self.assertEqual(process.results, [('c1', {'ok':True})])

    def test_falsy_tool_arguments_preserved_through_coordinator(self):
        for arguments in (False, 0, [], '', None):
            with self.subTest(arguments=arguments):
                published = queue.Queue()
                class Sink:
                    def publish_provider_event(self, event):
                        published.put(event)
                handle = CursorProviderRunHandle(run_id=REQUEST.run_id, sink=Sink(),
                    now=lambda: '2026-09-12T00:00:00Z')
                process, session, _, _, _ = self.build(on_event=handle.on_sdk_event)
                handle.bind(session)
                process.events.put({'type':'started'})
                process.events.put({'type':'tool_call','call_id':'c1','name':'alias','arguments':arguments})
                self.assertEqual(published.get(timeout=1).kind, 'run.started')
                tool = published.get(timeout=1)
                self.assertEqual(tool.kind, 'tool.requested')
                self.assertEqual(tool.payload['arguments'], arguments)
                self.assertIs(type(tool.payload['arguments']), type(arguments))
                self.assertEqual(handle.outstanding_call_id, 'c1')
                session.close()
                self.assertTrue(process.closed.wait(1))
                self.assertEqual(process.close_count, 1)

    def test_missing_tool_arguments_fail_closed_through_coordinator(self):
        published = queue.Queue()
        class Sink:
            def publish_provider_event(self, event):
                published.put(event)
        handle = CursorProviderRunHandle(run_id=REQUEST.run_id, sink=Sink(),
            now=lambda: '2026-09-12T00:00:00Z')
        process, session, _, _, _ = self.build(on_event=handle.on_sdk_event)
        handle.bind(session)
        process.events.put({'type':'started'})
        process.events.put({'type':'tool_call','call_id':'c1','name':'alias'})
        self.assertEqual(published.get(timeout=1).kind, 'run.started')
        self.assertEqual(published.get(timeout=1).kind, 'run.failed')
        self.assertTrue(handle.is_terminal)
        self.assertIsNone(handle.outstanding_call_id)
        self.assertTrue(process.closed.wait(1))
        session.close()
        self.assertEqual(process.close_count, 1)
        self.assertTrue(published.empty())

    def test_usage_callback_receives_intermediate_and_final_separately(self):
        calls = []
        def normalize(request, observations, final):
            calls.append((request, observations, final))
            return ({'units': final['usage']['input_tokens']},)
        process, _, events, _, _ = self.build(normalize)
        for event in ({'type':'started'}, {'type':'usage','usage':{'input_tokens':2}},
                      {'type':'usage','usage':{'input_tokens':3}},
                      {'type':'done','status':'finished','usage':{'input_tokens':5}}):
            process.events.put(event)
        observed = [events.get(timeout=1) for _ in range(3)]
        self.assertEqual([event.kind for event in observed], ['run.started','usage.observed','run.completed'])
        self.assertEqual(observed[1].payload, {'usage':{'units':5}})
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][1]), 2)
        self.assertEqual(calls[0][2]['usage']['input_tokens'], 5)

    def test_error_before_started_and_eof_error_are_safe(self):
        for event in ({'type':'error','message':'secret-test-value'},
                      {'type':'error','message':'Cursor SDK stopped before completing the run.'}):
            process, _, events, _, _ = self.build()
            process.events.put(event)
            terminal = events.get(timeout=1)
            self.assertEqual(terminal.kind, 'run.failed')
            self.assertNotIn('secret-test-value', repr(terminal))
            self.assertTrue(process.closed.wait(1))
            self.assertTrue(events.empty())

    def test_cancel_returns_before_slow_close_without_confirmation(self):
        process, session, events, _, _ = self.build()
        process.close_release.clear()
        start = time.monotonic()
        result = session.request_cancel(deadline='2026-09-12T00:00:00Z')
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertTrue(result.request_accepted)
        self.assertEqual(result.termination_status, ProviderCancelTerminationStatus.UNKNOWN)
        self.assertFalse(process.closed.is_set())
        self.assertFalse(session.request_cancel(deadline='ignored').request_accepted)
        process.close_release.set()
        self.assertEqual(events.get(timeout=1).kind, 'run.interrupted')
        self.assertTrue(events.empty())
        self.assertEqual(process.close_count, 1)

    def test_unknown_alias_and_prestart_text_fail_closed(self):
        for bad in ({'type':'text','text':'too early'}, {'type':'tool_call','name':'foreign','call_id':'c','arguments':{}}):
            process, _, events, _, _ = self.build()
            if bad['type'] == 'tool_call':
                process.events.put({'type':'started'})
                self.assertEqual(events.get(timeout=1).kind, 'run.started')
            process.events.put(bad)
            self.assertEqual(events.get(timeout=1).kind, 'run.failed')
            self.assertTrue(process.closed.wait(1))

    def test_usage_callback_failure_is_safe_and_closes_once(self):
        def broken(*args):
            raise RuntimeError('secret-test-value')
        process, session, events, _, _ = self.build(broken)
        process.events.put({'type':'started'})
        process.events.put({'type':'done','status':'finished'})
        self.assertEqual(events.get(timeout=1).kind, 'run.started')
        failed = events.get(timeout=1)
        self.assertEqual(failed.kind, 'run.failed')
        self.assertNotIn('secret-test-value', repr(failed))
        self.assertTrue(process.closed.wait(1))
        session.close()
        self.assertEqual(process.close_count, 1)

    def test_factory_failure_does_not_echo_prepared_credentials(self):
        def factory(payload):
            raise RuntimeError(payload['api_key'])
        runtime = CursorProcessRuntime(process_factory=factory,
            prepare_payload=lambda request: PreparedCursorRun({'api_key':'secret-test-value'}, {}),
            normalize_usage=lambda *args: ())
        import traceback
        try:
            runtime.start(REQUEST, lambda event: None)
        except RuntimeError as error:
            self.assertNotIn('secret-test-value', str(error) + repr(error) + traceback.format_exc())
        else:
            self.fail('factory failure unexpectedly accepted')
