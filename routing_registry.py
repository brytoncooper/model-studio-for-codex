"""Read managed native agents and persist the picker selection without credentials."""

import json
import os
from pathlib import Path
import re
import tempfile
import tomllib
import urllib.parse
import uuid


AGENT_MARKER = "# Managed by OpenRouter Settings native-agent registration v1"
PROVIDER = "openrouter-settings"
CURSOR_BASE_URL = "https://api.cursor.com"
CURSOR_BILLING = "Uses Cursor SDK pricing and the IDE/Cloud Agents request pools; account limits and overages apply."
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
MAX_DISPLAY_NAME = 48
BRAND_WORDS = {"deepseek": "DeepSeek", "glm": "GLM", "gpt": "GPT", "llama": "Llama", "qwen": "Qwen", "qwq": "QwQ",
               "kimi": "Kimi", "grok": "Grok", "claude": "Claude", "gemini": "Gemini", "gemma": "Gemma",
               "mistral": "Mistral", "mixtral": "Mixtral", "minimax": "MiniMax", "phi": "Phi", "nemotron": "Nemotron",
               "hermes": "Hermes", "sonnet": "Sonnet", "opus": "Opus", "haiku": "Haiku", "flash": "Flash",
               "pro": "Pro", "max": "Max", "mini": "Mini", "nano": "Nano", "lite": "Lite", "instruct": "Instruct",
               "thinking": "Thinking", "coder": "Coder", "chat": "Chat", "latest": "Latest", "preview": "Preview",
               "free": "Free", "oss": "OSS", "r1": "R1", "v3": "V3", "turbo": "Turbo", "plus": "Plus"}


def friendly_model_name(model):
    """A short picker name for an OpenRouter model id: 'deepseek/deepseek-v4.1-flash' -> 'DeepSeek V4.1 Flash'."""
    name = str(model).lstrip("~")
    variant = None
    if ":" in name:
        name, variant = name.split(":", 1)
    if "/" in name:
        name = name.split("/", 1)[1]
    words = []
    for token in re.split(r"[-_]+", name):
        if not token:
            continue
        lower = token.lower()
        fused = re.match(r"^([a-z]+)(\d[\d.]*)$", lower)
        if lower in BRAND_WORDS:
            words.append(BRAND_WORDS[lower])
        elif fused and fused.group(1) in BRAND_WORDS:
            words.append(BRAND_WORDS[fused.group(1)] + " " + fused.group(2))
        elif re.match(r"^v\d", lower) or re.match(r"^\d+(\.\d+)?[bmk]$", lower):
            words.append(token.upper())
        elif lower[0].isdigit():
            words.append(token)
        else:
            words.append(token[:1].upper() + token[1:])
    text = " ".join(words) or str(model)
    if variant:
        text += f" ({variant[:1].upper() + variant[1:]})"
    return text[:MAX_DISPLAY_NAME]


def support_directory(home=None):
    """Model Deck's state folder. The app's original "Codex OpenRouter" folder is renamed once."""
    support = (Path(home) if home is not None else Path.home()) / "Library/Application Support"
    current, legacy = support / "Model Deck", support / "Codex OpenRouter"
    if not current.exists() and legacy.is_dir() and not legacy.is_symlink():
        try:
            legacy.rename(current)
        except OSError:
            return legacy
    elif current.is_dir() and legacy.is_dir() and not legacy.is_symlink():
        _absorb_legacy_logs(legacy, current)
    return current


def _absorb_legacy_logs(legacy, current):
    """A router started before the rename keeps appending to the old folder; fold those lines in."""
    for name in ("router.log", "router-ledger.jsonl"):
        source = legacy / name
        if not source.is_file() or source.is_symlink():
            continue
        try:
            with open(current / name, "ab") as target, open(source, "rb") as stream:
                target.write(stream.read())
            source.unlink()
        except OSError:
            return
    try:
        legacy.rmdir()  # only succeeds when nothing else is left behind
    except OSError:
        pass


def endpoint_description(endpoint, price=None):
    """One line for Codex's picker saying where a registered model runs, who pays, and the list price."""
    if endpoint.get("cursor"):
        text = "Routed through Cursor SDK by Model Deck. " + CURSOR_BILLING
    elif endpoint.get("openrouter"):
        text = "Routed to OpenRouter by Model Deck. Uses OpenRouter credits."
    else:
        from provider_connections import provider_billing_description
        billing = provider_billing_description(endpoint.get("base_url"), has_key=bool(endpoint.get("has_key")))
        text = f"Routed to {endpoint.get('name') or 'a custom endpoint'} by Model Deck. {billing}"
    return f"{text} {price}." if price else text


