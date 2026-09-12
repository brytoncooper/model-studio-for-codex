"""Bounded subprocess adapter over ``LifecycleSession`` + ``StdioCodec``.

Spawns exactly one explicitly injected child argv with no shell, drives
``plugin.v1`` lifecycle JSON-RPC (hello / activate / drain) over its
stdio pipes, and owns that child's cleanup. Payload construction and
result acceptance stay inside :class:`LifecycleSession`, so this module
never invents wire fields or touches frozen contracts directly.

Bounds: every exchange (stdin write + stdout read) shares one monotonic
deadline; a daemon thread drains child stderr continuously so a verbose
child can never block the exchange; retained stderr is capped; response
frames are strictly validated (``jsonrpc == "2.0"``, integer ``id``
exactly matching the request, exactly one of ``result``/``error``); any
handshake/transport failure fail-closes the owned child.
"""
from __future__ import annotations

import math
import os
import select
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from model_deck.plugins.lifecycle_session import LifecycleSession
from model_deck.plugins.lifecycle_session.errors import SessionError
from model_deck.plugins.stdio_codec import CodecError, StdioCodec, encode_frame

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
class ProcessRuntime:
    """Owns one injected child process for lifecycle exchange."""

    config: ProcessRuntimeConfig
    _proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _request_id: int = field(default=0, init=False, repr=False)
    _stderr_tail: bytes = field(default=b"", init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _spawned: bool = field(default=False, init=False, repr=False)
    _in_exchange: bool = field(default=False, init=False, repr=False)
    _codec: StdioCodec = field(default_factory=StdioCodec, init=False, repr=False)
    _stderr_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stderr_chunks: Any = field(default=None, init=False, repr=False)
    _stderr_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

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

    def run_hello(self, session: LifecycleSession, nonce: str) -> dict[str, Any]:
        """Perform hello exchange and return the verified worker result."""
        try:
            params = session.prepare_hello_request(nonce)
        except SessionError as exc:
            self.close()
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.PROTOCOL,
                detail=f"hello request rejected: {exc.code}",
            ) from None
        result = self._exchange_guarded(_METHODS["hello"], params)
        try:
            session.accept_hello_result(result)
        except SessionError as exc:
            self.close()
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.PROTOCOL,
                detail=f"hello result rejected: {exc.code}",
            ) from None
        return dict(result)

    def run_activation(self, session: LifecycleSession) -> dict[str, Any]:
        """Perform activation exchange and return the worker result."""
        try:
            params = session.prepare_activation_request()
        except SessionError as exc:
            self.close()
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.PROTOCOL,
                detail=f"activation request rejected: {exc.code}",
            ) from None
        result = self._exchange_guarded(_METHODS["activate"], params)
        try:
            session.accept_activation_result(result)
        except SessionError as exc:
            self.close()
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.PROTOCOL,
                detail=f"activation result rejected: {exc.code}",
            ) from None
        return dict(result)

    def run_drain(self, session: LifecycleSession, deadline_ms: int) -> dict[str, Any]:
        """Perform drain exchange and return the worker result."""
        try:
            params = session.prepare_drain_request(deadline_ms)
        except SessionError as exc:
            self.close()
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.PROTOCOL,
                detail=f"drain request rejected: {exc.code}",
            ) from None
        result = self._exchange_guarded(_METHODS["drain"], params)
        try:
            session.accept_drain_result(result)
        except SessionError as exc:
            self.close()
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.PROTOCOL,
                detail=f"drain result rejected: {exc.code}",
            ) from None
        return dict(result)

    def close(self) -> None:
        """Terminate the owned child and reap it within a bounded wait."""
        proc, self._proc = self._proc, None
        self._closed = True
        if proc is None:
            self._snapshot_stderr_tail()
            return
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass
        finally:
            self._terminate_and_reap(proc)
        self._snapshot_stderr_tail()
        thread = self._stderr_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def _require_proc(self) -> subprocess.Popen:
        proc = self._proc
        if proc is None or self._closed:
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.TRANSPORT,
                detail="runtime is not running",
            )
        if proc.poll() is not None:
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.MALFORMED_EOF,
                detail="child exited before exchange completed",
            )
        return proc

    def _exchange_guarded(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._in_exchange:
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.TRANSPORT,
                detail="exchange already in progress",
            )
        self._in_exchange = True
        try:
            return self._exchange(method, params)
        except ProcessRuntimeError:
            self.close()
            raise
        except SessionError as exc:
            self.close()
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.PROTOCOL,
                detail=f"worker result rejected: {exc.code}",
            ) from None
        finally:
            self._in_exchange = False

    def _exchange(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        proc = self._require_proc()
        self._request_id += 1
        request_id = self._request_id
        try:
            frame = encode_frame(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
        except CodecError as exc:
            raise ProcessRuntimeError(
                code=ProcessRuntimeErrorCode.PROTOCOL,
                detail=f"request encoding failed: {exc.code}",
            ) from None
        assert proc.stdin is not None and proc.stdout is not None
        deadline = time.monotonic() + float(self.config.timeout_s)
        self._write_frame(proc, frame, deadline, method)
        stdout_fd = proc.stdout.fileno()
        seen = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.TIMEOUT,
                    detail=f"{method} exchange timed out",
                )
            if proc.poll() is not None:
                drained = self._read_available(stdout_fd, remaining)
                if drained:
                    try:
                        frames = self._codec.feed(drained)
                    except CodecError:
                        raise ProcessRuntimeError(
                            code=ProcessRuntimeErrorCode.TRANSPORT,
                            detail="child frame failed codec validation",
                        ) from None
                    seen += len(frames)
                    if seen > self.config.max_frames:
                        raise ProcessRuntimeError(
                            code=ProcessRuntimeErrorCode.FRAME_LIMIT,
                            detail="child exceeded per-exchange frame limit",
                        )
                    matched = self._match(frames, request_id, method)
                    if matched is not None:
                        return matched
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.MALFORMED_EOF,
                    detail="child stream ended without a matching response",
                )
            ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 0.05))
            if not ready:
                continue
            try:
                chunk = os.read(stdout_fd, 65536)
            except OSError:
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.TRANSPORT,
                    detail="failed reading child stdout",
                ) from None
            if not chunk:
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.MALFORMED_EOF,
                    detail="child stream ended without a matching response",
                )
            try:
                frames = self._codec.feed(chunk)
            except CodecError:
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.TRANSPORT,
                    detail="child frame failed codec validation",
                ) from None
            seen += len(frames)
            if seen > self.config.max_frames:
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.FRAME_LIMIT,
                    detail="child exceeded per-exchange frame limit",
                )
            matched = self._match(frames, request_id, method)
            if matched is not None:
                return matched

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

    @staticmethod
    def _match(
        frames: list[dict[str, Any]], request_id: int, method: str
    ) -> dict[str, Any] | None:
        matched: dict[str, Any] | None = None
        for frame in frames:
            if not isinstance(frame, dict):
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.PROTOCOL,
                    detail=f"{method} response was not an object",
                )
            if frame.get("jsonrpc") != "2.0":
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.PROTOCOL,
                    detail=f"{method} response jsonrpc was invalid",
                )
            if "id" not in frame or isinstance(frame.get("id"), bool):
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.PROTOCOL,
                    detail=f"{method} response id was invalid",
                )
            response_id = frame.get("id")
            if not isinstance(response_id, int):
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.PROTOCOL,
                    detail=f"{method} response id was invalid",
                )
            has_result = "result" in frame
            has_error = "error" in frame
            if has_result == has_error:
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.PROTOCOL,
                    detail=f"{method} response must carry exactly one of result or error",
                )
            if response_id != request_id:
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.ID_MISMATCH,
                    detail=(
                        f"{method} received unsolicited response "
                        f"id {response_id!r}"
                    ),
                )
            if has_error:
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.PROTOCOL,
                    detail=f"{method} response carried an error",
                )
            result = frame.get("result")
            if not isinstance(result, dict):
                raise ProcessRuntimeError(
                    code=ProcessRuntimeErrorCode.PROTOCOL,
                    detail=f"{method} response result was not an object",
                )
            matched = dict(result)
        return matched

    @staticmethod
    def _read_available(fd: int, remaining: float) -> bytes:
        chunks: list[bytes] = []
        end = time.monotonic() + min(max(remaining, 0.0), 0.2)
        while time.monotonic() < end:
            ready, _, _ = select.select([fd], [], [], 0.02)
            if not ready:
                break
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                break
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

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
                    assert self._stderr_chunks is not None
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
                proc.terminate()
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
