"""One owned child with bounded lifecycle, invocation, broker, and provider I/O."""
from __future__ import annotations

import copy
import math
import os
import queue
import select
import subprocess
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from typing import Any

from model_deck.plugins.lifecycle_session import LifecycleSession
from model_deck.plugins.lifecycle_session.errors import SessionError
from model_deck.plugins.stdio_codec import CodecError, StdioCodec, encode_frame

from model_deck_contracts.inventory import iter_inventory_methods
from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

from .channel import ProviderChannel, ProviderMethod
from .errors import ProcessRuntimeError, ProcessRuntimeErrorCode
from .invocation_channel import BrokerRequestHandler, InvocationChannel

_METHODS = {
    "hello": "plugin.v1.lifecycle.hello",
    "activate": "plugin.v1.lifecycle.activate",
    "drain": "plugin.v1.lifecycle.drain",
}
_INVOKE_METHOD = "plugin.v1.invoke"
_BROKER_METHOD_PREFIX = "plugin.v1.broker."
_INVENTORY_BROKER_METHODS = frozenset(
    method for method in iter_inventory_methods() if method.startswith(_BROKER_METHOD_PREFIX)
)

_MAX_STDERR_RETAINED = 1_048_576
_MAX_TIMEOUT_S = 60.0
_MAX_FRAMES = 1024
_BROKER_WORKER_COUNT = 2
_BROKER_ERROR_CODE = -32000


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


@dataclass(frozen=True)
class _BrokerRequest:
    request_id: int | str
    method: str
    params: dict[str, Any]
    deadline: float
    is_allowed_by_runtime: bool