def valid_display_name(value):
    return (isinstance(value, str) and 0 < len(value.strip()) <= MAX_DISPLAY_NAME
            and not any(ord(character) < 32 for character in value))


class RegistryError(ValueError):
    """The managed routing files are unsafe or invalid."""


def reject_symlinks(path):
    if path.is_symlink() or path.parent.is_symlink():
        raise RegistryError(f"Symbolic links are not supported: {path.name}")


def valid_model(model):
    return (isinstance(model, str) and 0 < len(model) <= 256
            and not any(character.isspace() or ord(character) < 32 for character in model))


def validate_selection(selection):
    if not isinstance(selection, dict) or set(selection) != {"model", "effort"}:
        raise RegistryError("Picker selection must contain only model and effort.")
    if not valid_model(selection["model"]):
        raise RegistryError("Picker selection has an invalid model.")
    effort = selection["effort"]
    if effort is not None and (not isinstance(effort, str) or effort not in EFFORTS):
        raise RegistryError("Picker selection has an unsupported reasoning effort.")
    return selection


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
WIRE_FORMATS = ("auto", "responses", "chat", "cursor")


def is_cursor_url(value):
    """Only the fixed SDK origin is a Cursor route; aliases and paths are not."""
    return isinstance(value, str) and value.rstrip("/") == CURSOR_BASE_URL


def validate_cursor_route(model, base_url, wire=None):
    cursor = is_cursor_url(base_url)
    reserved = model.lower().lstrip("~").startswith("cursor/")
    if cursor and (not model.startswith("cursor/") or not model[7:] or "/" in model[7:]):
        raise RegistryError("Cursor SDK models require cursor/<SDK model id>.")
    if reserved and not cursor:
        raise RegistryError("Cursor model IDs require the fixed Cursor SDK endpoint.")
    if wire is not None and (wire not in WIRE_FORMATS or (wire == "cursor") != cursor):
        raise RegistryError("Cursor endpoints require the cursor wire format; other endpoints cannot use it.")
LOCAL_HOST_SUFFIXES = (".local", ".localhost", ".lan", ".home", ".internal")


def is_local_host(host):
    """Plain http is only accepted for hosts on this machine or the local network."""
    host = (host or "").lower().strip("[]")
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if host.endswith(LOCAL_HOST_SUFFIXES):
        return True
    octets = host.split(".")
    if len(octets) == 4 and all(part.isdigit() for part in octets):
        first, second = int(octets[0]), int(octets[1])
        return first == 10 or (first == 192 and second == 168) or (first == 172 and 16 <= second <= 31)
    return False


def validate_base_url(value):
    """An endpoint base URL such as https://openrouter.ai/api/v1 or http://localhost:1234/v1."""
    if not isinstance(value, str) or len(value) > 512 or any(ord(character) < 33 for character in value):
        raise RegistryError("Endpoint URL must be a single-line http(s) URL.")
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        raise RegistryError("Endpoint URL must be an http(s) URL without query or fragment.")
    if parts.scheme == "http" and not is_local_host(parts.hostname):
        raise RegistryError("Plain http endpoints are only allowed on this Mac or the local network.")
    return value.rstrip("/")


