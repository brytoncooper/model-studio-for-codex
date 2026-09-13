"""Acceptance tests for the B13 HTTP POST transport adapter slice."""
from __future__ import annotations

import threading
import unittest

from model_deck.integrations.providers.openai_compatible.http_transport import (
    HttpStreamResponse,
    post_stream,
)

SECRET = "Bearer sk-secret-value"


class FakeRaw:
    def __init__(self, status=200, chunks=(b"hello", b" world"), read_error=None):
        self.status = status
        self._chunks = list(chunks)
        self._read_error = read_error

    def read(self, amt=None):
        if self._read_error is not None:
            raise self._read_error
        if amt is None:
            out = b"".join(self._chunks)
            self._chunks = []
            return out
        out = b""
        while self._chunks and len(out) < amt:
            need = amt - len(out)
            head = self._chunks[0]
            out += head[:need]
            self._chunks[0] = head[need:]
            if not self._chunks[0]:
                self._chunks.pop(0)
        return out

    def read1(self, amt=-1):
        if self._read_error is not None:
            raise self._read_error
        if not self._chunks:
            return b""
        head = self._chunks.pop(0)
        if amt == -1:
            return head
        return head[:amt]


class FakeConnection:
    def __init__(self, raw=None, request_error=None, response_error=None):
        self.raw = raw if raw is not None else FakeRaw()
        self.request_error = request_error
        self.response_error = response_error
        self.seen = {}
        self.close_calls = 0
        self.close_error = None

    def request(self, method, path, body=None, headers=None):
        self.seen = {"method": method, "path": path, "body": body, "headers": headers}
        if self.request_error is not None:
            raise self.request_error

    def getresponse(self):
        if self.response_error is not None:
            raise self.response_error
        return self.raw

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _factory_for(conn, probe=None):
    def factory(host, port, secure, timeout):
        if probe is not None:
            probe.update({"host": host, "port": port, "secure": secure, "timeout": timeout})
        return conn
    return factory


class PostStreamTests(unittest.TestCase):
    def test_success_forwards_exact_bytes_and_headers(self):
        conn = FakeConnection()
        probe: dict = {}
        payload = b'{"a":1}'
        headers = {"Content-Type": "application/json", "Authorization": SECRET}
        handle = post_stream(
            host="h", port=443, secure=True, path="/p",
            payload=payload, headers=headers,
            connection_factory=_factory_for(conn, probe),
        )
        self.assertEqual(handle.status, 200)
        self.assertEqual(conn.seen["method"], "POST")
        self.assertEqual(conn.seen["path"], "/p")
        self.assertEqual(conn.seen["body"], payload)
        self.assertEqual(conn.seen["headers"], headers)
        self.assertEqual(probe, {"host": "h", "port": 443, "secure": True, "timeout": 600})
        self.assertEqual(handle.read1(), b"hello")
        self.assertEqual(handle.read(6), b" world")
        handle.close()
        self.assertEqual(conn.close_calls, 1)

    def test_non200_preserves_status_and_body(self):
        conn = FakeConnection(raw=FakeRaw(status=404, chunks=(b"nope",)))
        handle = post_stream(
            host="h", port=80, secure=False, path="/r",
            payload=b"x", headers={"Authorization": SECRET},
            connection_factory=_factory_for(conn),
        )
        self.assertEqual(handle.status, 404)
        self.assertEqual(handle.read(), b"nope")
        handle.close()
        self.assertEqual(conn.close_calls, 1)

    def test_read_error_does_not_leak_and_close_idempotent(self):
        conn = FakeConnection(raw=FakeRaw(read_error=OSError("boom")))
        handle = post_stream(
            host="h", port=80, secure=False, path="/r",
            payload=b"x", headers={},
            connection_factory=_factory_for(conn),
        )
        with self.assertRaises(OSError):
            handle.read()
        handle.close()
        handle.close()
        self.assertEqual(conn.close_calls, 1)
        self.assertIsInstance(handle, HttpStreamResponse)

    def test_request_error_closes_connection(self):
        conn = FakeConnection(request_error=OSError("down"))
        with self.assertRaises(OSError):
            post_stream(
                host="h", port=80, secure=False, path="/r",
                payload=b"x", headers={},
                connection_factory=_factory_for(conn),
            )
        self.assertEqual(conn.close_calls, 1)

    def test_getresponse_error_closes_connection(self):
        conn = FakeConnection(response_error=OSError("reset"))
        with self.assertRaises(OSError):
            post_stream(
                host="h", port=80, secure=False, path="/r",
                payload=b"x", headers={},
                connection_factory=_factory_for(conn),
            )
        self.assertEqual(conn.close_calls, 1)

    def test_factory_creation_error_propagates_without_close(self):
        def factory(host, port, secure, timeout):
            raise OSError("no-socket")
        with self.assertRaises(OSError):
            post_stream(host="h", port=80, secure=False, path="/r",
                        payload=b"x", headers={}, connection_factory=factory)

    def test_repeated_close_and_close_error_tolerated(self):
        conn = FakeConnection()
        conn.close_error = OSError("close-boom")
        handle = post_stream(host="h", port=80, secure=False, path="/r",
                             payload=b"x", headers={},
                             connection_factory=_factory_for(conn))
        handle.close()
        handle.close()
        self.assertEqual(conn.close_calls, 1)

    def test_concurrent_close_closes_once(self):
        import time
        entered = threading.Event()
        release = threading.Event()
        class GatedCloseConnection(FakeConnection):
            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    entered.set()
                    release.wait(timeout=5)
                return None
        conn = GatedCloseConnection()
        handle = post_stream(host="h", port=80, secure=False, path="/r",
                             payload=b"x", headers={},
                             connection_factory=_factory_for(conn))
        workers = [threading.Thread(target=handle.close) for _ in range(2)]
        for worker in workers:
            worker.start()
        self.assertTrue(entered.wait(timeout=5))
        time.sleep(0.1)
        release.set()
        for worker in workers:
            worker.join(timeout=5)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(conn.close_calls, 1)

    def test_repr_and_errors_expose_no_secrets(self):
        conn = FakeConnection()
        handle = post_stream(
            host="h", port=80, secure=False, path="/r",
            payload=b"super-secret-bytes", headers={"Authorization": SECRET},
            connection_factory=_factory_for(conn),
        )
        text = repr(handle)
        try:
            raise ValueError("wrapped")
        except ValueError as error:
            err_text = repr(error)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("super-secret", text)
        self.assertNotIn(SECRET, err_text)
        handle.close()


if __name__ == "__main__":
    unittest.main()
