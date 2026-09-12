import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.editing_check as editing_check
from development.guard.paths import repo_root


def architecture_repo() -> Path:
    return repo_root(Path(__file__).resolve().parent)


class EditingCheckTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> int:
        return editing_check.main(argv)

    def test_rejects_extra_arguments(self):
        self.assertEqual(self._run(["--files", "test_development_guard.py", "--", "noop"]), 2)

    def test_rejects_more_than_three_files(self):
        files = [
            "test_development_guard.py",
            "test_editing_check.py",
            "test_pricing.py",
            "test_codex_runtime.py",
        ]
        self.assertEqual(self._run(["--files", *files]), 2)

    def test_rejects_glob_in_files(self):
        self.assertEqual(self._run(["--files", "test_*.py"]), 2)

    def test_rejects_non_test_module(self):
        self.assertEqual(self._run(["--files", "local_router.py"]), 2)

    def test_runs_owned_unittest_modules(self):
        self.assertEqual(self._run(["--files", "test_development_guard.py"]), 0)

    def test_repository_root_ignores_caller_cwd(self):
        root = architecture_repo()
        previous = os.getcwd()
        try:
            os.chdir(root.parent)
            resolved = editing_check._repository_root()
        finally:
            os.chdir(previous)
        self.assertEqual(resolved, root)

    def test_timeout_kills_entire_process_tree(self):
        root = architecture_repo()
        fixture = root / "development/fixtures/ignore_sigterm_tree.py"
        with tempfile.TemporaryDirectory() as state_dir:
            marker = Path(state_dir) / "pids.txt"
            with mock.patch.object(editing_check, "DEADLINE_SECONDS", 4):
                with mock.patch.object(editing_check, "CLEANUP_RESERVE_SECONDS", 1.0):
                    with self.assertRaises(editing_check.EditingCheckInconclusiveError):
                        editing_check.run_bounded(
                            [sys.executable, str(fixture), str(marker)],
                            root,
                            Path(state_dir),
                        )
            pids: list[int] = []
            for line in marker.read_text(encoding="utf-8").splitlines():
                _, pid_text = line.split(":", 1)
                pids.append(int(pid_text))
            self.assertGreaterEqual(len(pids), 3)
            for pid in pids:
                self.assertFalse(editing_check.pid_alive(pid), f"pid {pid} still alive")

    def test_output_limit_is_inconclusive(self):
        root = architecture_repo()
        huge = "print('x' * 300000)"
        with tempfile.TemporaryDirectory() as state_dir:
            with mock.patch.object(editing_check, "MAX_OUTPUT_BYTES", 1024):
                with self.assertRaises(editing_check.EditingCheckInconclusiveError):
                    editing_check.run_bounded(
                        [sys.executable, "-c", huge],
                        root,
                        Path(state_dir),
                    )

if __name__ == "__main__":
    unittest.main()