def is_openrouter_url(value):
    try:
        host = (urllib.parse.urlsplit(value).hostname or "").lower()
    except ValueError:
        return False
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def validate_provider(provider):
    allowed = {"name", "base_url", "wire_api", "supports_websockets", "auth"}
    if not isinstance(provider, dict) or not {"name", "base_url", "wire_api", "supports_websockets"} <= set(provider) \
            or not set(provider) <= allowed:
        raise RegistryError("Managed provider must use only the approved endpoint fields.")
    if (not isinstance(provider["name"], str) or not 0 < len(provider["name"].strip()) <= 64
            or any(ord(character) < 32 for character in provider["name"])):
        raise RegistryError("Managed provider has an invalid endpoint name.")
    validate_base_url(provider["base_url"])
    if provider["wire_api"] != "responses" or provider["supports_websockets"] is not False:
        raise RegistryError("Managed provider has unexpected transport settings.")
    if "auth" not in provider:
        if is_openrouter_url(provider["base_url"]) or is_cursor_url(provider["base_url"]):
            raise RegistryError("OpenRouter and Cursor endpoints require a saved API key.")
        return provider
    auth = provider["auth"]
    if not isinstance(auth, dict) or set(auth) != {"command", "args", "timeout_ms", "refresh_interval_ms"}:
        raise RegistryError("Managed provider requires command authentication without literal secrets.")
    command = auth["command"]
    if (not isinstance(command, str) or not Path(command).is_absolute()
            or any(ord(character) < 32 for character in command)):
        raise RegistryError("Credential command must be an absolute path.")
    arguments = auth["args"]
    if (not isinstance(arguments, list) or len(arguments) != 2
            or arguments[0] != "--token" or not isinstance(arguments[1], str)):
        raise RegistryError("Credential command must reference a key account UUID.")
    try:
        if str(uuid.UUID(arguments[1])) != arguments[1].lower():
            raise ValueError()
    except ValueError:
        raise RegistryError("Credential command has an invalid key account UUID.") from None
    for setting in ("timeout_ms", "refresh_interval_ms"):
        if type(auth[setting]) is not int or auth[setting] <= 0:
            raise RegistryError("Credential timing settings must be positive integers.")
    return provider


def reject_secret_fields(document):
    if isinstance(document, dict):
        for name, value in document.items():
            if name in {"experimental_bearer_token", "env_key", "api_key", "bearer_token", "http_headers", "env_http_headers"}:
                raise RegistryError("Managed agent contains unsupported credential fields.")
            reject_secret_fields(value)
    elif isinstance(document, list):
        for value in document:
            reject_secret_fields(value)


