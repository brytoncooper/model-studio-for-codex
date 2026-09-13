from __future__ import annotations

import fcntl
import os
import threading
from pathlib import Path
from typing import TextIO


class InstanceLockNotHeldError(RuntimeError):
    pass


class InstanceLockProcessError(RuntimeError):
    pass


class FileInstanceLock:
    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._handle: TextIO | None = None
        self._process_id = os.getpid()
        self._state_lock = threading.RLock()

    def acquire(self, timeout_seconds: float) -> bool:
        self._assert_original_process()
        with self._state_lock:
            if self._handle is not None:
                self.assert_held()
                return True
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._lock_path.open("w")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return False
            handle.write("locked" + chr(10))
            handle.flush()
            self._handle = handle
            return True

    def assert_held(self) -> None:
        self._assert_original_process()
        with self._state_lock:
            if self._handle is None or self._handle.closed:
                raise InstanceLockNotHeldError("instance lock is not held")

    def release(self) -> None:
        if os.getpid() != self._process_id:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            raise InstanceLockProcessError(
                "instance lock cannot be released by a forked process"
            )
        with self._state_lock:
            if self._handle is None:
                return
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None

    def _assert_original_process(self) -> None:
        if os.getpid() != self._process_id:
            raise InstanceLockProcessError(
                "instance lock cannot be used by a forked process"
            )
