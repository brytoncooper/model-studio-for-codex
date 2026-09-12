"""One owned child, one stdout reader, bounded lifecycle/provider exchanges."""
from __future__ import annotations

import copy
import math
import os
import select
import subprocess
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from model_deck.plugins.lifecycle_session import LifecycleSession
from model_deck.plugins.lifecycle_session.errors import SessionError
from model_deck.plugins.stdio_codec import CodecError, StdioCodec, encode_frame

from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

from .channel import ProviderChannel, ProviderMethod
from .errors import ProcessRuntimeError, ProcessRuntimeErrorCode

_METHODS = {
    "hello": "plugin.v1.lifecycle.hello",
    "activate": "plugin.v1.lifecycle.activate",
    "drain": "plugin.v1.lifecycle.drain",
}

_MAX_STDERR_RETAINED = 1_048_576
_MAX_TIMEOUT_S = 60.0
_MAX_FRAMES = 1024


@dataclass(frozen=True)
class ProcessRuntimeConfig:
    """Explicit launch + bound configuration.

    Args:
        argv: Explicit executable argv; used verbatim, never shelled.
        package_dir: Explicit child working directory.
        timeout_s: Per-exchange deadline in seconds.
        max_frames: Max frames accepted per single exchange.
        max_stderr_bytes: Cap on stderr bytes retained for bounded drain.
    """

    argv: tuple[str, ...]
    package_dir: str
    timeout_s: float = 5.0
    max_frames: int = 16
    max_stderr_bytes: int = 65536
    max_pending_requests: int = 16

    def __post_init__(self) -> None:
        if not isinstance(self.argv, (tuple, list)) or not self.argv:
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.SPAWN_FAILED,
                detail="argv must be a non-empty list of strings",
            )
        for entry in self.argv:
            if not isinstance(entry, str) or not entry:
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.SPAWN_FAILED,
                    detail="argv must be a non-empty list of strings",
                )
        if not isinstance(self.package_dir, str) or not os.path.isdir(self.package_dir):
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.SPAWN_FAILED,
                detail="package_dir must be an existing directory",
            )
        if type(self.max_pending_requests) is not int or not 1 <= self.max_pending_requests <= 1024:
            raise ProcessRuntimeError("protocol", "invalid pending request limit")
        timeout = self.timeout_s
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.SPAWN_FAILED,
                detail="timeout_s must be a positive number of seconds",
            )
        if not math.isfinite(float(timeout)) or not 0 < float(timeout) <= _MAX_TIMEOUT_S:
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.SPAWN_FAILED,
                detail="timeout_s must be a positive number of seconds",
            )
        if isinstance(self.max_frames, bool) or not isinstance(self.max_frames, int):
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.SPAWN_FAILED,
                detail="max_frames must be a positive integer",
            )
        if not 1 <= self.max_frames <= _MAX_FRAMES:
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.SPAWN_FAILED,
                detail="max_frames must be a positive integer",
            )
        if isinstance(self.max_stderr_bytes, bool) or not isinstance(
            self.max_stderr_bytes, int
        ):
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.SPAWN_FAILED,
                detail="max_stderr_bytes must be a non-negative integer",
            )
        if not 0 <= self.max_stderr_bytes <= _MAX_STDERR_RETAINED:
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.SPAWN_FAILED,
                detail="max_stderr_bytes must be a non-negative integer",
            )


@dataclass
class _PendingRequest:
    method: str
    result: dict[str, Any] | None = None
    frames_seen: int = 0


