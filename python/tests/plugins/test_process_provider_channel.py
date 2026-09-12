"""Owned subprocess coverage for the activation-bound provider channel."""
import concurrent.futures
import json
import shutil
import traceback
import unittest
from pathlib import Path

from model_deck.plugins.process_runtime import ProcessRuntimeError, ProviderMethod
from tests.plugins.test_process_runtime import (
    ACTIVATION_ID, PLUGIN_ID, PLUGIN_VERSION, TOKEN, _runtime, _session, _Watchdog,
)

EVENT = {"adapter_run_handle": ACTIVATION_ID, "sequence": 0, "event": {
    "kind": "run.started", "run_id": ACTIVATION_ID, "session_id": ACTIVATION_ID,
    "sequence": 0, "event_schema_version": 1, "observed_at": "2026-09-12T00:00:00Z",
}}
CANCEL = {"adapter_run_handle": ACTIVATION_ID}
TOOL = {"adapter_run_handle": ACTIVATION_ID, "call_id": "call-1", "result": {"ok": True}}


def child(provider_body):
    return f'''import json,sys,time
EVENT={EVENT!r}
def emit(frame):
    sys.stdout.write(json.dumps(frame)+"\\n")
    sys.stdout.flush()
def reply(req,result):
    emit({{"jsonrpc":"2.0","id":req["id"],"result":result}})
def event():
    emit({{"jsonrpc":"2.0","method":"plugin.v1.provider.event","params":EVENT}})
for line in sys.stdin:
    req=json.loads(line)
    method=req["method"]
    if method.endswith(".hello"):
        reply(req,{{"plugin_id":{PLUGIN_ID!r},"plugin_version":{PLUGIN_VERSION!r},"capabilities":[]}})
    elif method.endswith(".activate"):
        reply(req,{{"activation_id":{ACTIVATION_ID!r}}})
    elif method.endswith(".drain"):
        reply(req,{{"drained":True}})
    else:
''' + "\n".join("        " + line for line in provider_body.splitlines()) + "\n"


