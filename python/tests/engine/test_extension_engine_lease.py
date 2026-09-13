from __future__ import annotations

import os
import signal
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from model_deck.adapters.platform.macos.extension_lease import (
    ExtensionEngineLease,
    ExtensionEngineLeaseBindingError,
    ExtensionEngineLeaseProcessError,
)
from model_deck.adapters.platform.macos.instance_lock import (
    FileInstanceLock,
    InstanceLockNotHeldError,
    InstanceLockProcessError,
)
from model_deck.adapters.storage.sqlite_extension_lifecycle import (
    SQLiteExtensionLifecycleRepository,
)
from model_deck.engine.extensions.service import (
    HeldExclusiveEngineLease,
    LifecycleExecutionConflictError,
)


OPERATION_ID = "40000000-0000-4000-8000-000000000001"
OTHER_OPERATION_ID = "40000000-0000-4000-8000-000000000002"


class ExtensionEngineLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        root = Path(self._temporary_directory.name)
        self.lock_path = root / "engine" / "instance.lock"
        self.instance_lock = FileInstanceLock(self.lock_path)
        self.repository = SQLiteExtensionLifecycleRepository(root / "state.sqlite3")
        self.lease = ExtensionEngineLease(self.instance_lock, self.repository)

    def tearDown(self) -> None:
        try:
            self.instance_lock.release()
        finally:
            self._temporary_directory.cleanup()

    def test_file_lock_double_acquire_is_idempotent_and_release_does_not_leak(self) -> None:
        self.assertTrue(self.instance_lock.acquire(0.0))
        self.instance_lock.assert_held()
        self.assertTrue(self.instance_lock.acquire(0.0))
        self.instance_lock.assert_held()

        self.instance_lock.release()
        self.instance_lock.release()
        with self.assertRaises(InstanceLockNotHeldError):
            self.instance_lock.assert_held()

        replacement = FileInstanceLock(self.lock_path)
        try:
            self.assertTrue(replacement.acquire(0.0))
            replacement.assert_held()
        finally:
            replacement.release()

    def test_lease_requires_held_lock_and_exact_repository_identity(self) -> None:
        self.assertIsInstance(self.lease, HeldExclusiveEngineLease)
        with self.assertRaises(InstanceLockNotHeldError):
            self.lease.assert_held_for(self.repository)

        self.assertTrue(self.instance_lock.acquire(0.0))
        self.lease.assert_held_for(self.repository)
        other_repository = SQLiteExtensionLifecycleRepository(
            Path(self._temporary_directory.name) / "other.sqlite3"
        )
        with self.assertRaises(ExtensionEngineLeaseBindingError):
            self.lease.assert_held_for(other_repository)

        self.instance_lock.release()
        with self.assertRaises(InstanceLockNotHeldError):
            self.lease.assert_held_for(self.repository)
        with self.assertRaises(InstanceLockNotHeldError):
            with self.lease.execution_owner(self.repository, OPERATION_ID):
                pass

    def test_shared_lease_rejects_concurrent_owner_for_same_operation(self) -> None:
        self.assertTrue(self.instance_lock.acquire(0.0))
        entered = threading.Event()
        release_owner = threading.Event()

        def hold_owner() -> None:
            with self.lease.execution_owner(self.repository, OPERATION_ID):
                entered.set()
                if not release_owner.wait(timeout=5):
                    raise AssertionError("timed out waiting to release operation owner")

        with ThreadPoolExecutor(max_workers=1) as executor:
            owner = executor.submit(hold_owner)
            self.assertTrue(entered.wait(timeout=5))
            try:
                with self.assertRaises(LifecycleExecutionConflictError):
                    with self.lease.execution_owner(self.repository, OPERATION_ID):
                        pass
                with self.lease.execution_owner(
                    self.repository,
                    OTHER_OPERATION_ID,
                ):
                    pass
            finally:
                release_owner.set()
            owner.result(timeout=5)

        with self.lease.execution_owner(self.repository, OPERATION_ID):
            pass

    def test_wrappers_over_same_file_lock_share_operation_registry(self) -> None:
        second_lease = ExtensionEngineLease(self.instance_lock, self.repository)
        self.assertTrue(self.instance_lock.acquire(0.0))

        with self.lease.execution_owner(self.repository, OPERATION_ID):
            with self.assertRaises(LifecycleExecutionConflictError):
                with second_lease.execution_owner(self.repository, OPERATION_ID):
                    pass

        with second_lease.execution_owner(self.repository, OPERATION_ID):
            pass

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork")
    def test_forked_copy_is_rejected_and_cannot_compete_for_parent_lock(self) -> None:
        self.assertTrue(self.instance_lock.acquire(0.0))
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            results: list[str] = []
            try:
                try:
                    self.instance_lock.assert_held()
                except InstanceLockProcessError:
                    results.append("1")
                else:
                    results.append("0")
                try:
                    self.instance_lock.release()
                except InstanceLockProcessError:
                    results.append("1")
                else:
                    results.append("0")
                try:
                    self.lease.assert_held_for(self.repository)
                except ExtensionEngineLeaseProcessError:
                    results.append("1")
                else:
                    results.append("0")
                competitor = FileInstanceLock(self.lock_path)
                acquired = competitor.acquire(0.0)
                results.append("0" if acquired else "1")
                if acquired:
                    competitor.release()
                os.write(write_fd, "".join(results).encode("ascii"))
            finally:
                os.close(write_fd)
                os._exit(0)

        os.close(write_fd)
        try:
            status = self._wait_for_child(child_pid)
            result = os.read(read_fd, 16)
        finally:
            os.close(read_fd)
        self.assertEqual(status, 0)
        self.assertEqual(result, b"1111")
        self.instance_lock.assert_held()
        self.lease.assert_held_for(self.repository)

    @staticmethod
    def _wait_for_child(child_pid: int) -> int:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            completed_pid, status = os.waitpid(child_pid, os.WNOHANG)
            if completed_pid == child_pid:
                return os.waitstatus_to_exitcode(status)
            time.sleep(0.01)
        os.kill(child_pid, signal.SIGKILL)
        os.waitpid(child_pid, 0)
        raise AssertionError("forked lock probe timed out")


if __name__ == "__main__":
    unittest.main()
