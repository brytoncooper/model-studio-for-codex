from __future__ import annotations

import fcntl
from pathlib import Path


class FileInstanceLock:
    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._handle = None

    def acquire(self, timeout_seconds: float) -> bool:
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

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