class RoutingRegistry:
    def __init__(self, agents_dir=None, preferences_path=None, selection_path=None, display_names_path=None):
        support = support_directory()
        self.agents_dir = Path(agents_dir) if agents_dir is not None else Path.home() / ".codex/agents"
        # Preferences are intentionally not read: registration is the routing authority.
        self.preferences_path = Path(preferences_path) if preferences_path is not None else support / "preferences.json"
        self.selection_path = Path(selection_path) if selection_path is not None else support / "picker-selection.json"
        self.display_names_path = (Path(display_names_path) if display_names_path is not None
                                   else support / "display-names.json")
        self.endpoints_path = support / "endpoints.json"

    def load_endpoints(self):
        """Endpoint settings the app keeps beside the keys: wire format by key account or base URL."""
        try:
            if self.endpoints_path.is_symlink() or not self.endpoints_path.is_file():
                return {}
            document = json.loads(self.endpoints_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(document, dict):
            return {}
        endpoints = {}
        for key, entry in document.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            wire = entry.get("wire") if entry.get("wire") in WIRE_FORMATS else "auto"
            endpoints[key] = {"wire": wire, "name": entry.get("name") if isinstance(entry.get("name"), str) else None}
        return endpoints

    @staticmethod
    def endpoint_summary(provider, endpoints):
        """What the router needs to reach a model's endpoint; never includes a key."""
        auth = provider.get("auth")
        account = auth["args"][1] if auth else None
        base_url = provider["base_url"].rstrip("/")
        settings = (endpoints.get(account) if account else None) or endpoints.get(base_url) or {}
        cursor = is_cursor_url(base_url)
        if settings.get("wire") == "cursor" and not cursor:
            raise RegistryError("Cursor wire requires the fixed Cursor SDK endpoint.")
        summary = {"name": provider["name"], "base_url": base_url, "has_key": auth is not None, "account": account,
                   "openrouter": is_openrouter_url(base_url), "wire": "cursor" if cursor else settings.get("wire", "auto")}
        if cursor:
            summary["cursor"] = True
        return summary

    def load_display_names(self):
        """Custom picker names by model id. Display only, so a bad file is ignored rather than fatal."""
        try:
            if self.display_names_path.is_symlink() or not self.display_names_path.is_file():
                return {}
            document = json.loads(self.display_names_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(document, dict):
            return {}
        return {model: name.strip() for model, name in document.items()
                if valid_model(model) and valid_display_name(name)}

    def display_name_for(self, model):
        return self.load_display_names().get(model) or friendly_model_name(model)

    def set_display_name(self, model, name):
        """Write (or, with an empty name, clear) the custom picker name for one model."""
        if not valid_model(model):
            raise RegistryError("Invalid model id.")
        if name is not None and name.strip() and not valid_display_name(name):
            raise RegistryError("Display names must be 1-64 printable characters.")
        names = self.load_display_names()
        if name is not None and name.strip():
            names[model] = name.strip()
        else:
            names.pop(model, None)
        path = self.display_names_path
        reject_symlinks(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".display-names-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                json.dump(names, temporary, sort_keys=True, indent=2, ensure_ascii=False)
                temporary.write("\n")
            os.chmod(temporary_name, 0o600)
            reject_symlinks(path)
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return names

    def remove_model(self, model):
        """Delete the managed role file that registers `model`; returns the role name."""
        entry = self.load_models().get(model)
        if entry is None:
            raise RegistryError(f"{model} is not an added model.")
        path = self.agents_dir / (entry["role"] + ".toml")
        reject_symlinks(path)
        if not path.read_text(encoding="utf-8").startswith(AGENT_MARKER + "\n"):
            raise RegistryError("That role file is not managed by Model Deck; it was left alone.")
        path.unlink()
        self.set_display_name(model, None)
        return entry["role"]

    def load_models(self):
        reject_symlinks(self.agents_dir)
        models = {}
        endpoints = self.load_endpoints()
        for path in sorted(self.agents_dir.glob("openrouter_*.toml")):
            reject_symlinks(path)
            try:
                source = path.read_text(encoding="utf-8")
                if not source.startswith(AGENT_MARKER + "\n"):
                    continue
                agent = tomllib.loads(source)
            except (OSError, UnicodeError, tomllib.TOMLDecodeError):
                raise RegistryError(f"Cannot read managed agent: {path.name}") from None
            reject_secret_fields(agent)
            model = agent.get("model")
            if not valid_model(model) or model.lower().lstrip("~").startswith(("openai/", "gpt-", "codex-")):
                raise RegistryError(f"Managed agent has an invalid model id: {path.name}")
            if agent.get("model_provider") != PROVIDER:
                raise RegistryError(f"Managed agent has an unexpected provider: {path.name}")
            role = agent.get("name")
            if role != path.stem:
                raise RegistryError(f"Managed agent name does not match its filename: {path.name}")
            providers = agent.get("model_providers")
            if not isinstance(providers, dict) or set(providers) != {PROVIDER}:
                raise RegistryError(f"Managed agent has unexpected provider definitions: {path.name}")
            provider = validate_provider(providers[PROVIDER])
            validate_cursor_route(model, provider["base_url"])
            if is_openrouter_url(provider["base_url"]) and ("/" not in model or not all(model.split("/", 1))):
                raise RegistryError(f"OpenRouter models need a provider/model id: {path.name}")
            if model in models:
                raise RegistryError(f"Multiple managed agents register the same model: {model}")
            models[model] = {"provider": PROVIDER, "config": {"model_providers": {PROVIDER: provider}},
                             "role": role, "endpoint": self.endpoint_summary(provider, endpoints)}
        return models

    def catalog_entries(self, price_lines=None):
        """Picker rows for the desktop's model/list; `price_lines` maps model id -> list-price text."""
        names = self.load_display_names()
        price_lines = price_lines or {}
        return [{"id": model, "model": model, "displayName": names.get(model) or friendly_model_name(model),
                 "description": endpoint_description(entry["endpoint"], price_lines.get(model)),
                 "hidden": False, "isDefault": False, "defaultReasoningEffort": "low",
                 "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": "Low reasoning"},
                                               {"reasoningEffort": "medium", "description": "Medium reasoning"},
                                               {"reasoningEffort": "high", "description": "High reasoning"}],
                 "inputModalities": ["text"], "supportsPersonality": False}
                for model, entry in self.load_models().items()]

    def selected(self):
        reject_symlinks(self.selection_path)
        if not self.selection_path.exists():
            return {}
        try:
            selection = json.loads(self.selection_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise RegistryError("Cannot read picker selection; existing file was preserved.") from None
        return validate_selection(selection)

    def select(self, model, effort=None):
        selection = validate_selection({"model": model, "effort": effort})
        self.selected()  # Never replace an unknown or malformed existing document.
        path = self.selection_path
        reject_symlinks(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".picker-selection-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                json.dump(selection, temporary, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            reject_symlinks(path)
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return selection
