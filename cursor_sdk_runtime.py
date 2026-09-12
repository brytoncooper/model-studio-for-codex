"""Isolated, pinned Cursor SDK installation and a credential-safe subprocess broker.

The app's Python environment never imports Cursor's SDK. Each inference broker owns
one SDK agent and stays alive while Codex executes its custom-tool callbacks.
"""
import json
import math
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid


SDK_VERSION = "1.0.31"
INSTALL_TIMEOUT = 300
CALLBACK_TIMEOUT = 900


class CursorRuntimeError(Exception):
    pass


def error_diagnostic(error):
    """Exception location without its message, request, locals, or credentials."""
    return {"exception_type": type(error).__name__, "frames": [
        {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
        for frame in traceback.extract_tb(error.__traceback__)[-6:]]}


def runtime_directory():
    from routing_registry import support_directory
    return support_directory() / "cursor-sdk"


def _python(directory):
    return Path(directory) / "venv" / "bin" / "python3"


def _environment():
    # Auth always travels over stdin. SDK debug logging can expose prompt contents.
    return {key: value for key, value in os.environ.items()
            if key not in {"CURSOR_API_KEY", "CURSOR_SDK_LOG", "PYTHONPATH", "PYTHONHOME"}}


def _bounded_command(arguments, timeout, input_text=None):
    process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, env=_environment(),
                               start_new_session=True)
    try:
        output, _ = process.communicate(input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_process(process)
        raise CursorRuntimeError("Cursor SDK operation timed out.") from None
    if process.returncode:
        raise CursorRuntimeError("Cursor SDK operation failed. Check the installation and API key.")
    if len(output) > 4 * 1024 * 1024:
        raise CursorRuntimeError("Cursor SDK returned an unexpectedly large response.")
    return output


def _stop_process(process):
    # The SDK's Node bridge shares this group, so terminating the broker also
    # terminates its bridge and releases callbacks after a disconnected request.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


def _installed_version(directory):
    interpreter = _python(directory)
    if not interpreter.is_file():
        return None
    try:
        output = _bounded_command([str(interpreter), "-c",
            "from importlib.metadata import version; from cursor_sdk import Agent, CustomTool, CursorClient; "
            "print(version('cursor-sdk'))"], 15)
        return output.strip()
    except (OSError, CursorRuntimeError):
        return None


def status():
    version = _installed_version(runtime_directory())
    installed = version == SDK_VERSION
    return {"ok": True, "installed": installed, "version": version,
            "required_version": SDK_VERSION,
            "message": "Cursor SDK is ready." if installed else "Install Cursor SDK to use Cursor models."}


def _verify_bridge(directory, timeout=40):
    script = ("import tempfile; from cursor_sdk import CursorClient; "
              "workspace = tempfile.TemporaryDirectory(prefix='model-deck-cursor-verify-'); "
              "client = CursorClient.launch_bridge(workspace=workspace.name, state_root=workspace.name, "
              "allow_api_key_env_fallback=False); "
              "client.ping(); client.close(); workspace.cleanup(); print('bridge-ready')")
    output = _bounded_command([str(_python(directory)), "-c", script], timeout)
    if output.strip() != "bridge-ready":
        raise CursorRuntimeError("Cursor SDK bridge verification failed.")


def install():
    """Stage and verify a new environment before replacing the installed runtime."""
    target = runtime_directory()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.parent / ".cursor-sdk-install.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return {"ok": False, "installed": False, "version": None, "required_version": SDK_VERSION,
                "message": "A Cursor SDK installation is already in progress."}
    os.close(descriptor)
    staged = None
    backup = None
    try:
        if target.is_symlink():
            raise CursorRuntimeError("Cursor SDK installation cannot replace a symbolic link.")
        if _installed_version(target) == SDK_VERSION:
            return status()
        staged = Path(tempfile.mkdtemp(prefix=".cursor-sdk-install-", dir=target.parent))
        deadline = time.monotonic() + INSTALL_TIMEOUT
        _bounded_command([sys.executable, "-m", "venv", str(staged / "venv")],
                         max(1, deadline - time.monotonic()))
        _bounded_command([str(_python(staged)), "-m", "pip", "install", "--disable-pip-version-check",
                          "--no-input", "--quiet", "cursor-sdk==" + SDK_VERSION],
                         max(1, deadline - time.monotonic()))
        if _installed_version(staged) != SDK_VERSION:
            raise CursorRuntimeError("Cursor SDK installation verification failed.")
        if target.exists():
            backup = target.parent / (".cursor-sdk-backup-" + uuid.uuid4().hex)
            target.rename(backup)
        staged.rename(target)
        staged = None
        if _installed_version(target) != SDK_VERSION:
            shutil.rmtree(target)
            raise CursorRuntimeError("Cursor SDK could not run from its installed location.")
        try:
            _verify_bridge(target, max(1, min(40, deadline - time.monotonic())))
        except (OSError, CursorRuntimeError):
            shutil.rmtree(target)
            raise
        if backup:
            shutil.rmtree(backup)
            backup = None
        return status()
    except (OSError, CursorRuntimeError):
        if backup and backup.exists() and not target.exists():
            backup.rename(target)
        return {"ok": False, "installed": False, "version": None, "required_version": SDK_VERSION,
                "message": "Cursor SDK installation failed; the previous installation was preserved."}
    finally:
        if staged and staged.exists():
            shutil.rmtree(staged)
        lock.unlink(missing_ok=True)