@dataclass
class ProcessRuntime:
    """Owns one injected child process for lifecycle exchange."""

    config: ProcessRuntimeConfig
    _proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _request_id: int = field(default=0, init=False, repr=False)
    _stderr_tail: bytes = field(default=b"", init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _spawned: bool = field(default=False, init=False, repr=False)
    _codec: StdioCodec = field(default_factory=StdioCodec, init=False, repr=False)
    _stderr_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stderr_chunks: Any = field(default=None, init=False, repr=False)
    _stderr_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    _condition: threading.Condition = field(default_factory=threading.Condition, init=False, repr=False)
    _write_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _lifecycle_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _cleanup_done: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _reader_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _failure: ProcessRuntimeError | None = field(default=None, init=False, repr=False)
    _pending: dict[int, _PendingRequest] = field(default_factory=dict, init=False, repr=False)
    _events: Any = field(default_factory=deque, init=False, repr=False)
    _event_bytes: int = field(default=0, init=False, repr=False)
    _session: LifecycleSession | None = field(default=None, init=False, repr=False)
    _activation_id: str | None = field(default=None, init=False, repr=False)

    def __repr__(self) -> str:
        return f"ProcessRuntime(closed={self._closed!r})"

    @property
    def stderr_bytes_drained(self) -> int:
        """Bounded stderr bytes retained at last cleanup."""
        with self._stderr_lock:
            if self._stderr_chunks is not None:
                return sum(len(c) for c in self._stderr_chunks)
            return len(self._stderr_tail)

    def spawn(self) -> None:
        """Launch the injected child; no shell, no discovery."""
        with self._condition:
            self._spawn()

    def _spawn(self) -> None:
        if self._spawned or self._proc is not None or self._closed:
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.TRANSPORT,
                detail="runtime already spawned",
            )
        argv = self.config.argv
        if not argv or any(not isinstance(a, str) or not a for a in argv):
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.SPAWN_FAILED,
                detail="argv must be a non-empty list of strings",
            )
        if not isinstance(self.config.package_dir, str) or not os.path.isdir(
            self.config.package_dir
        ):
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.SPAWN_FAILED,
                detail="package_dir must be an existing directory",
            )
        try:
            self._proc = subprocess.Popen(
                list(argv),
                cwd=self.config.package_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
            )
        except (OSError, ValueError):
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.SPAWN_FAILED,
                detail="spawn failed",
            ) from None
        self._spawned = True
        import fcntl as _fcntl
        for _stream in (self._proc.stdin, self._proc.stdout):
            if _stream is not None:
                try:
                    _fd = _stream.fileno()
                    _flags = _fcntl.fcntl(_fd, _fcntl.F_GETFL)
                    _fcntl.fcntl(_fd, _fcntl.F_SETFL, _flags | os.O_NONBLOCK)
                except (OSError, ValueError):
                    pass
        self._stderr_chunks = deque()
        thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread = thread
        thread.start()
        reader = threading.Thread(target=self._read_stdout, args=(self._proc,), daemon=True)
        self._reader_thread = reader
        reader.start()

    def run_hello(self, session: LifecycleSession, nonce: str) -> dict[str, Any]:
        return self._lifecycle_exchange(session, "hello", nonce)

    def run_activation(self, session: LifecycleSession) -> dict[str, Any]:
        return self._lifecycle_exchange(session, "activate")

    def run_drain(self, session: LifecycleSession, deadline_ms: int) -> dict[str, Any]:
        return self._lifecycle_exchange(session, "drain", deadline_ms)

    def _lifecycle_exchange(self, session, operation, argument=None):
        if not self._lifecycle_lock.acquire(blocking=False):
            raise ProcessRuntimeError("transport", "lifecycle exchange already in progress")
        try:
            with self._condition:
                self._raise_if_closed()
                if operation == "hello":
                    if self._session is not None:
                        raise ProcessRuntimeError("protocol", "hello already performed")
                    self._session = session
                elif self._session is not session:
                    raise ProcessRuntimeError("protocol", "lifecycle session does not match runtime")
            if operation == "hello":
                params = session.prepare_hello_request(argument)
            elif operation == "activate":
                params = session.prepare_activation_request()
            else:
                params = session.prepare_drain_request(argument)
            result = self._exchange_guarded(_METHODS[operation], params)
            with self._condition:
                self._raise_if_closed()
                if operation == "hello":
                    session.accept_hello_result(result)
                elif operation == "activate":
                    session.accept_activation_result(result)
                    self._activation_id = session.activation_id
                else:
                    session.accept_drain_result(result)
            return result
        except SessionError:
            self.close()
            raise ProcessRuntimeError("protocol", "lifecycle exchange rejected") from None
        except ProcessRuntimeError:
            self.close()
            raise
        finally:
            self._lifecycle_lock.release()

    def provider_channel(self) -> ProviderChannel:
        try:
            with self._condition:
                self._require_provider()
        except ProcessRuntimeError as failure:
            if self._activation_id is not None:
                self._stop(failure)
            raise
        return ProviderChannel(self)

    def _require_provider(self, method=None):
        self._raise_if_closed()
        if self._activation_id is None or self._session is None:
            raise ProcessRuntimeError("protocol", "provider activation is not established")
        if self._session.activation_id != self._activation_id:
            raise ProcessRuntimeError("protocol", "provider activation identity changed")
        allowed = ("active", "draining")
        if self._session.state not in allowed:
            raise ProcessRuntimeError("protocol", "provider activation is not available")
        if self._session.state == "draining" and method in (ProviderMethod.START, ProviderMethod.RESUME):
            raise ProcessRuntimeError("protocol", "provider activation is draining")

    def _provider_activation_id(self):
        try:
            with self._condition:
                self._require_provider()
                return self._activation_id
        except ProcessRuntimeError as failure:
            self._stop(failure)
            raise

    @staticmethod
    def _validate_provider(method, kind, payload):
        name = method.rsplit(".", 1)[-1]
        try:
            validate_schema_ref(f"contracts/plugin.v1/provider/{name}.{kind}.schema.json", payload)
        except (SchemaValidationError, ValueError, TypeError, RecursionError):
            raise ProcessRuntimeError("protocol", "provider payload failed schema validation") from None

    def _provider_request(self, method, params, timeout_s):
        try:
            if not isinstance(method, str) or method not in tuple(ProviderMethod):
                raise ProcessRuntimeError("protocol", "provider method is not allowed")
            method = ProviderMethod(method).value
            if not isinstance(params, Mapping):
                raise ProcessRuntimeError("protocol", "provider params must be an object")
            try:
                payload = copy.deepcopy(dict(params))
            except Exception:
                raise ProcessRuntimeError("protocol", "provider params could not be copied") from None
            self._validate_provider(method, "params", payload)
            with self._condition:
                self._require_provider(method)
            return self._exchange_guarded(method, payload, timeout_s)
        except ProcessRuntimeError:
            self.close()
            raise

    def _receive_provider_event(self, timeout_s):
        try:
            deadline = time.monotonic() + self._timeout(timeout_s, allow_zero=True)
            with self._condition:
                while True:
                    self._require_provider()
                    if self._events:
                        event, size = self._events.popleft()
                        self._event_bytes -= size
                        return event
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._condition.wait(remaining)
        except ProcessRuntimeError as failure:
            self._stop(failure)
            raise

    def _raise_if_closed(self):
        if self._failure is not None:
            raise ProcessRuntimeError(self._failure.code, self._failure.detail)
        if self._closed or self._proc is None:
            raise ProcessRuntimeError("transport", "runtime is not running")

    def close(self) -> None:
        self._stop(ProcessRuntimeError("transport", "runtime closed"))
        if threading.current_thread() not in (self._reader_thread, self._stderr_thread):
            self._cleanup_done.wait(min(float(self.config.timeout_s), 5.0) + 7.0)

    def _stop(self, failure):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._failure = failure
            proc, self._proc = self._proc, None
            self._events.clear()
            self._event_bytes = 0
            self._condition.notify_all()
        try:
            if proc is not None:
                self._terminate_and_reap(proc)
                if proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except (OSError, ValueError):
                        pass
            for thread in (self._reader_thread, self._stderr_thread):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=1.0)
            self._snapshot_stderr_tail()
        finally:
            self._cleanup_done.set()

    def _timeout(self, timeout_s, *, allow_zero=False):
        value = self.config.timeout_s if timeout_s is None else timeout_s
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or value > _MAX_TIMEOUT_S or value < 0 or not math.isfinite(value)
                or (value == 0 and not allow_zero)):
            raise ProcessRuntimeError("protocol", "invalid request timeout")
        return float(value)

    def _exchange_guarded(self, method, params, timeout_s=None):
        try:
            return self._exchange(method, params, timeout_s)
        except ProcessRuntimeError as failure:
            self._stop(failure)
            self._cleanup_done.wait(min(float(self.config.timeout_s), 5.0) + 7.0)
            raise
        except (OSError, ValueError, CodecError):
            failure = ProcessRuntimeError("transport", "process transport failed")
            self._stop(failure)
            self._cleanup_done.wait(min(float(self.config.timeout_s), 5.0) + 7.0)
            raise failure from None

    def _exchange(self, method, params, timeout_s=None):
        deadline = time.monotonic() + self._timeout(timeout_s)
        with self._condition:
            self._raise_if_closed()
            if len(self._pending) >= self.config.max_pending_requests:
                raise ProcessRuntimeError("frame_limit", "pending request limit exceeded")
            self._request_id += 1
            request_id = self._request_id
            pending = _PendingRequest(method)
            self._pending[request_id] = pending
            proc = self._proc
        try:
            frame = encode_frame({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            if not self._write_lock.acquire(timeout=max(0, deadline - time.monotonic())):
                raise ProcessRuntimeError("timeout", "request write timed out")
            try:
                with self._condition:
                    self._raise_if_closed()
                    if method.startswith("plugin.v1.provider."):
                        self._require_provider(method)
                self._write_frame(proc, frame, deadline, method)
            finally:
                self._write_lock.release()
            with self._condition:
                while True:
                    self._raise_if_closed()
                    if pending.result is not None:
                        return pending.result
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProcessRuntimeError("timeout", "request response timed out")
                    self._condition.wait(remaining)
        finally:
            with self._condition:
                self._pending.pop(request_id, None)

    def _read_stdout(self, proc):
        try:
            while True:
                with self._condition:
                    if self._closed:
                        return
                ready, _, _ = select.select([proc.stdout], [], [], 0.05)
                if not ready:
                    continue
                try:
                    chunk = os.read(proc.stdout.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    raise ProcessRuntimeError("malformed_eof", "child stdout ended")
                frames = self._codec.feed(chunk)
                with self._condition:
                    # Commit the batch only after every frame validates. A valid
                    # hello bundled with an unknown reply must never succeed.
                    responses, events = [], []
                    batch_ids = set()
                    byte_count = self._event_bytes
                    for pending in self._pending.values():
                        if pending.method.startswith("plugin.v1.lifecycle."):
                            pending.frames_seen += len(frames)
                            if pending.frames_seen > self.config.max_frames:
                                raise ProcessRuntimeError("frame_limit", "lifecycle frame limit exceeded")
                    for frame in frames:
                        if frame.get("jsonrpc") != "2.0":
                            raise ProcessRuntimeError("protocol", "invalid JSON-RPC envelope")
                        if "method" in frame:
                            if (set(frame) != {"jsonrpc", "method", "params"}
                                    or frame["method"] != "plugin.v1.provider.event"):
                                raise ProcessRuntimeError("protocol", "worker notification is not allowed")
                            self._require_provider()
                            self._validate_provider(frame["method"], "params", frame["params"])
                            size = len(encode_frame(frame))
                            byte_count += size
                            if len(self._events) + len(events) >= 256 or byte_count > 1_048_576:
                                raise ProcessRuntimeError("frame_limit", "provider event queue limit exceeded")
                            events.append((frame["params"], size))
                            continue
                        request_id = frame.get("id")
                        if type(request_id) is not int:
                            raise ProcessRuntimeError("protocol", "response id must be an integer")
                        if set(frame) not in ({"jsonrpc", "id", "result"}, {"jsonrpc", "id", "error"}):
                            raise ProcessRuntimeError("protocol", "invalid response envelope")
                        pending = self._pending.get(request_id)
                        if pending is None or pending.result is not None or request_id in batch_ids:
                            raise ProcessRuntimeError("id_mismatch", "unknown or duplicate response id")
                        if "error" in frame or not isinstance(frame.get("result"), dict):
                            raise ProcessRuntimeError("protocol", "worker request failed")
                        if pending.method.startswith("plugin.v1.provider."):
                            self._validate_provider(pending.method, "result", frame["result"])
                        batch_ids.add(request_id)
                        responses.append((pending, frame["result"]))
                    for pending, result in responses:
                        pending.result = result
                    self._events.extend(events)
                    self._event_bytes = byte_count
                    self._condition.notify_all()
        except ProcessRuntimeError as failure:
            self._stop(failure)
        except (OSError, ValueError, CodecError):
            self._stop(ProcessRuntimeError("transport", "child frame transport failed"))

    def _write_frame(
        self, proc: subprocess.Popen, frame: bytes, deadline: float, method: str
    ) -> None:
        assert proc.stdin is not None
        fd = proc.stdin.fileno()
        view = memoryview(frame)
        offset = 0
        while offset < len(frame):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.TIMEOUT,
                    detail=f"{method} exchange timed out",
                )
            if proc.poll() is not None:
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.MALFORMED_EOF,
                    detail="child exited before exchange completed",
                )
            _, writable, _ = select.select([], [proc.stdin], [], min(remaining, 0.05))
            if not writable:
                continue
            try:
                written = os.write(fd, view[offset:])
            except BlockingIOError:
                continue
            except OSError:
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.TRANSPORT,
                    detail="failed writing to child stdin",
                ) from None
            if written <= 0:
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.TRANSPORT,
                    detail="failed writing to child stdin",
                ) from None
            offset += written
        try:
            proc.stdin.flush()
        except (OSError, ValueError):
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.TRANSPORT,
                detail="failed writing to child stdin",
            ) from None

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        fd = proc.stderr.fileno()
        cap = max(0, int(self.config.max_stderr_bytes))
        try:
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                if cap <= 0:
                    continue
                with self._stderr_lock:
                    if self._stderr_chunks is None:
                        return
                    self._stderr_chunks.append(chunk)
                    total = sum(len(c) for c in self._stderr_chunks)
                    while total > cap and self._stderr_chunks:
                        dropped = self._stderr_chunks.popleft()
                        total -= len(dropped)
                    if total > cap:
                        tail = b"".join(self._stderr_chunks)[-cap:]
                        self._stderr_chunks = deque([tail])
        finally:
            pass

    def _snapshot_stderr_tail(self) -> None:
        with self._stderr_lock:
            if self._stderr_chunks is not None:
                self._stderr_tail = b"".join(self._stderr_chunks)
                self._stderr_chunks = None

    def _terminate_and_reap(self, proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=min(float(self.config.timeout_s), 5.0))
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except (OSError, ValueError):
                    pass
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            try:
                if proc.stderr is not None:
                    try:
                        proc.stderr.close()
                    except (OSError, ValueError):
                        pass
            except (OSError, ValueError):
                pass
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except (OSError, ValueError):
                pass