class ProviderChannelTests(unittest.TestCase):
    def runtime(self, body, **config):
        runtime = _runtime(child(body), **config)
        self.addCleanup(shutil.rmtree, runtime.config.package_dir)
        self.addCleanup(runtime.close)
        watchdog = _Watchdog(runtime)
        watchdog.__enter__()
        self.addCleanup(watchdog.__exit__)
        runtime.spawn()
        return runtime

    def activate(self, body, **config):
        runtime = self.runtime(body, **config)
        session = _session()
        runtime.run_hello(session, "channel-nonce")
        runtime.run_activation(session)
        return runtime, session, runtime.provider_channel()

    def test_same_runtime_session_binding_and_no_channel_before_activation(self):
        runtime = self.runtime("reply(req, {'accepted': True})")
        with self.assertRaises(ProcessRuntimeError):
            runtime.provider_channel()
        first, second = _session(), _session()
        runtime.run_hello(first, "bound")
        with self.assertRaises(ProcessRuntimeError):
            runtime.provider_channel()
        # Even a different session independently advanced through hello cannot
        # authorize activation on this runtime's authenticated pipe.
        second.prepare_hello_request("other")
        second.accept_hello_result({"plugin_id": PLUGIN_ID, "plugin_version": PLUGIN_VERSION, "capabilities": []})
        with self.assertRaises(ProcessRuntimeError):
            runtime.run_activation(second)
        self.assertIsNone(runtime._proc)

    def test_interleaved_events_and_ack_request_result(self):
        runtime, session, channel = self.activate("event()\nreply(req, {'credit': 1})\nevent()")
        self.assertEqual(channel.activation_id, ACTIVATION_ID)
        self.assertEqual(channel.request(ProviderMethod.ACK, {"adapter_run_handle": ACTIVATION_ID, "sequence": 0}), {"credit": 1})
        first = channel.receive_event(timeout_s=1)
        first["event"]["kind"] = "changed"
        self.assertEqual(channel.receive_event(timeout_s=1), EVENT)
        self.assertIsNone(channel.receive_event(timeout_s=0.01))
        runtime.run_drain(session, 1000)

    def test_complete_reply_and_partial_event_remain_in_decoder(self):
        body = '''wire=json.dumps({'jsonrpc':'2.0','method':'plugin.v1.provider.event','params':EVENT})+'\\n'
response=json.dumps({'jsonrpc':'2.0','id':req['id'],'result':{'accepted':True}})+'\\n'
sys.stdout.write(response+wire[:40]); sys.stdout.flush()
time.sleep(0.05)
sys.stdout.write(wire[40:]); sys.stdout.flush()'''
        _, _, channel = self.activate(body)
        self.assertEqual(channel.request(ProviderMethod.CANCEL, CANCEL), {"accepted": True})
        self.assertEqual(channel.receive_event(timeout_s=1), EVENT)

    def test_concurrent_tool_cancel_reversed_replies_and_event_receiver(self):
        body = '''second=json.loads(sys.stdin.readline())
event()
for command in (second,req):
    reply(command, {'accepted':True, **({'confirmed':False} if command['method'].endswith('.cancel') else {})})'''
        _, _, channel = self.activate(body)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            receive = pool.submit(channel.receive_event, timeout_s=2)
            tool = pool.submit(channel.request, ProviderMethod.SUBMIT_TOOL_RESULT, TOOL)
            cancel = pool.submit(channel.request, ProviderMethod.CANCEL, CANCEL)
            self.assertEqual(tool.result(3), {"accepted": True})
            self.assertEqual(cancel.result(3), {"accepted": True, "confirmed": False})
            self.assertEqual(receive.result(3), EVENT)

    def test_unknown_duplicate_and_boolean_reply_ids_fail_closed(self):
        bodies = (
            "emit({'jsonrpc':'2.0','id':9999,'result':{'accepted':True}})",
            "sys.stdout.write((json.dumps({'jsonrpc':'2.0','id':req['id'],'result':{'accepted':True}})+'\\n')*2);sys.stdout.flush()",
            "emit({'jsonrpc':'2.0','id':True,'result':{'accepted':True}})",
        )
        for body in bodies:
            with self.subTest(body=body):
                runtime, _, channel = self.activate(body)
                proc = runtime._proc
                with self.assertRaises(ProcessRuntimeError):
                    channel.request(ProviderMethod.CANCEL, CANCEL)
                self.assertIsNone(runtime._proc)
                self.assertIsNotNone(proc.poll())

    def test_timeout_closes_owned_child_without_resubmit(self):
        runtime, _, channel = self.activate("time.sleep(30)")
        proc = runtime._proc
        with self.assertRaises(ProcessRuntimeError) as caught:
            channel.request(ProviderMethod.CANCEL, CANCEL, timeout_s=0.05)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertIsNone(runtime._proc)
        self.assertIsNotNone(proc.poll())

    def test_close_releases_reply_and_event_waiters(self):
        runtime, _, channel = self.activate("event()\ntime.sleep(30)")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            request = pool.submit(channel.request, ProviderMethod.CANCEL, CANCEL)
            # This event proves the outstanding request reached the child.
            self.assertEqual(channel.receive_event(timeout_s=1), EVENT)
            receiver = pool.submit(channel.receive_event, timeout_s=5)
            runtime.close()
            for future in (request, receiver):
                with self.assertRaises(ProcessRuntimeError):
                    future.result(2)

    def test_event_count_overflow_closes_without_dropping(self):
        runtime, _, channel = self.activate("for number in range(257): event()")
        with self.assertRaises(ProcessRuntimeError) as caught:
            channel.request(ProviderMethod.CANCEL, CANCEL)
        self.assertEqual(caught.exception.code, "frame_limit")
        self.assertIsNone(runtime._proc)

    def test_event_byte_overflow_closes(self):
        body = '''EVENT['event'].update(kind='content.delta', delta='x'*65536, channel='text')
for number in range(17): event()'''
        runtime, _, channel = self.activate(body)
        with self.assertRaises(ProcessRuntimeError) as caught:
            channel.request(ProviderMethod.CANCEL, CANCEL)
        self.assertEqual(caught.exception.code, "frame_limit")
        self.assertIsNone(runtime._proc)

    def test_pending_command_limit_is_bounded(self):
        runtime, _, channel = self.activate("event()\ntime.sleep(30)", max_pending_requests=1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(channel.request, ProviderMethod.CANCEL, CANCEL)
            self.assertEqual(channel.receive_event(timeout_s=1), EVENT)
            with self.assertRaises(ProcessRuntimeError) as caught:
                channel.request(ProviderMethod.CANCEL, CANCEL)
            self.assertEqual(caught.exception.code, "frame_limit")
            with self.assertRaises(ProcessRuntimeError):
                first.result(2)
        self.assertIsNone(runtime._proc)

    def test_schema_errors_and_remote_errors_do_not_echo_secrets(self):
        for body in ("reply(req, {'accepted': '" + TOKEN + "'})",
                     "emit({'jsonrpc':'2.0','id':req['id'],'error':{'code':-1,'message':" + repr(TOKEN) + "}})",
                     "EVENT['event']['extra']=" + repr(TOKEN) + ";event()"):
            with self.subTest(body=body):
                runtime, _, channel = self.activate(body)
                try:
                    channel.request(ProviderMethod.CANCEL, CANCEL)
                except ProcessRuntimeError as failure:
                    self.assertNotIn(TOKEN, str(failure) + repr(failure) + traceback.format_exc())
                else:
                    self.fail("invalid provider frame accepted")
                self.assertIsNone(runtime._proc)

    def test_outbound_frozen_schema_and_method_allowlist(self):
        for method, params in ((ProviderMethod.CANCEL, {**CANCEL, "activation_token": TOKEN}),
                               ("plugin.v1.broker.credentials.resolve", {})):
            runtime, _, channel = self.activate("reply(req, {'accepted':True})")
            with self.assertRaises(ProcessRuntimeError):
                channel.request(method, params)
            self.assertIsNone(runtime._proc)

    def test_draining_refuses_new_start(self):
        runtime, session, channel = self.activate("reply(req, {'accepted':True})")
        runtime.run_drain(session, 1000)
        fixture = Path(__file__).resolve().parents[2] / "src/model_deck_contracts/schemas/fixtures/valid/provider_start_minimal.json"
        with self.assertRaises(ProcessRuntimeError):
            channel.request(ProviderMethod.START, json.loads(fixture.read_text()))

    def test_start_uses_frozen_params_and_returns_handle(self):
        _, _, channel = self.activate("reply(req, {'adapter_run_handle': EVENT['adapter_run_handle']})")
        fixture = Path(__file__).resolve().parents[2] / "src/model_deck_contracts/schemas/fixtures/valid/provider_start_minimal.json"
        result = channel.request(ProviderMethod.START, json.loads(fixture.read_text()))
        self.assertEqual(result, {"adapter_run_handle": ACTIVATION_ID})

    def test_eof_interrupts_pending_request_and_closes_pipes(self):
        runtime, _, channel = self.activate("sys.exit(0)")
        proc = runtime._proc
        with self.assertRaises(ProcessRuntimeError) as caught:
            channel.request(ProviderMethod.CANCEL, CANCEL)
        self.assertEqual(caught.exception.code, "malformed_eof")
        self.assertTrue(all(pipe.closed for pipe in (proc.stdin, proc.stdout, proc.stderr)))

    def test_notification_request_and_oversized_frame_rejected(self):
        for body in (
            "emit({'jsonrpc':'2.0','id':17,'method':'plugin.v1.provider.event','params':EVENT})",
            "emit({'jsonrpc':'2.0','method':'plugin.v1.provider.other','params':EVENT})",
            "sys.stdout.write('x'*1048577+'\\n');sys.stdout.flush()",
        ):
            with self.subTest(body=body):
                runtime, _, channel = self.activate(body)
                with self.assertRaises(ProcessRuntimeError):
                    channel.request(ProviderMethod.CANCEL, CANCEL)
                self.assertIsNone(runtime._proc)

    def test_lifecycle_deactivation_invalidates_existing_channel(self):
        runtime, session, channel = self.activate("reply(req, {'accepted':True})")
        runtime.run_drain(session, 1000)
        session.deactivate()
        with self.assertRaises(ProcessRuntimeError):
            channel.receive_event(timeout_s=0)
        self.assertIsNone(runtime._proc)


if __name__ == "__main__":
    unittest.main()
