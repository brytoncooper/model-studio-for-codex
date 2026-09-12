"""Private, transactional Codex configuration editing. Never accepts API keys."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import tempfile
import uuid

import tomlkit


PROVIDER = "openrouter-settings"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
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


def validated_endpoint_fields(request):
    """Endpoint name and base URL; OpenRouter by default. Returns (name, base_url, is_openrouter)."""
    from routing_registry import RegistryError, is_openrouter_url, is_cursor_url, validate_base_url
    base_url = request.get("base_url") or OPENROUTER_BASE_URL
    try:
        base_url = validate_base_url(base_url)
    except RegistryError as error:
        raise SettingsError(str(error)) from None
    is_openrouter = is_openrouter_url(base_url)
    name = request.get("endpoint_name") or ("OpenRouter" if is_openrouter else "Cursor" if is_cursor_url(base_url) else "Custom endpoint")
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 64 or any(ord(c) < 32 for c in name):
        raise SettingsError("Give the endpoint a short name.")
    return name.strip(), base_url, is_openrouter


def validated_model_fields(request):
    model, account, executable = request.get("model"), request.get("account"), request.get("executable")
    effort = request.get("effort", "default")
    _, base_url, is_openrouter = validated_endpoint_fields(request)
    if (not isinstance(model, str) or not 0 < len(model) <= 256
            or any(c.isspace() or ord(c) < 32 for c in model) or '"' in model):
        raise SettingsError("Enter a valid model ID without spaces.")
    from routing_registry import RegistryError, is_cursor_url, validate_cursor_route
    try:
        validate_cursor_route(model, base_url, request.get("wire"))
    except RegistryError as error:
        raise SettingsError(str(error)) from None
    if is_openrouter and ("/" not in model or not all(model.split("/", 1))):
        raise SettingsError("Enter a valid OpenRouter model ID, including its provider and a slash, without spaces.")
    if model.lower().lstrip("~").startswith(("gpt-", "codex-")):
        raise SettingsError("Names starting with gpt- or codex- are reserved for OpenAI models.")
    if account is not None or is_openrouter or is_cursor_url(base_url):
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


def provider_table(account, executable, base_url=OPENROUTER_BASE_URL, name="OpenRouter"):
    provider = tomlkit.table()
    provider.update({"name": name, "base_url": base_url, "wire_api": "responses", "supports_websockets": False})
    if account is not None:
        auth = tomlkit.table()
        auth.update({"command": executable, "args": ["--token", account],
                     "timeout_ms": 5000, "refresh_interval_ms": 300000})
        provider["auth"] = auth
    return provider


def write_managed_agent(request, agent, preserve_registered_route=False):
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
        if preserve_registered_route:
            old_provider = previous_config.get("model_providers", {}).get(PROVIDER, {})
            new_provider = agent["model_providers"][PROVIDER]
            from routing_registry import RegistryError, validate_provider
            try:
                validate_provider(old_provider.unwrap() if hasattr(old_provider, "unwrap") else old_provider)
            except RegistryError:
                raise SettingsError("The existing model connection is invalid. Remove and add this model again to repair it.") from None
            old_account = old_provider.get("auth", {}).get("args", [])
            new_account = new_provider.get("auth", {}).get("args", [])
            if (previous_config.get("model_provider") != PROVIDER
                    or old_provider.get("base_url", "").rstrip("/") != new_provider["base_url"].rstrip("/")
                    or old_account != new_account):
                raise SettingsError("This model is already registered on another connection. Remove it from your library before moving it.")
            if old_account and old_provider["auth"]["command"] != new_provider["auth"]["command"]:
                old_provider["auth"]["command"] = new_provider["auth"]["command"]
                if read_bytes(path) != previous:
                    raise SettingsError("The agent file changed during registration. Try again after other editors finish.")
                atomic_write(path, tomlkit.dumps(previous_config).encode("utf-8"))
            return path, True
    encoded = tomlkit.dumps(agent).encode("utf-8")
    parse_config(encoded)
    agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if read_bytes(path) != previous:
        raise SettingsError("The agent file changed during registration. Try again after other editors finish.")
    if previous != encoded:
        atomic_write(path, encoded)
    return path, False


def register_agent(request, config, config_path, state):
    model, account, executable, effort = validated_model_fields(request)
    endpoint_name, base_url, is_openrouter = validated_endpoint_fields(request)
    if model.lower().startswith(("openai/", "~openai/")):
        raise SettingsError("Use your normal Codex subscription for OpenAI models. Register a non-OpenAI model here.")
    slug = re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")[:50] or "model"
    digest = hashlib.sha256(model.encode("utf-8")).hexdigest()[:8]
    name = f"openrouter_{slug}_{digest}"
    from routing_registry import CURSOR_BILLING, is_cursor_url
    from provider_connections import provider_billing_description
    billing = (CURSOR_BILLING if is_cursor_url(base_url) else "Uses OpenRouter credits." if is_openrouter
               else provider_billing_description(base_url, has_key=account is not None))
    agent = tomlkit.document()
    agent.add(tomlkit.comment(AGENT_MARKER[2:]))
    agent["name"] = name
    agent["description"] = f"Bounded task worker using {model} through {endpoint_name}. {billing}"
    agent["developer_instructions"] = BOUNDED_AGENT_INSTRUCTIONS
    agent["model"] = model
    agent["model_reasoning_effort"] = "low" if effort == "default" else effort
    agent["model_provider"] = PROVIDER
    agent["model_providers"] = tomlkit.table()
    agent["model_providers"][PROVIDER] = provider_table(account, executable, base_url, endpoint_name)
    path, already_registered = write_managed_agent(request, agent, preserve_registered_route=True)
    result = response(config, config_path, state)
    result.update({"agent_name": name, "agent_path": str(path),
                   "already_registered": already_registered,
                   "message": (f"{model} is already in your library on this connection." if already_registered else
                               f"Registered {name} on {endpoint_name}. It appears in Codex's picker on the next turn.")})
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
    path, _ = write_managed_agent(request, agent)
    result = response(config, config_path, state)
    result.update({"agent_name": name, "agent_path": str(path),
                   "message": f"Registered {name}. Start a new Codex task to use this subscription agent."})
    return result


def endpoint_models(request):
    """Read a catalog only after matching its destination to the saved connection."""
    from provider_connections import ProviderConnectionError, fetch_endpoint_models
    from routing_registry import RegistryError, is_openrouter_url, support_directory, validate_base_url
    try:
        account = request.get("account")
        if not isinstance(account, str) or str(uuid.UUID(account)) != account.lower():
            raise SettingsError("Choose a saved connection before loading its models.")
        base_url = validate_base_url(request.get("base_url")).rstrip("/")
        preferences_path = Path(request.get("state_dir", str(support_directory()))) / "preferences.json"
        check_path(preferences_path)
        with preferences_path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise SettingsError("The saved connections file is too large.")
        preferences = json.loads(raw)
        accounts = preferences.get("accounts", [])
        if not isinstance(accounts, list):
            raise SettingsError("Could not read saved connections.")
        matches = [entry for entry in accounts if isinstance(entry, dict) and entry.get("id") == account]
        if len(matches) != 1:
            raise SettingsError("This connection is no longer saved. Choose it again.")
        saved = matches[0]
        saved_url = validate_base_url(saved.get("baseURL") or OPENROUTER_BASE_URL).rstrip("/")
        saved_wire = saved.get("wire") or "auto"
        saved_keyed = saved.get("hasKey", True)
        if (type(saved_keyed) is not bool or saved_url != base_url
                or request.get("wire", "auto") != saved_wire
                or request.get("has_key", saved_keyed) != saved_keyed):
            raise SettingsError("The connection changed. Choose it again to load its models.")
        token = None
        if saved_keyed and not is_openrouter_url(saved_url):
            executable = request.get("executable")
            if (not isinstance(executable, str) or not Path(executable).is_absolute()
                    or any(ord(character) < 32 for character in executable)):
                raise SettingsError("The authentication executable must be an absolute path.")
            completed = subprocess.run([executable, "--token", account], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
                                       check=False, text=True)
            token = completed.stdout.strip()
            if completed.returncode or not token or len(token) > 16384 or any(c.isspace() for c in token):
                raise SettingsError("Could not read this connection's saved key. Update the key and try again.")
        return dict(fetch_endpoint_models(saved_url, key=token), ok=True)
    except (SettingsError, ProviderConnectionError) as error:
        raise SettingsError(str(error)) from None
    except RegistryError:
        raise SettingsError("The saved connection has an invalid endpoint URL.") from None
    except Exception:
        raise SettingsError("Could not load this connection's models. Check the saved connection and try again.") from None


def handle(request):
    action = request.get("action")
    if action == "endpoint_models":
        return endpoint_models(request)
    if action in ("cursor_status", "install_cursor_sdk", "cursor_models"):
        import cursor_sdk_runtime
        try:
            if action == "cursor_status":
                return cursor_sdk_runtime.status()
            if action == "install_cursor_sdk":
                return cursor_sdk_runtime.install()
            from routing_registry import CURSOR_BASE_URL
            fields = dict(request, model="cursor/auto", base_url=CURSOR_BASE_URL, wire="cursor")
            _, account, executable, _ = validated_model_fields(fields)
            completed = subprocess.run([executable, "--token", account], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
                                       check=False, text=True)
            token = completed.stdout.strip()
            if completed.returncode or not token or len(token) > 16384 or any(c.isspace() for c in token):
                raise SettingsError("Could not read the saved Cursor API key.")
            return cursor_sdk_runtime.list_models(token)
        except SettingsError:
            raise
        except Exception:
            raise SettingsError("The Cursor SDK operation failed. Check SDK installation and the saved Cursor key.") from None
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
    if action == "remove_agent":
        from routing_registry import RegistryError, RoutingRegistry
        model = request.get("model")
        if not isinstance(model, str) or not model:
            raise SettingsError("Choose a model to remove.")
        try:
            role = RoutingRegistry(agents_dir=request.get("agents_dir")).remove_model(model)
        except RegistryError as error:
            raise SettingsError(str(error)) from None
        except Exception:
            raise SettingsError("Could not remove that model. Check the managed agent files.") from None
        return {"ok": True, "agent_name": role, "message": f"Removed {model}. It leaves Codex's picker on the next turn."}
    if action not in ("status", "apply", "restore", "register_agent", "register_subscription_agent"):
        raise SettingsError("Unknown settings action.")
    config_path = Path(request.get("config_path", str(Path.home() / ".codex/config.toml"))).absolute()
    from routing_registry import support_directory
    state_dir = Path(request.get("state_dir", str(support_directory()))).absolute()
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
        if model.startswith("cursor/"):
            raise SettingsError("Add Cursor as a registered model; global Codex settings are preserved.")
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
