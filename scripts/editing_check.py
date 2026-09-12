#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import fcntl
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve()
_REPO_ROOT = _SCRIPT_PATH.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from development.guard.env import isolated_subprocess_env
from development.guard.paths import (
    DevelopmentGuardError,
    repo_root_from_script,
    unittest_module_name,
)

MAX_FILES = 3
DEADLINE_SECONDS = 30
CLEANUP_RESERVE_SECONDS = 2.0
MAX_OUTPUT_BYTES = 256 * 1024

EXIT_UNSUPPORTED = 2
EXIT_INCONCLUSIVE = 4


class EditingCheckError(ValueError):
    pass


class EditingCheckInconclusiveError(OSError):
    pass


def _repository_root() -> Path:
    return repo_root_from_script(_SCRIPT_PATH)


def parse_file_args(argv: list[str]) -> list[Path]:
    parser = argparse.ArgumentParser(prog="editing_check.py", add_help=False)
    parser.add_argument("--files", nargs="+", required=True)
    ns, rest = parser.parse_known_args(argv)
    if rest:
        raise EditingCheckError(f"unsupported arguments: {rest}")
    return [Path(item) for item in ns.files]


def validate_files(files: list[Path], root: Path) -> list[Path]:
    if not files or len(files) > MAX_FILES:
        raise EditingCheckError(f"--files requires 1 to {MAX_FILES} exact paths")
    resolved: list[Path] = []
    for path in files:
        text = str(path)
        if any(ch in text for ch in "*?[]{"):
            raise EditingCheckError(f"globs are not allowed in --files: {path}")
        if not path.is_absolute():
            path = (root / path).resolve()
        else:
            path = path.resolve()
        if not path.is_file():
            raise EditingCheckError(f"owned file does not exist: {path}")
        try:
            path.relative_to(root.resolve())
        except ValueError:
            raise EditingCheckError(f"owned file must stay inside the repository: {path}")
        if not (path.name.startswith("test_") and path.suffix == ".py"):
            raise EditingCheckError(
                f"only unittest modules named test_*.py are supported: {path.name}"
            )
        resolved.append(path)
    return resolved


def build_unittest_command(files: list[Path], root: Path) -> list[str]:
    modules = [unittest_module_name(path, root) for path in files]
    return [sys.executable, "-B", "-m", "unittest", *modules]


def _wait_for_process(process: subprocess.Popen[int], deadline: float) -> None:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            process.wait(timeout=min(0.05, remaining))
        except subprocess.TimeoutExpired:
            continue


def kill_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    time.sleep(0.15)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _read_available(fd: int, output: bytearray, limit: int) -> bool:
    while True:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            return False
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            raise
        if not chunk:
            return False
        output.extend(chunk)
        if len(output) > limit:
            return True
    return False


def run_bounded(
    command: list[str],
    cwd: Path,
    state_root: Path,
    artifact_root: Path | None = None,
) -> int:
    artifact = artifact_root or (state_root / "artifacts")
    env = isolated_subprocess_env(state_root=state_root, artifact_root=artifact)
    hard_deadline = time.monotonic() + DEADLINE_SECONDS
    run_deadline = hard_deadline - CLEANUP_RESERVE_SECONDS
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    assert process.stdout is not None
    fd = process.stdout.fileno()
    _set_nonblocking(fd)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = bytearray()
    timed_out = False
    output_limit_hit = False
    try:
        while True:
            now = time.monotonic()
            if now >= run_deadline:
                timed_out = True
                break
            if output_limit_hit:
                break
            if process.poll() is not None:
                if _read_available(fd, output, MAX_OUTPUT_BYTES):
                    output_limit_hit = True
                    break
                break
            timeout = max(0.0, min(0.05, run_deadline - now))
            events = selector.select(timeout=timeout)
            if not events:
                continue
            if _read_available(fd, output, MAX_OUTPUT_BYTES):
                output_limit_hit = True
                break
        returncode = process.poll()
        if returncode is None and not timed_out and not output_limit_hit:
            remaining = max(0.0, run_deadline - time.monotonic())
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                returncode = None
    finally:
        selector.close()
        try:
            process.stdout.close()
        except Exception:
            pass
        kill_process_group(process.pid)
        _wait_for_process(process, hard_deadline)
        if process.poll() is None:
            kill_process_group(process.pid)
            _wait_for_process(process, hard_deadline)

    if timed_out:
        raise EditingCheckInconclusiveError(
            f"editing check exceeded {DEADLINE_SECONDS}s process-tree deadline"
        )
    if output_limit_hit:
        raise EditingCheckInconclusiveError(
            f"editing check output exceeded {MAX_OUTPUT_BYTES} bytes"
        )
    text = output.decode("utf-8", errors="replace")
    sys.stdout.write(text)
    return int(returncode if returncode is not None else 1)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    try:
        root = _repository_root()
        files = parse_file_args(argv)
        resolved = validate_files(files, root)
        command = build_unittest_command(resolved, root)
        with tempfile.TemporaryDirectory(prefix="md-editing-") as temp:
            return run_bounded(command, root, Path(temp))
    except EditingCheckInconclusiveError as exc:
        print(f"editing-check: {exc}", file=sys.stderr)
        return EXIT_INCONCLUSIVE
    except EditingCheckError as exc:
        print(f"editing-check: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except DevelopmentGuardError as exc:
        print(f"editing-check: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except OSError as exc:
        print(f"editing-check: {exc}", file=sys.stderr)
        return EXIT_INCONCLUSIVE


if __name__ == "__main__":
    raise SystemExit(main())
