from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from model_deck.adapters.platform.macos.isolated_roots import validate_isolated_roots

REPO_ROOT = Path(__file__).resolve().parents[3]
PYTHON_SRC = REPO_ROOT / "python" / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
LEGACY_AGENTS_DIR = FIXTURES / "legacy_agent"
DEFAULT_CONNECTION_ID = "550e8400-e29b-41d4-a716-446655440002"
READINESS_DEADLINE_SECONDS = 20.0
MODELS_LIST_TIMEOUT_SECONDS = 10.0
CLEANUP_WAIT_SECONDS = 3.0
SIGTERM_GRACE_SECONDS = 0.5


class EngineCliSubprocessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temps: list[tempfile.TemporaryDirectory[str]] = []
        self._owned_server: subprocess.Popen[bytes] | None = None

    def tearDown(self) -> None:
        self._stop_owned_server()
        for temp in self._temps:
            temp.cleanup()

    def _temp_dir(self) -> Path:
        temp = tempfile.TemporaryDirectory()
        self._temps.append(temp)
        return Path(temp.name)

    def _isolated_child_env(
        self,
        *,
        state_root: Path,
        artifact_root: Path,
        socket_root: Path,
    ) -> dict[str, str]:
        state_root.mkdir(parents=True, exist_ok=True)
        artifact_root.mkdir(parents=True, exist_ok=True)
        socket_root.mkdir(parents=True, exist_ok=True)
        tmp = state_root / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        codex_home = state_root / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        env: dict[str, str] = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PYTHON_SRC),
        }
        for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        env["HOME"] = str(state_root)
        env["TMPDIR"] = str(tmp)
        env["USERPROFILE"] = str(state_root)
        env["MODEL_DECK_STATE_ROOT"] = str(state_root)
        env["MODEL_DECK_ARTIFACT_ROOT"] = str(artifact_root)
        env["MODEL_DECK_SOCKET_ROOT"] = str(socket_root)
        env["CODEX_HOME"] = str(codex_home)
        env["XDG_CONFIG_HOME"] = str(state_root / ".config")
        return env

    def _redact(self, text: str, secret: str) -> str:
        if not secret:
            return text
        return text.replace(secret, "<redacted>")

    def _process_group_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _kill_process_group(self, pid: int, process: subprocess.Popen[bytes] | None = None) -> None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        term_deadline = time.monotonic() + SIGTERM_GRACE_SECONDS
        while time.monotonic() < term_deadline:
            if process is not None and process.poll() is not None:
                return
            if not self._process_group_alive(pid):
                return
            remaining = term_deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))
        if process is not None and process.poll() is not None:
            return
        if not self._process_group_alive(pid):
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                return

    def _wait_for_process(self, process: subprocess.Popen[bytes], deadline: float) -> None:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    def _stop_owned_server(self) -> None:
        process = self._owned_server
        self._owned_server = None
        if process is None:
            return
        if process.poll() is None and process.pid is not None:
            self._kill_process_group(process.pid, process)
        deadline = time.monotonic() + CLEANUP_WAIT_SECONDS
        self._wait_for_process(process, deadline)
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass

    def _wait_for_engine_files(self, state_root: Path, deadline: float) -> tuple[Path, Path]:
        rendezvous = state_root / "engine" / "rendezvous.json"
        credential = state_root / "engine" / "operator_credential"
        while time.monotonic() < deadline:
            if rendezvous.is_file() and credential.is_file():
                return rendezvous, credential
            if self._owned_server is not None and self._owned_server.poll() is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))
        server_tail = ""
        server = self._owned_server
        if server is not None and server.poll() is not None and server.stdout is not None:
            try:
                server_tail = server.stdout.read().decode("utf-8", errors="replace")
            except OSError:
                server_tail = ""
        elif server is not None and server.poll() is None:
            server_tail = "<server still running; stdout not read>"
        raise AssertionError(
            "engine did not publish rendezvous and operator credential before deadline; "
            f"missing={[str(p) for p in (rendezvous, credential) if not p.is_file()]} "
            f"server_exit={getattr(server, 'returncode', None)} "
            f"server_output={server_tail[-4000:]}"
        )


    def _run_models_list(
        self,
        list_cmd: list[str],
        env: dict[str, str],
        credential: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list_cmd,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=MODELS_LIST_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = self._redact(
                (exc.stdout or b"").decode("utf-8", errors="replace")
                if isinstance(exc.stdout, (bytes, bytearray))
                else (exc.stdout or ""),
                credential,
            )
            stderr = self._redact(
                (exc.stderr or b"").decode("utf-8", errors="replace")
                if isinstance(exc.stderr, (bytes, bytearray))
                else (exc.stderr or ""),
                credential,
            )
            self.fail(
                "models list timed out after "
                f"{MODELS_LIST_TIMEOUT_SECONDS}s: stdout={stdout} stderr={stderr}"
            )

    def _cli_base(self) -> list[str]:
        return [sys.executable, "-B", "-m", "model_deck.cli.main"]

    def test_models_list_reaches_registered_fixture_over_real_cli(self) -> None:
        state_root = self._temp_dir()
        artifact_root = self._temp_dir()
        socket_root = self._temp_dir()
        validate_isolated_roots(
            state_root,
            artifact_root,
            socket_root,
            source_root=REPO_ROOT,
        )
        env = self._isolated_child_env(
            state_root=state_root,
            artifact_root=artifact_root,
            socket_root=socket_root,
        )
        serve_cmd = [
            *self._cli_base(),
            "engine",
            "serve",
            "--state-root",
            str(state_root),
            "--artifact-root",
            str(artifact_root),
            "--socket-root",
            str(socket_root),
            "--legacy-agents-dir",
            str(LEGACY_AGENTS_DIR),
            "--default-connection-id",
            DEFAULT_CONNECTION_ID,
        ]
        self._owned_server = subprocess.Popen(
            serve_cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + READINESS_DEADLINE_SECONDS
        rendezvous_path, credential_path = self._wait_for_engine_files(state_root, deadline)
        credential = credential_path.read_text(encoding="utf-8").strip()
        list_cmd = [
            *self._cli_base(),
            "models",
            "list",
            "--rendezvous",
            str(rendezvous_path),
            "--credential",
            str(credential_path),
        ]
        completed = self._run_models_list(list_cmd, env, credential)
        if completed.returncode != 0:
            stderr = self._redact(completed.stderr, credential)
            stdout = self._redact(completed.stdout, credential)
            self.fail(
                "models list failed: "
                f"exit={completed.returncode} stdout={stdout} stderr={stderr}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            redacted = self._redact(completed.stdout, credential)
            self.fail(f"models list did not emit JSON: {exc}; stdout={redacted}")
        self.assertEqual(payload.get("collection"), "registered")
        items = payload.get("items")
        self.assertIsInstance(items, list)
        provider_ids = [
            item.get("provider_model_id")
            for item in items
            if isinstance(item, dict)
        ]
        self.assertIn("qwen/test", provider_ids)
        matching = [
            item
            for item in items
            if isinstance(item, dict) and item.get("provider_model_id") == "qwen/test"
        ]
        self.assertEqual(len(matching), 1)
        item = matching[0]
        self.assertEqual(item.get("kind"), "registered")
        self.assertEqual(item.get("connection_id"), DEFAULT_CONNECTION_ID)
        self.assertIn("registration_id", item)
        self.assertIn("display_name", item)
        self.assertIn("revision", item)
        self.assertIsNotNone(self._owned_server)
        self.assertIsNone(
            self._owned_server.poll(),
            "engine serve process exited after models list disconnected",
        )
        second = self._run_models_list(list_cmd, env, credential)
        self.assertEqual(
            second.returncode,
            0,
            self._redact(second.stderr or second.stdout, credential),
        )
        self.assertIsNone(
            self._owned_server.poll(),
            "engine serve process exited after second models list",
        )


if __name__ == "__main__":
    unittest.main()