def list_models(api_key):
    if not api_key:
        return {"ok": False, "models": [], "message": "Add a Cursor user API key first."}
    if not status()["installed"]:
        return {"ok": False, "models": [], "message": "Install Cursor SDK first."}
    try:
        output = _bounded_command([str(_python(runtime_directory())), str(Path(__file__).resolve()),
                                   "--models"], 90, json.dumps({"api_key": api_key}))
        result = json.loads(output)
        if not isinstance(result, dict) or not isinstance(result.get("models"), list):
            raise CursorRuntimeError("Invalid Cursor model catalog.")
        if result.get("ok"):
            save_catalog_cache(result["models"])
        return result
    except (OSError, ValueError, CursorRuntimeError):
        return {"ok": False, "models": [],
                "message": "Could not load Cursor models. Check your API key and account access."}


CATALOG_CACHE_NAME = "cursor-models.json"


def catalog_cache_path():
    from routing_registry import support_directory
    return support_directory() / CATALOG_CACHE_NAME


def save_catalog_cache(models):
    """Keep the public catalog rows (ids, parameters, variants) so the router can offer Fast. Never the key."""
    path = catalog_cache_path()
    document = json.dumps({"version": 1, "saved_at": time.time(), "models": models})
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with open(temporary, "w", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
            stream.write(document)
        os.replace(temporary, path)
    except OSError:
        pass


def load_catalog_cache():
    """Cached catalog rows, or None when no successful listing has been saved yet."""
    try:
        document = json.loads(catalog_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    models = document.get("models") if isinstance(document, dict) else None
    return models if isinstance(models, list) else None


def catalog_cache_age():
    try:
        return time.time() - catalog_cache_path().stat().st_mtime
    except OSError:
        return None


def fast_capable_models(models=None):
    """cursor/<id> for every catalog row with a `fast` parameter, or None without a cached catalog."""
    rows = load_catalog_cache() if models is None else models
    if rows is None:
        return None
    capable = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            continue
        if any(isinstance(parameter, dict) and parameter.get("id") == "fast"
               and "true" in (parameter.get("values") or []) for parameter in row.get("parameters") or []):
            capable.add(row["id"] if row["id"].startswith("cursor/") else "cursor/" + row["id"])
    return capable


class CursorSdkProcess:
    """One generation's SDK process. Public messages contain no credential fields."""

    def __init__(self, payload):
        directory = runtime_directory()
        if _installed_version(directory) != SDK_VERSION:
            raise CursorRuntimeError("Install Cursor SDK in Model Deck before using Cursor models.")
        self.events = queue.Queue(maxsize=1024)
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        self._process = subprocess.Popen([str(_python(directory)), str(Path(__file__).resolve()), "--broker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=_environment(), start_new_session=True)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            self._send(dict(payload, command="start"))
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self._process.stdout:
                event = json.loads(line)
                while not self._closed.is_set():
                    try:
                        self.events.put(event, timeout=0.5)
                        break
                    except queue.Full:
                        continue
        except (ValueError, OSError):
            pass
        finally:
            if not self._closed.is_set():
                try:
                    self.events.put({"type": "error", "message": "Cursor SDK stopped before completing the run."},
                                    timeout=1)
                except queue.Full:
                    pass

    def _send(self, message):
        with self._write_lock:
            if self._closed.is_set():
                raise CursorRuntimeError("Cursor SDK run has closed.")
            self._process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self._process.stdin.flush()

    def tool_result(self, call_id, output):
        self._send({"command": "tool_result", "call_id": call_id, "output": output})

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        _stop_process(self._process)
        for stream in (self._process.stdin, self._process.stdout):
            try:
                stream.close()
            except OSError:
                pass


def _sdk_usage(usage):
    if usage is None:
        return None
    cached = usage.cache_read_tokens
    input_tokens = usage.input_tokens + cached + usage.cache_write_tokens
    return {"input_tokens": input_tokens, "output_tokens": usage.output_tokens,
            "total_tokens": input_tokens + usage.output_tokens,
            "input_tokens_details": {"cached_tokens": cached},
            "output_tokens_details": {"reasoning_tokens": usage.reasoning_tokens or 0}}


def _tool_content(output):
    """Translate Codex result blocks to MCP custom-tool result content."""
    if isinstance(output, dict) and isinstance(output.get("content"), list):
        output = output["content"]
    if not isinstance(output, list):
        return {"content": [{"type": "text", "text": output if isinstance(output, str) else json.dumps(output)}]}
    content = []
    for part in output:
        if isinstance(part, dict) and part.get("type") in {"input_text", "output_text", "text"}:
            content.append({"type": "text", "text": str(part.get("text", ""))})
        elif isinstance(part, dict) and part.get("type") == "input_image":
            url = part.get("image_url", "")
            if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                header, data = url.split(";base64,", 1)
                content.append({"type": "image", "data": data, "mimeType": header[5:]})
            else:
                content.append({"type": "text", "text": json.dumps(part)})
        else:
            content.append({"type": "text", "text": json.dumps(part)})
    return {"content": content}


def _model_selection(model_id, catalog, reasoning=None, service_tier=None):
    """Cursor's model selection for a Codex request: reasoning effort and the Fast toggle as SDK parameters."""
    model = next((model for model in catalog if model.id == model_id), None)
    if model is None:
        raise CursorRuntimeError("model_unavailable")
    parameters = []
    if model_id == "auto-smart":
        option = next((option for option in model.parameters if option.id == "optimize_for"), None)
        if option is None or "balanced" not in {value.value for value in option.values}:
            raise CursorRuntimeError("router_mode_unavailable")
        parameters.append({"id": "optimize_for", "value": "balanced"})
    effort = (reasoning or {}).get("effort")
    if effort:
        # Cursor names this parameter differently per model family (effort, reasoning, reasoning_effort).
        option = next((option for option in model.parameters
                       if option.id in {"reasoning_effort", "reasoning", "effort"}), None)
        if option is not None and effort in {value.value for value in option.values}:
            parameters.append({"id": option.id, "value": effort})
    # Codex's Fast toggle arrives as service tier "priority" (or "fast"); anything else means off.
    wants_fast = str(service_tier or "").lower() in {"fast", "priority"}
    fast = next((option for option in model.parameters if option.id == "fast"), None)
    if fast is None or "true" not in {value.value for value in fast.values}:
        if wants_fast:
            raise CursorRuntimeError("fast_unavailable")
    else:
        parameters.append({"id": "fast", "value": "true" if wants_fast else "false"})
    return {"id": model_id, "params": parameters}


def _billed_cost(agent):
    original_client = agent.client
    try:
        agent.client = original_client.with_options(unary_timeout=5, max_retries=0)
        usage = agent.get_usage()
        if usage.cost is None:
            return None
        if any(not isinstance(value, (int, float)) or isinstance(value, bool)
               or not math.isfinite(value) or value < 0
               for value in (usage.cost.raw_cost_cents, usage.cost.charged_cents)):
            return None
        return {"raw_cost_cents": usage.cost.raw_cost_cents, "charged_cents": usage.cost.charged_cents}
    except Exception:
        return None
    finally:
        agent.client = original_client


def _catalog_main():
    from cursor_sdk import CursorClient
    payload = json.load(sys.stdin)
    with tempfile.TemporaryDirectory(prefix="model-deck-cursor-catalog-") as workspace:
        with CursorClient.launch_bridge(workspace=workspace, state_root=workspace,
                                       allow_api_key_env_fallback=False) as client:
            models = client.models.list(api_key=payload["api_key"])
            return {"ok": True, "models": [{"id": "cursor/" + model.id,
                "name": model.display_name or model.id, "description": model.description,
                "parameters": [{"id": parameter.id, "values": [value.value for value in parameter.values]}
                               for parameter in model.parameters],
                "variants": [{"name": variant.display_name, "description": variant.description,
                              "is_default": variant.is_default,
                              "params": [{"id": value.id, "value": value.value} for value in variant.params]}
                             for variant in model.variants]} for model in models],
                "message": "Cursor models loaded for this API key."}


def _isolated_control_input():
    """Keep SDK children away from the pipe carrying Codex callback results."""
    control_fd = os.dup(sys.stdin.fileno())
    try:
        os.set_inheritable(control_fd, False)
        # The bundled Node bridge inherits fd 0 and makes it nonblocking. Give
        # it /dev/null so that operation cannot turn our command pipe into an
        # apparent EOF while a Codex tool callback is still waiting.
        with open(os.devnull, "rb") as bridge_stdin:
            os.dup2(bridge_stdin.fileno(), sys.stdin.fileno())
        return os.fdopen(control_fd, "r", encoding="utf-8")
    except BaseException:
        os.close(control_fd)
        raise


def _broker_main():
    with _isolated_control_input() as commands:
        _run_broker(commands)


def _run_broker(commands):
    from cursor_sdk import AgentOptions, CursorClient, CustomTool, LocalAgentOptions, SendOptions
    write_lock = threading.Lock()
    pending_lock = threading.Lock()
    pending = {}
    stopped = threading.Event()

    def emit(message):
        with write_lock:
            sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    def callback(tool):
        def execute(arguments, context):
            call_id = "cursor-call-" + uuid.uuid4().hex
            result = {"ready": threading.Event(), "output": None}
            with pending_lock:
                pending[call_id] = result
            emit({"type": "tool_call", "call_id": call_id, "name": tool["name"], "arguments": arguments})
            deadline = time.monotonic() + CALLBACK_TIMEOUT
            try:
                while not result["ready"].wait(0.5):
                    if stopped.is_set() or time.monotonic() >= deadline:
                        return {"content": [{"type": "text", "text": "Codex tool result was cancelled or expired."}],
                                "isError": True}
                return _tool_content(result["output"])
            finally:
                with pending_lock:
                    pending.pop(call_id, None)
        return execute

    def run(payload):
        try:
            with tempfile.TemporaryDirectory(prefix="model-deck-cursor-run-") as workspace:
                # No workspace files/settings are exposed to Cursor. Actual working
                # directory and instruction context stay in the Codex prompt envelope.
                with CursorClient.launch_bridge(workspace=workspace, state_root=workspace,
                    allow_api_key_env_fallback=False, client_timeout=CALLBACK_TIMEOUT + 60) as client:
                    custom_tools = {tool["name"]: CustomTool(execute=callback(tool),
                        description=tool.get("description", ""), input_schema=tool.get("parameters", {}))
                        for tool in payload["tools"]}
                    catalog = client.with_options(unary_timeout=30).models.list(api_key=payload["api_key"])
                    selection = _model_selection(payload["model"], catalog, payload.get("reasoning"),
                                                 payload.get("service_tier"))
                    options = AgentOptions(model=selection, api_key=payload["api_key"],
                        tools=["mcp"] if custom_tools else [], mcp_servers={}, agents={},
                        local=LocalAgentOptions(cwd=workspace, setting_sources=[], custom_tools=custom_tools))
                    with client.agents.create(options) as agent:
                        emit({"type": "started", "agent_id": agent.agent_id})
                        def delta(update):
                            if update.type == "text-delta":
                                emit({"type": "text", "text": update.text})
                            elif update.type == "thinking-delta":
                                emit({"type": "thinking", "text": update.text})
                        active_run = agent.send(payload["message"], SendOptions(on_delta=delta))
                        for message in active_run.messages():
                            if stopped.is_set():
                                if active_run.status == "running":
                                    active_run.cancel()
                                break
                            if getattr(message, "type", None) == "usage":
                                emit({"type": "usage", "usage": _sdk_usage(message.usage)})
                        result = active_run.wait()
                        cost = _billed_cost(agent) if result.status == "finished" else None
                        emit({"type": "done", "status": result.status,
                              "usage": _sdk_usage(result.usage), "agent_id": agent.agent_id,
                              "run_id": active_run.id, "cursor_usage_cost": cost})
        except CursorRuntimeError as error:
            emit({"type": "error", "code": str(error)})
        except Exception as error:
            emit({"type": "error", "diagnostic": error_diagnostic(error),
                  "message": "Cursor SDK run failed. Check your API key, model access, and SDK installation."})

    started = False
    worker = None
    try:
        for line in commands:
            message = json.loads(line)
            if message.get("command") == "start" and not started:
                started = True
                worker = threading.Thread(target=run, args=(message,), daemon=True)
                worker.start()
            elif message.get("command") == "tool_result":
                with pending_lock:
                    result = pending.get(message.get("call_id"))
                    if result is not None:
                        result["output"] = message.get("output")
                        result["ready"].set()
            elif message.get("command") == "cancel":
                break
    finally:
        stopped.set()
        if worker:
            worker.join(timeout=3)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--models"]:
            print(json.dumps(_catalog_main()))
        elif sys.argv[1:] == ["--broker"]:
            _broker_main()
        else:
            raise CursorRuntimeError("Unknown Cursor SDK operation.")
    except Exception:
        print(json.dumps({"ok": False, "models": [], "message": "Cursor SDK operation failed."}))
        sys.exit(1)