@dataclass
class ProcessRuntime:
    """Owns one injected child process for lifecycle exchange."""

    config: ProcessRuntimeConfig
    allowed_broker_methods: InitVar[tuple[str, ...]] = field(default=(), kw_only=True)
    broker_request_handler: InitVar[BrokerRequestHandler | None] = field(
        default=None, kw_only=True
    )
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
    _allowed_broker_methods: frozenset[str] = field(
        default_factory=frozenset, init=False, repr=False
    )
    _broker_request_handler: BrokerRequestHandler | None = field(
        default=None, init=False, repr=False
    )
    _broker_requests: queue.Queue[_BrokerRequest] = field(init=False, repr=False)
    _broker_request_ids: set[int | str] = field(
        default_factory=set, init=False, repr=False
    )
    _broker_request_deadlines: dict[int | str, float] = field(
        default_factory=dict, init=False, repr=False
    )
    _broker_threads: list[threading.Thread] = field(
        default_factory=list, init=False, repr=False
    )
    _broker_deadline_thread: threading.Thread | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(
        self,
        allowed_broker_methods: tuple[str, ...],
        broker_request_handler: BrokerRequestHandler | None,
    ) -> None:
        if type(allowed_broker_methods) is not tuple:
            raise ProcessRuntimeError("protocol", "allowed broker methods must be a tuple")
        if any(
            type(method) is not str or method not in _INVENTORY_BROKER_METHODS
            for method in allowed_broker_methods
        ):
            raise ProcessRuntimeError("protocol", "allowed broker method is not in the inventory")
        if len(set(allowed_broker_methods)) != len(allowed_broker_methods):
            raise ProcessRuntimeError("protocol", "allowed broker methods contain duplicates")
        if broker_request_handler is not None and not callable(broker_request_handler):
            raise ProcessRuntimeError("protocol", "broker request handler must be callable")
        self._allowed_broker_methods = frozenset(allowed_broker_methods)
        self._broker_request_handler = broker_request_handler
        self._broker_requests = queue.Queue(maxsize=self.config.max_pending_requests)

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
                # Keep the worker out of the engine's terminal process group.
                # Cleanup remains deliberately scoped to this Popen object.
                start_new_session=(os.name == "posix"),
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
        for index in range(_BROKER_WORKER_COUNT):
            broker_thread = threading.Thread(
                target=self._service_broker_requests,
                name=f"model-deck-broker-{index + 1}",
                daemon=True,
            )
            self._broker_threads.append(broker_thread)
            broker_thread.start()
        deadline_thread = threading.Thread(
            target=self._enforce_broker_request_deadlines,
            name="model-deck-broker-deadlines",
            daemon=True,
        )
        self._broker_deadline_thread = deadline_thread
        deadline_thread.start()
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

    def invocation_channel(self) -> InvocationChannel:
        """Return a generic invocation view only for the active bound session."""
        try:
            with self._condition:
                self._require_invocation()
        except ProcessRuntimeError as failure:
            if self._activation_id is not None:
                self._stop(failure)
            raise
        return InvocationChannel(self)

    def _require_invocation(self) -> None:
        self._raise_if_closed()
        if self._activation_id is None or self._session is None:
            raise ProcessRuntimeError("protocol", "plugin activation is not established")
        if self._session.activation_id != self._activation_id:
            raise ProcessRuntimeError("protocol", "plugin activation identity changed")
        if self._session.state != "active":
            raise ProcessRuntimeError("protocol", "plugin invocation is not available")

    def _invocation_activation_id(self) -> str:
        try:
            with self._condition:
                self._require_invocation()
                assert self._activation_id is not None
                return self._activation_id
        except ProcessRuntimeError as failure:
            self._stop(failure)
            raise

    @staticmethod
    def _validate_invocation(kind: str, payload: Any) -> None:
        try:
            validate_schema_ref(
                f"contracts/plugin.v1/lifecycle/invoke.{kind}.schema.json",
                payload,
            )
        except (SchemaValidationError, ValueError, TypeError, RecursionError):
            raise ProcessRuntimeError(
                "protocol", "invocation payload failed schema validation"
            ) from None

    def _invocation_request(
        self,
        operation_id: str,
        input: Any,
        broker_context: Mapping[str, Any],
        timeout_s: float | None,
    ) -> dict[str, Any]:
        try:
            if not isinstance(broker_context, Mapping):
                raise ProcessRuntimeError("protocol", "broker context must be an object")
            try:
                payload = copy.deepcopy(
                    {
                        "operation_id": operation_id,
                        "input": input,
                        "broker_context": dict(broker_context),
                    }
                )
            except Exception:
                raise ProcessRuntimeError(
                    "protocol", "invocation params could not be copied"
                ) from None
            self._validate_invocation("params", payload)
            with self._condition:
                self._require_invocation()
                if payload["broker_context"]["activation_id"] != self._activation_id:
                    raise ProcessRuntimeError(
                        "protocol", "broker context activation does not match runtime"
                    )
            return self._exchange_guarded(_INVOKE_METHOD, payload, timeout_s)
        except ProcessRuntimeError as failure:
            self._stop(failure)
            raise

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

    def _require_broker_activation(self) -> None:
        self._raise_if_closed()
        if self._activation_id is None or self._session is None:
            raise ProcessRuntimeError("protocol", "broker activation is not established")
        if self._session.activation_id != self._activation_id:
            raise ProcessRuntimeError("protocol", "broker activation identity changed")
        if self._session.state not in ("active", "draining"):
            raise ProcessRuntimeError("protocol", "broker activation is not available")

    @staticmethod
    def _validate_broker(method: str, kind: str, payload: Any) -> None:
        if method not in _INVENTORY_BROKER_METHODS:
            raise ProcessRuntimeError("protocol", "broker method is not in the inventory")
        name = method.removeprefix(_BROKER_METHOD_PREFIX)
        try:
            validate_schema_ref(
                f"contracts/plugin.v1/broker/{name}.{kind}.schema.json",
                payload,
            )
        except (SchemaValidationError, ValueError, TypeError, RecursionError):
            raise ProcessRuntimeError(
                "protocol", "broker payload failed schema validation"
            ) from None

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
            self._broker_request_ids.clear()
            self._broker_request_deadlines.clear()
            self._condition.notify_all()
        while True:
            try:
                self._broker_requests.get_nowait()
            except queue.Empty:
                break
            else:
                self._broker_requests.task_done()
        try:
            if proc is not None:
                self._terminate_and_reap(proc)
                if proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except (OSError, ValueError):
                        pass
            threads = (
                self._reader_thread,
                self._stderr_thread,
                self._broker_deadline_thread,
                *self._broker_threads,
            )
            for thread in threads:
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=0.2 if thread in self._broker_threads else 1.0)
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
                    responses, events, broker_requests = [], [], []
                    batch_ids = set()
                    batch_broker_ids = set()
                    byte_count = self._event_bytes
                    for pending in self._pending.values():
                        if (
                            pending.method.startswith("plugin.v1.lifecycle.")
                            or pending.method == _INVOKE_METHOD
                        ):
                            pending.frames_seen += len(frames)
                            if pending.frames_seen > self.config.max_frames:
                                raise ProcessRuntimeError(
                                    "frame_limit", "lifecycle frame limit exceeded"
                                )
                    for frame in frames:
                        if frame.get("jsonrpc") != "2.0":
                            raise ProcessRuntimeError("protocol", "invalid JSON-RPC envelope")
                        if "method" in frame:
                            if "id" in frame:
                                if set(frame) != {"jsonrpc", "id", "method", "params"}:
                                    raise ProcessRuntimeError(
                                        "protocol", "invalid worker request envelope"
                                    )
                                broker_request_id = frame["id"]
                                if type(broker_request_id) not in (int, str):
                                    raise ProcessRuntimeError(
                                        "protocol", "worker request id must be an integer or string"
                                    )
                                if (
                                    broker_request_id in self._broker_request_ids
                                    or broker_request_id in batch_broker_ids
                                ):
                                    raise ProcessRuntimeError(
                                        "id_mismatch", "duplicate worker request id"
                                    )
                                if (
                                    len(self._broker_request_ids) + len(batch_broker_ids)
                                    >= self.config.max_pending_requests
                                ):
                                    raise ProcessRuntimeError(
                                        "frame_limit", "pending broker request limit exceeded"
                                    )
                                self._require_broker_activation()
                                method = frame["method"]
                                self._validate_broker(method, "params", frame["params"])
                                try:
                                    params = copy.deepcopy(dict(frame["params"]))
                                except Exception:
                                    raise ProcessRuntimeError(
                                        "protocol", "broker params could not be copied"
                                    ) from None
                                batch_broker_ids.add(broker_request_id)
                                broker_requests.append(
                                    _BrokerRequest(
                                        request_id=broker_request_id,
                                        method=method,
                                        params=params,
                                        deadline=time.monotonic()
                                        + float(self.config.timeout_s),
                                        is_allowed_by_runtime=(
                                            method in self._allowed_broker_methods
                                        ),
                                    )
                                )
                                continue
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
                        elif pending.method == _INVOKE_METHOD:
                            self._validate_invocation("result", frame["result"])
                        batch_ids.add(request_id)
                        responses.append((pending, frame["result"]))
                    for broker_request in broker_requests:
                        self._broker_request_ids.add(broker_request.request_id)
                        self._broker_request_deadlines[
                            broker_request.request_id
                        ] = broker_request.deadline
                        try:
                            self._broker_requests.put_nowait(broker_request)
                        except queue.Full:
                            raise ProcessRuntimeError(
                                "frame_limit", "pending broker request queue exceeded"
                            ) from None
                    for pending, result in responses:
                        pending.result = result
                    self._events.extend(events)
                    self._event_bytes = byte_count
                    self._condition.notify_all()
        except ProcessRuntimeError as failure:
            self._stop(failure)
        except (OSError, ValueError, CodecError):
            self._stop(ProcessRuntimeError("transport", "child frame transport failed"))

    def _enforce_broker_request_deadlines(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
                if not self._broker_request_deadlines:
                    self._condition.wait()
                    continue
                next_deadline = min(self._broker_request_deadlines.values())
                remaining = next_deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
            self._stop(ProcessRuntimeError("timeout", "broker request timed out"))
            return

    def _service_broker_requests(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
            try:
                request = self._broker_requests.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self._service_broker_request(request)
            except Exception:
                self._stop(
                    ProcessRuntimeError("transport", "broker request worker failed")
                )
            finally:
                self._broker_requests.task_done()

    def _service_broker_request(self, request: _BrokerRequest) -> None:
        try:
            with self._condition:
                self._require_broker_activation()
                activation_id = self._activation_id
                handler = self._broker_request_handler
            if not request.is_allowed_by_runtime or handler is None:
                raise ProcessRuntimeError("protocol", "broker request denied")
            assert activation_id is not None
            try:
                result = handler(activation_id, request.method, request.params)
            except Exception:
                raise ProcessRuntimeError(
                    "protocol", "broker request handler rejected request"
                ) from None
            if time.monotonic() >= request.deadline:
                raise ProcessRuntimeError("timeout", "broker request timed out")
            if not isinstance(result, Mapping):
                raise ProcessRuntimeError("protocol", "broker result must be an object")
            try:
                payload = copy.deepcopy(dict(result))
            except Exception:
                raise ProcessRuntimeError(
                    "protocol", "broker result could not be copied"
                ) from None
            self._validate_broker(request.method, "result", payload)
            self._send_broker_response(request, result=payload)
        except ProcessRuntimeError as failure:
            self._send_broker_error(request)
            self._stop(failure)

    def _send_broker_response(
        self,
        request: _BrokerRequest,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if result is None and error is None:
            raise ProcessRuntimeError("protocol", "broker response is empty")
        if result is not None and error is not None:
            raise ProcessRuntimeError("protocol", "broker response is ambiguous")
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": request.request_id}
        if result is not None:
            response["result"] = result
        else:
            response["error"] = error
        try:
            frame = encode_frame(response)
        except CodecError as failure:
            code = "frame_limit" if failure.code == "frame_too_large" else "protocol"
            raise ProcessRuntimeError(
                code, "broker response could not be encoded"
            ) from None
        remaining = request.deadline - time.monotonic()
        if remaining <= 0:
            raise ProcessRuntimeError("timeout", "broker response timed out")
        if not self._write_lock.acquire(timeout=remaining):
            raise ProcessRuntimeError("timeout", "broker response write timed out")
        try:
            with self._condition:
                if self._closed:
                    return
                self._require_broker_activation()
                proc = self._proc
                self._broker_request_deadlines.pop(request.request_id, None)
                self._condition.notify_all()
            assert proc is not None
            self._write_frame(proc, frame, request.deadline, request.method)
            with self._condition:
                self._broker_request_ids.discard(request.request_id)
                self._condition.notify_all()
        finally:
            self._write_lock.release()

    def _send_broker_error(self, request: _BrokerRequest) -> None:
        try:
            self._send_broker_response(
                request,
                error={
                    "code": _BROKER_ERROR_CODE,
                    "message": "broker request denied",
                },
            )
        except (ProcessRuntimeError, OSError, ValueError, CodecError):
            return

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
