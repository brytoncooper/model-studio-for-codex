"""Private, transactional Codex configuration editing. Never accepts API keys."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

import tomlkit


PROVIDER = "openrouter-settings"
ROOT_FIELDS = ("model_provider", "model", "model_reasoning_effort", "service_tier")
AGENT_MARKER = "# Managed by OpenRouter Settings native-agent registration v1"
SUBSCRIPTION_MODELS = ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
                       "gpt-5.5", "gpt-5.3-codex-spark")
BOUNDED_AGENT_INSTRUCTIONS = (
    "Complete only the bounded task assigned by the parent agent. Obey the parent's scope, "
    "ownership boundaries, fixed decisions, and verification requirements. Preserve other agents' "
    "changes. Report results and unresolved limits to the parent. Do not delegate to additional "
    "agents unless the parent explicitly asks you to do so."
)


class SettingsError(Exception):
    pass


def check_path(path):
    if path.is_symlink() or path.parent.is_symlink():
        raise SettingsError("Symbolic links are not supported for settings files.")
    if path.exists() and not path.is_file():
        raise SettingsError("A settings file path is not a regular file.")


def read_bytes(path):
    check_path(path)
    return path.read_bytes() if path.exists() else None


def parse_config(data):
    try:
        return tomlkit.parse((data or b"").decode("utf-8"))
    except Exception:
        raise SettingsError("The Codex configuration is not valid UTF-8 TOML.") from None


def atomic_write(path, data):
    check_path(path)
    fd, temporary = tempfile.mkstemp(prefix=".openrouter-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        check_path(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def replace_bytes(path, data):
    if data is None:
        check_path(path)
        if path.exists():
            path.unlink()
    else:
        atomic_write(path, data)


def providers(config):
    value = config.get("model_providers", {})
    if not isinstance(value, dict):
        raise SettingsError("model_providers must be a TOML table.")
    return value


def touched(config):
    values = {key: config[key].unwrap() if hasattr(config[key], "unwrap") else config[key]
              for key in ROOT_FIELDS if key in config}
    if PROVIDER in providers(config):
        provider = providers(config)[PROVIDER]
        values["provider_table"] = provider.unwrap() if hasattr(provider, "unwrap") else provider
    return values


def response(config, config_path, state):
    return {"ok": True, "provider": config.get("model_provider", "openai"),
            "model": config.get("model", ""), "can_restore": state is not None,
            "config_path": str(config_path)}


def transaction(config_path, new_config, old_config, state_path, new_state, old_state):
    if read_bytes(config_path) != old_config or read_bytes(state_path) != old_state:
        raise SettingsError("Settings changed during this operation. Try again after other editors finish.")
    try:
        replace_bytes(config_path, new_config)
        replace_bytes(state_path, new_state)
    except Exception:
        try:
            replace_bytes(config_path, old_config)
            replace_bytes(state_path, old_state)
        except Exception:
            raise SettingsError("Saving failed and rollback was incomplete. The original backup remains in the application support folder.") from None
        raise SettingsError("Saving failed. Previous settings were restored.") from None


def validated_model_fields(request):
    model, account, executable = request.get("model"), request.get("account"), request.get("executable")
    effort = request.get("effort", "default")
    if (not isinstance(model, str) or len(model) > 256 or "/" not in model
            or any(c.isspace() or ord(c) < 32 for c in model)
            or not all(model.split("/", 1))):
        raise SettingsError("Enter a valid OpenRouter model ID, including its provider and a slash, without spaces.")
    try:
        if str(uuid.UUID(account)) != account.lower():
            raise ValueError()
    except Exception:
        raise SettingsError("The key account identifier must be a UUID.") from None
    if (not isinstance(executable, str) or not Path(executable).is_absolute()
            or any(ord(c) < 32 for c in executable)):
        raise SettingsError("The authentication executable must be an absolute path.")
    if effort not in ("default", "low", "medium", "high", "xhigh"):
        raise SettingsError("Unsupported reasoning effort.")
    return model, account, executable, effort


def provider_table(account, executable):
    provider = tomlkit.table()
    provider.update({"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                     "wire_api": "responses", "supports_websockets": False})
    auth = tomlkit.table()
    auth.update({"command": executable, "args": ["--token", account],
                 "timeout_ms": 5000, "refresh_interval_ms": 300000})
    provider["auth"] = auth
    return provider


def write_managed_agent(request, agent):
    name, model = agent["name"], agent["model"]
    agents_dir = Path(request.get("agents_dir", str(Path.home() / ".codex/agents"))).absolute()
    if agents_dir.is_symlink() or agents_dir.parent.is_symlink():
        raise SettingsError("Symbolic links are not supported for the agents folder.")
    path = agents_dir / f"{name}.toml"
    previous = read_bytes(path)
    if previous is not None:
        if not previous.startswith((AGENT_MARKER + "\n").encode("utf-8")):
            raise SettingsError("An unowned agent file already exists at this path. It was not changed.")
        previous_config = parse_config(previous)
        if previous_config.get("name") != name or previous_config.get("model") != model:
            raise SettingsError("The managed agent file has conflicting identity fields. It was not changed.")
    encoded = tomlkit.dumps(agent).encode("utf-8")
    parse_config(encoded)
    agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if read_bytes(path) != previous:
        raise SettingsError("The agent file changed during registration. Try again after other editors finish.")
    if previous != encoded:
        atomic_write(path, encoded)
    return path


def register_agent(request, config, config_path, state):
    model, account, executable, effort = validated_model_fields(request)
    if model.lower().startswith(("openai/", "~openai/")):
        raise SettingsError("Use your normal Codex subscription for OpenAI models. Register a non-OpenAI model here.")
    slug = re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")[:50] or "model"
    digest = hashlib.sha256(model.encode("utf-8")).hexdigest()[:8]
    name = f"openrouter_{slug}_{digest}"
    agent = tomlkit.document()
    agent.add(tomlkit.comment(AGENT_MARKER[2:]))
    agent["name"] = name
    agent["description"] = f"Bounded task worker using {model} through OpenRouter. Uses OpenRouter credits."
    agent["developer_instructions"] = BOUNDED_AGENT_INSTRUCTIONS
    agent["model"] = model
    agent["model_reasoning_effort"] = "low" if effort == "default" else effort
    agent["model_provider"] = PROVIDER
    agent["model_providers"] = tomlkit.table()
    agent["model_providers"][PROVIDER] = provider_table(account, executable)
    path = write_managed_agent(request, agent)
    result = response(config, config_path, state)
    result.update({"agent_name": name, "agent_path": str(path),
                   "message": f"Registered {name}. Start a new Codex task to use this OpenRouter agent."})
    return result


def register_subscription_agent(request, config, config_path, state):
    model = request.get("model")
    effort = request.get("effort", "low")
    if not isinstance(model, str) or model not in SUBSCRIPTION_MODELS:
        raise SettingsError("Choose a supported bare OpenAI subscription model ID.")
    if effort not in ("low", "medium", "high", "xhigh"):
        raise SettingsError("Unsupported subscription agent reasoning effort.")
    name = "subscription_" + re.sub(r"[^a-z0-9]+", "_", model)
    agent = tomlkit.document()
    agent.add(tomlkit.comment(AGENT_MARKER[2:]))
    agent["name"] = name
    agent["description"] = f"Bounded task worker using {model} through the OpenAI subscription connection."
    agent["developer_instructions"] = BOUNDED_AGENT_INSTRUCTIONS
    agent["model"] = model
    agent["model_provider"] = "openai"
    agent["model_reasoning_effort"] = effort
    path = write_managed_agent(request, agent)
    result = response(config, config_path, state)
    result.update({"agent_name": name, "agent_path": str(path),
                   "message": f"Registered {name}. Start a new Codex task to use this subscription agent."})
    return result


def handle(request):
    action = request.get("action")
    if action == "runtime_info":
        from codex_runtime import discover_runtime
        try:
            return {"ok": True, "runtime": discover_runtime()}
        except RuntimeError:
            raise SettingsError("No supported Codex desktop runtime was found in Applications.") from None
    if action == "list_models":
        from routing_registry import RoutingRegistry
        try:
            models = RoutingRegistry(agents_dir=request.get("agents_dir")).load_models()
            return {"ok": True, "models": [{"model": model, "role": entry["role"]}
                                            for model, entry in sorted(models.items())]}
        except Exception:
            raise SettingsError("Could not read registered models. Check the managed agent files.") from None
    if action not in ("status", "apply", "restore", "register_agent", "register_subscription_agent"):
        raise SettingsError("Unknown settings action.")
    config_path = Path(request.get("config_path", str(Path.home() / ".codex/config.toml"))).absolute()
    state_dir = Path(request.get("state_dir", str(Path.home() / "Library/Application Support/Codex OpenRouter"))).absolute()
    if state_dir.is_symlink() or state_dir.parent.is_symlink():
        raise SettingsError("Symbolic links are not supported for the application support folder.")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    state_path = state_dir / "state.json"
    backup_path = state_dir / "original-config.toml"
    lock_path = state_dir / "settings.lock"
    check_path(lock_path)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SettingsError("Another settings operation is running. Try again.") from None
        old_config = read_bytes(config_path)
        config = parse_config(old_config)
        old_state = read_bytes(state_path)
        state = None
        if old_state is not None:
            try:
                state = json.loads(old_state)
                if (state["version"] != 1 or state["config_path"] != str(config_path)
                        or not isinstance(state["last"], dict)
                        or not isinstance(state["original_exists"], bool)):
                    raise ValueError()
            except Exception:
                raise SettingsError("The saved restore record is invalid or belongs to another configuration.") from None
        if action == "status":
            return response(config, config_path, state)
        if action == "register_agent":
            return register_agent(request, config, config_path, state)
        if action == "register_subscription_agent":
            return register_subscription_agent(request, config, config_path, state)
        if state and touched(config) != state["last"]:
            raise SettingsError("Codex model settings changed outside this app. Restore or apply was stopped to preserve those edits.")
        if action == "restore":
            if state is None:
                raise SettingsError("There are no saved settings to restore.")
            original_bytes = read_bytes(backup_path)
            if original_bytes is None:
                raise SettingsError("The original configuration backup is missing.")
            original = parse_config(original_bytes)
            for key in ROOT_FIELDS:
                if key in original:
                    config[key] = original[key]
                elif key in config:
                    del config[key]
            del config["model_providers"][PROVIDER]
            if not config["model_providers"] and "model_providers" not in original:
                del config["model_providers"]
            encoded = tomlkit.dumps(config).encode("utf-8")
            parse_config(encoded)
            if not state["original_exists"] and not encoded.strip():
                encoded = None
            transaction(config_path, encoded, old_config, state_path, None, old_state)
            result = response(config, config_path, None)
            result["message"] = "Previous model settings restored. Restart Codex to use them."
            return result
        model, account, executable, effort = validated_model_fields(request)
        if state is None and PROVIDER in providers(config):
            raise SettingsError("The reserved OpenRouter provider already exists. It was not changed.")
        if state is None:
            state = {"version": 1, "config_path": str(config_path), "original_exists": old_config is not None}
            atomic_write(backup_path, old_config or b"")
        config["model_provider"] = PROVIDER
        config["model"] = model.strip()
        if effort == "default":
            config.pop("model_reasoning_effort", None)
        else:
            config["model_reasoning_effort"] = effort
        config.pop("service_tier", None)
        if "model_providers" not in config:
            config["model_providers"] = tomlkit.table()
        config["model_providers"][PROVIDER] = provider_table(account, executable)
        encoded = tomlkit.dumps(config).encode("utf-8")
        validated = parse_config(encoded)
        state["last"] = touched(validated)
        config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        transaction(config_path, encoded, old_config, state_path,
                    json.dumps(state, indent=2).encode("utf-8"), old_state)
        result = response(validated, config_path, state)
        result["message"] = "OpenRouter settings saved. Restart Codex and start a new task."
        return result


def main():
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise SettingsError("Expected a JSON settings request.")
        result = handle(request)
    except SettingsError as error:
        result = {"ok": False, "error": str(error)}
    except Exception:
        result = {"ok": False, "error": "The settings operation failed. Check file permissions and the request format."}
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
