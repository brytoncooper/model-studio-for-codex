"""Read managed native agents and persist the picker selection without credentials."""

import json
import os
from pathlib import Path
import tempfile
import tomllib
import uuid


AGENT_MARKER = "# Managed by OpenRouter Settings native-agent registration v1"
PROVIDER = "openrouter-settings"
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


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


def validate_provider(provider):
    allowed = {"name", "base_url", "wire_api", "supports_websockets", "auth"}
    if not isinstance(provider, dict) or set(provider) != allowed:
        raise RegistryError("Managed provider must use only the approved command-auth fields.")
    if (provider["name"] != "OpenRouter"
            or provider["base_url"] != "https://openrouter.ai/api/v1"
            or provider["wire_api"] != "responses"
            or provider["supports_websockets"] is not False):
        raise RegistryError("Managed provider has unexpected transport settings.")
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
    def __init__(self, agents_dir=None, preferences_path=None, selection_path=None):
        support = Path.home() / "Library/Application Support/Codex OpenRouter"
        self.agents_dir = Path(agents_dir) if agents_dir is not None else Path.home() / ".codex/agents"
        # Preferences are intentionally not read: registration is the routing authority.
        self.preferences_path = Path(preferences_path) if preferences_path is not None else support / "preferences.json"
        self.selection_path = Path(selection_path) if selection_path is not None else support / "picker-selection.json"

    def load_models(self):
        reject_symlinks(self.agents_dir)
        models = {}
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
            if (not valid_model(model) or "/" not in model or not all(model.split("/", 1))
                    or model.lower().lstrip("~").startswith("openai/")):
                raise RegistryError(f"Managed agent has an invalid OpenRouter model: {path.name}")
            if agent.get("model_provider") != PROVIDER:
                raise RegistryError(f"Managed agent has an unexpected provider: {path.name}")
            role = agent.get("name")
            if role != path.stem:
                raise RegistryError(f"Managed agent name does not match its filename: {path.name}")
            providers = agent.get("model_providers")
            if not isinstance(providers, dict) or set(providers) != {PROVIDER}:
                raise RegistryError(f"Managed agent has unexpected provider definitions: {path.name}")
            provider = validate_provider(providers[PROVIDER])
            if model in models:
                raise RegistryError(f"Multiple managed agents register the same model: {model}")
            models[model] = {"provider": PROVIDER,
                             "config": {"model_providers": {PROVIDER: provider}}, "role": role}
        return models

    def catalog_entries(self):
        return [{"id": model, "model": model, "displayName": f"{model} · OpenRouter",
                 "description": "Native agent model billed to OpenRouter API credits.",
                 "hidden": False, "isDefault": False, "defaultReasoningEffort": "low",
                 "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": "Low reasoning"}],
                 "inputModalities": ["text"], "supportsPersonality": False}
                for model in self.load_models()]

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
