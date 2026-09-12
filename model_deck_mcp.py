"""Model Deck's MCP server: lets a Codex agent discover models on OpenRouter or any saved endpoint
and add them to Model Deck itself. Stdio JSON-RPC. It can only add models to endpoints the user
already saved; it never sees or writes API keys (keyed lookups go through the credential helper)."""
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

_VENDOR = Path(__file__).resolve().with_name("vendor")
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

# Source checkouts carry the package here; staged bundles ship it in vendor.
# An incomplete bundle must fail explicitly, never silently bypass use cases.
_SOURCE_PYTHON = Path(__file__).resolve().parent / "python" / "src"
if (_SOURCE_PYTHON / "model_deck" / "__init__.py").is_file():
    sys.path.insert(0, str(_SOURCE_PYTHON))
elif not (_VENDOR / "model_deck" / "__init__.py").is_file():
    raise SystemExit(
        "Model Deck MCP requires its packaged engine service in Resources/vendor; "
        "the Architecture source checkout may instead supply python/src/model_deck."
    )
try:
    from model_deck.engine.model_library.use_cases import ListModelsUseCase
    from model_deck.integrations.clients.mcp import McpModelReadService, McpReadError, as_legacy_adapter
    from model_deck.integrations.clients.mcp.registry_snapshot import McpRegistrySnapshot
except ModuleNotFoundError as error:
    raise SystemExit(
        "Model Deck MCP requires its packaged engine service. Run from the Architecture source "
        "checkout or stage the engine package and its dependencies in Resources/vendor."
    ) from error

import codex_settings  # noqa: E402
import pricing  # noqa: E402
import model_benchmarks  # noqa: E402
from provider_connections import (ProviderConnectionError, fetch_catalog_json, fetch_endpoint_models,  # noqa: E402
                                  provider_billing_description, provider_for_base_url)
from routing_registry import (RegistryError, RoutingRegistry, WIRE_FORMATS, friendly_model_name,  # noqa: E402
                              is_openrouter_url, validate_base_url, valid_model)

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "model-deck", "version": "1.9"}
SERVER_INSTRUCTIONS = ("Model Deck discovers and registers models on saved endpoints. gpt-* ids stay on the ChatGPT subscription; "
                       "cursor/<SDK id> uses a saved Cursor endpoint and Cursor subscription; other models use their saved endpoint. "
                       "Use list_endpoints, search_models, and add_model, then pick or spawn the exact registered id next turn. "
                       "For evidence-based selection use benchmark_status, rank_models, model_benchmarks and compare_models. "
                       "Scores are published evidence with incomplete coverage; compare within the same test, never combine scales. "
                       "Use cursor_status for setup; SDK installation is an explicit action in Model Deck's UI. "
                       "Cursor runs with only Codex-provided MCP callbacks, so Codex remains the tool approval boundary.")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
MAX_LINE = 1024 * 1024
MAX_LIMIT = 50
CURSOR_BILLING = "Uses Cursor subscription IDE/Cloud usage pools according to the account plan; overage charges may apply. It does not use OpenRouter credits."
EFFORTS = ("default", "low", "medium", "high", "xhigh")


class DeckError(Exception):
    """A message for the calling agent; never includes secrets."""


def app_executable():
    """The credential helper's launcher: Contents/MacOS/ModelDeck next to this script's Resources folder."""
    candidate = Path(__file__).resolve().parent.parent / "MacOS" / "ModelDeck"
    return str(candidate) if candidate.is_file() else "/Applications/Model Deck.app/Contents/MacOS/ModelDeck"


def fetch_json(url, headers=None, timeout=15):
    return fetch_catalog_json(url, headers, timeout)


def endpoint_billing(entry):
    """Billing provenance follows the saved connection, never the model's name."""
    if entry.get("cursor"):
        return {"billing": "Cursor subscription", "billing_note": CURSOR_BILLING}
    if entry.get("openrouter"):
        return {"billing": "OpenRouter credits", "billing_note": None}
    keyed = bool(entry.get("keyed", entry.get("has_key", False)))
    provider = provider_for_base_url(entry.get("base_url"))
    unkeyed = "none (local)" if str(entry.get("base_url", "")).startswith("http://") else "endpoint managed"
    return {"billing": (provider["billing"] if keyed else "key required") if provider else "API key" if keyed else unkeyed,
            "billing_note": provider_billing_description(entry.get("base_url"), keyed)}


class Deck:
    def __init__(self, registry=None, pricing_loader=None, executable=None, fetch=None, settings=None, benchmarks=None):
        self.registry = registry or RoutingRegistry()
        self.pricing_loader = pricing_loader or pricing.load
        self.executable = executable or app_executable()
        self.fetch = fetch or fetch_json
        self.settings = dict(settings or {})
        self.benchmarks = benchmarks

    # -- endpoints -----------------------------------------------------------------------------

    def _read_json(self, path):
        try:
            if path.is_symlink() or not path.is_file():
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None

    def endpoints(self):
        """Saved endpoints from endpoints.json (or, for older installs, the preferences file)."""
        document = self._read_json(self.registry.endpoints_path)
        entries = []
        if isinstance(document, dict):
            for key, entry in document.items():
                if isinstance(key, str) and isinstance(entry, dict) and isinstance(entry.get("base_url"), str):
                    entries.append((key, entry.get("name"), entry["base_url"], entry.get("wire")))
        else:
            preferences = self._read_json(self.registry.preferences_path) or {}
            for account in preferences.get("accounts") or []:
                if isinstance(account, dict) and isinstance(account.get("id"), str):
                    keyed = account.get("hasKey") is not False
                    base_url = account.get("baseURL") or pricing.PRICING_URL.rsplit("/models", 1)[0]
                    entries.append((account["id"] if keyed else base_url, account.get("name"), base_url, account.get("wire")))
        endpoints = []
        for key, name, base_url, wire in entries:
            base_url = base_url.rstrip("/")
            try:
                validate_base_url(base_url)
            except RegistryError:
                continue
            keyed = bool(UUID_RE.match(key))
            endpoints.append({"name": name if isinstance(name, str) and name.strip() else base_url,
                              "base_url": base_url, "keyed": keyed, "account": key if keyed else None,
                              "wire": wire if wire in WIRE_FORMATS else "auto", "openrouter": is_openrouter_url(base_url),
                              "cursor": wire == "cursor" and base_url == "https://api.cursor.com"})
        endpoints.sort(key=lambda entry: (not entry["openrouter"], entry["name"].lower()))
        return endpoints

    def endpoint_named(self, name=None):
        endpoints = self.endpoints()
        if not endpoints:
            raise DeckError("No endpoints are saved in Model Deck. Ask the user to add one on its Endpoints page.")
        names = ", ".join(entry["name"] for entry in endpoints)
        if name is None or not str(name).strip():
            if len(endpoints) == 1:
                return endpoints[0]
            openrouter = [entry for entry in endpoints if entry["openrouter"]]
            if len(openrouter) == 1:
                return openrouter[0]
            raise DeckError("Say which endpoint to use. Saved endpoints: " + names)
        wanted = str(name).strip().lower().rstrip("/")
        for entry in endpoints:
            if entry["name"].lower() == wanted or entry["base_url"].lower() == wanted:
                return entry
        if wanted == "openrouter":
            openrouter = [entry for entry in endpoints if entry["openrouter"]]
            if len(openrouter) == 1:
                return openrouter[0]
        raise DeckError(f"No endpoint named {name!r}. Saved endpoints: {names}")

    def saved_endpoint_name(self, summary):
        """The user's saved name for a registry endpoint summary; falls back to the role file's name."""
        for entry in self.endpoints():
            if summary.get("account") and entry["account"] == summary.get("account"):
                return entry["name"]
            if not summary.get("has_key") and entry["base_url"] == (summary.get("base_url") or "").rstrip("/"):
                return entry["name"]
        return summary.get("name") or "OpenRouter"

    def _key_for_account(self, account):
        try:
            completed = subprocess.run([self.executable, "--token", account], stdin=subprocess.DEVNULL,
                                       capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            raise DeckError("That endpoint's API key could not be read from the Keychain.") from None
        key = completed.stdout.decode("utf-8", "replace").strip()
        if completed.returncode != 0 or not key:
            raise DeckError("That endpoint's API key could not be read from the Keychain. Open Model Deck → Endpoints → Test to authorize it.")
        return key

    # -- tools ---------------------------------------------------------------------------------

    def list_endpoints(self):
        return {"endpoints": [{"name": entry["name"], "base_url": entry["base_url"],
                               **endpoint_billing(entry),
                               "format": entry["wire"]} for entry in self.endpoints()]}

    @staticmethod
    def _describe(model_id, entry, endpoint_name):
        return {"id": model_id, "name": entry.get("name") or friendly_model_name(model_id), "endpoint": endpoint_name,
                "price": pricing.price_line(entry) or "not listed",
                "input_per_million": entry.get("input"), "output_per_million": entry.get("output"),
                "cache_read_per_million": entry.get("cache_read"), "context": entry.get("context"),
                "modalities": entry.get("modalities"), "tools": entry.get("tools"), "reasoning": entry.get("reasoning"),
                "description": (entry.get("description") or "")[:200]}

    def search_models(self, query, endpoint=None, limit=20):
        target = self.endpoint_named(endpoint)
        try:
            limit = max(1, min(int(limit), MAX_LIMIT))
        except (TypeError, ValueError):
            limit = 20
        terms = [term for term in re.split(r"\s+", str(query or "").lower()) if term]
        if target["cursor"]:
            if not target["keyed"]:
                raise DeckError("Cursor requires a saved API key. Add it in Model Deck → Endpoints.")
            try:
                document = codex_settings.handle({**self.settings, "action": "cursor_models", "account": target["account"], "executable": self.executable})
            except Exception:
                raise DeckError("Cursor models could not be discovered. Use cursor_status and Model Deck → Endpoints → Test.") from None
            rows = document.get("models", [])
            matching = [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
                        and all(term in (row["id"] + " " + str(row.get("name", ""))).lower() for term in terms)]
            return {"endpoint": target["name"], "matches": len(matching), "billing": "Cursor subscription", "billing_note": CURSOR_BILLING,
                    "models": [{"id": row["id"] if row["id"].startswith("cursor/") else "cursor/" + row["id"],
                                "name": row.get("name") or row["id"], "endpoint": target["name"], "price": "Cursor subscription"}
                               for row in matching[:limit]]}
        if target["openrouter"]:
            table = self.pricing_loader()
            if not table:
                raise DeckError("OpenRouter's catalog could not be loaded right now.")
            ranked = []
            for model_id, entry in table.items():
                haystack = " ".join([model_id, entry.get("name") or "", entry.get("description") or ""]).lower()
                if all(term in haystack for term in terms):
                    score = sum(2 for term in terms if term in model_id.lower()) + sum(1 for term in terms if term in (entry.get("name") or "").lower())
                    ranked.append((-score, model_id, entry))
            ranked.sort(key=lambda row: (row[0], row[1]))
            return {"endpoint": target["name"], "matches": len(ranked),
                    "models": [self._describe(model_id, entry, target["name"]) for _, model_id, entry in ranked[:limit]]}
        key = self._key_for_account(target["account"]) if target["keyed"] else None
        try:
            document = fetch_endpoint_models(target["base_url"], key, self.fetch)
        except ProviderConnectionError as error:
            raise DeckError(str(error)) from None
        matching = [row for row in document["models"]
                    if all(term in (row["id"] + " " + row["name"] + " " + row.get("description", "")).lower() for term in terms)]
        return {"endpoint": target["name"], "matches": len(matching), **endpoint_billing(target),
                "source": document["source"], "verified": document["verified"], "note": document["note"],
                "models": [{**row, "endpoint": target["name"], "price": "not listed"} for row in matching[:limit]]}

    def list_added_models(self):
        try:
            models = self.registry.load_models()
        except RegistryError as error:
            raise DeckError(str(error)) from None
        return {"models": self._registered_model_rows(models),
                "how_to_use": "Pick a model in Codex's picker, or spawn_agent with model set to the exact id."}

    def _registered_model_rows(self, models):
        """Shared legacy formatting for direct reads and application snapshots."""
        table = self.pricing_loader() if any((entry.get("endpoint") or {}).get("openrouter") for entry in models.values()) else {}
        rows = []
        for model_id, entry in sorted(models.items()):
            endpoint = entry.get("endpoint") or {}
            rows.append({"id": model_id, "name": self.registry.display_name_for(model_id),
                         "endpoint": self.saved_endpoint_name(endpoint),
                         **endpoint_billing(endpoint),
                         "price": (pricing.price_line(pricing.pricing_for(model_id, table)) or "not listed") if endpoint.get("openrouter") else "n/a",
                         "role": entry["role"]})
        return rows

    def _model_read_service(self, name):
        rows, connection_ids = [], {}
        if name == "list_added_models":
            try:
                models = self.registry.load_models()
            except RegistryError as error:
                raise DeckError(str(error)) from None
            rows = self._registered_model_rows(models)
            for model_id, entry in models.items():
                endpoint = entry.get("endpoint") or {}
                # The registry already normalizes captured URLs and resolves wire.
                # One account can appear in agents with different captured routes.
                identity = [endpoint.get("account"), endpoint.get("base_url"), endpoint.get("wire")]
                connection_ids[model_id] = str(uuid.uuid5(
                    uuid.NAMESPACE_URL, "model-deck:mcp:connection:" + json.dumps(identity)))
        snapshot = McpRegistrySnapshot(rows, connection_ids)
        return McpModelReadService(
            list_models=ListModelsUseCase(snapshot), legacy=as_legacy_adapter(self),
            presentation=snapshot, catalog_resolver=None,
        )

    def add_model(self, model, endpoint=None, display_name=None, effort=None):
        if not isinstance(model, str) or not valid_model(model.strip()):
            raise DeckError("Give a model id without spaces, for example deepseek/deepseek-v4.1-flash.")
        model = model.strip()
        effort = effort or "default"
        if effort not in EFFORTS:
            raise DeckError("effort must be one of " + ", ".join(EFFORTS))
        target = self.endpoint_named(endpoint)
        request = {"action": "register_agent", "model": model, "executable": self.executable,
                   "base_url": target["base_url"], "endpoint_name": target["name"], "wire": target["wire"],
                   "effort": effort, **self.settings}
        if target["keyed"]:
            request["account"] = target["account"]
        try:
            result = codex_settings.handle(request)
        except codex_settings.SettingsError as error:
            raise DeckError(str(error)) from None
        if display_name is not None and str(display_name).strip():
            try:
                self.registry.set_display_name(model, str(display_name))
            except RegistryError as error:
                raise DeckError(f"Added {model}, but the display name was rejected: {error}") from None
        warning = ""
        price = ""
        if target["openrouter"]:
            table = self.pricing_loader()
            entry = pricing.pricing_for(model, table)
            price = pricing.price_line(entry)
            if table and entry is None:
                warning = " Warning: OpenRouter's catalog does not list this id; check the spelling with search_models."
        already_registered = result.get("already_registered") is True
        return {"ok": True, "model": model, "endpoint": target["name"], "role": result.get("agent_name"),
                "already_registered": already_registered,
                "name": self.registry.display_name_for(model), "price": "Cursor subscription" if target["cursor"] else price or "n/a",
                **endpoint_billing(target),
                "message": (f"{model} is already added on {target['name']}. " if already_registered else f"Added {model} on {target['name']}. ")
                           + "It appears in Codex's picker on the next turn; "
                           f"spawn it with spawn_agent model=\"{model}\".{warning}"}

    def remove_model(self, model):
        if not isinstance(model, str) or not model.strip():
            raise DeckError("Say which model id to remove.")
        try:
            role = self.registry.remove_model(model.strip())
        except RegistryError as error:
            raise DeckError(str(error)) from None
        return {"ok": True, "model": model.strip(), "role": role, "message": f"Removed {model.strip()}. It leaves Codex's picker on the next turn."}

    def set_display_name(self, model, name):
        if not isinstance(model, str) or model.strip() not in self._added_ids():
            raise DeckError("That is not an added model. Use list_added_models to see the ids.")
        try:
            self.registry.set_display_name(model.strip(), name if isinstance(name, str) else None)
        except RegistryError as error:
            raise DeckError(str(error)) from None
        return {"ok": True, "model": model.strip(), "name": self.registry.display_name_for(model.strip())}

    def model_pricing(self, model):
        if not isinstance(model, str) or not model.strip():
            raise DeckError("Say which model id to price.")
        entry = pricing.pricing_for(model.strip(), self.pricing_loader())
        if entry is None:
            return {"id": model.strip(), "price": "not listed on OpenRouter", "note": "Local and other-provider endpoints do not publish prices here."}
        return self._describe(model.strip(), entry, "OpenRouter")

    def cursor_status(self):
        try:
            status = codex_settings.handle({**self.settings, "action": "cursor_status", "executable": self.executable})
        except Exception:
            raise DeckError("Cursor SDK status could not be read. Open Model Deck → Endpoints.") from None
        # Whitelist status fields; never return credentials or arbitrary helper output.
        return {"sdk": {key: status[key] for key in ("ok", "installed", "version", "required_version") if key in status},
                "saved_endpoints": [row["name"] for row in self.endpoints() if row["cursor"]],
                "setup": "Open Model Deck → Endpoints, add Cursor with its API key, explicitly install the Cursor SDK if needed, then Test.",
                "billing": "Cursor subscription", "billing_note": CURSOR_BILLING}

    def _benchmarks(self, ensure=True):
        if self.benchmarks is None:
            self.benchmarks = model_benchmarks.BenchmarkStore()
        return self.benchmarks.ensure() if ensure else self.benchmarks

    def benchmark_status(self):
        return self._benchmarks(ensure=False).status()

    def refresh_benchmarks(self, authenticated=False, endpoint=None):
        headers = None
        if authenticated:
            target = self.endpoint_named(endpoint)
            if not target["openrouter"] or not target["keyed"]:
                raise DeckError("Authenticated benchmarks require a saved OpenRouter endpoint with an API key.")
            headers = {"Authorization": "Bearer " + self._key_for_account(target["account"])}
        return self._benchmarks(ensure=False).refresh(headers=headers)

    def model_benchmarks(self, model, limit=100, offset=0):
        return self._benchmarks().profile(model, limit, offset)

    def compare_models(self, models, task=None, limit=20, offset=0):
        return self._benchmarks().compare(models, task, limit, offset)

    def rank_models(self, task, source="artificial-analysis", metric=None, arena="models", limit=20, offset=0):
        if source == "artificial-analysis" and task not in ("coding", "intelligence", "agentic"):
            raise DeckError("Artificial Analysis tasks are coding, intelligence and agentic. Use benchmark_status for Design Arena categories.")
        if metric is not None and metric not in (("coding_index", "intelligence_index", "agentic_index") if source == "artificial-analysis" else ("elo", "win_rate", "rank", "avg_generation_time_ms")):
            raise DeckError("That metric does not belong to the selected source.")
        return self._benchmarks().rank(task, source, metric, arena, limit, offset)

    def _added_ids(self):
        try:
            return set(self.registry.load_models())
        except RegistryError:
            return set()

    def call(self, name, arguments):
        if not isinstance(arguments, dict):
            raise DeckError("Tool arguments must be an object.")
        tool = TOOL_INDEX.get(name) if isinstance(name, str) else None
        if tool is None:
            raise DeckError(f"Unknown tool {name!r}.")
        allowed = set(tool["inputSchema"].get("properties", {}))
        unknown = set(arguments) - allowed
        if unknown:
            raise DeckError("Unknown arguments: " + ", ".join(sorted(unknown)))
        for key, value in arguments.items():
            schema = tool["inputSchema"]["properties"][key]
            expected = schema["type"]
            valid = {"string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool),
                     "boolean": isinstance(value, bool), "array": isinstance(value, list)}.get(expected, False)
            if not valid:
                raise DeckError(f"Argument {key} must be a {expected}.")
            if "enum" in schema and value not in schema["enum"]:
                raise DeckError(f"Argument {key} must be one of " + ", ".join(schema["enum"]))
            if expected == "integer" and not schema.get("minimum", 0) <= value <= schema.get("maximum", 100000):
                raise DeckError(f"Argument {key} is outside the allowed bounds.")
            if expected == "string" and len(value) > schema.get("maxLength", 500):
                raise DeckError(f"Argument {key} is too long.")
            if expected == "array":
                if not schema.get("minItems", 1) <= len(value) <= schema.get("maxItems", 10):
                    raise DeckError(f"Argument {key} has too many or too few items.")
                if any(not isinstance(item, str) or not item or len(item) > 300 for item in value) or len(set(value)) != len(value):
                    raise DeckError(f"Argument {key} requires unique, nonempty model ids of at most 300 characters.")
        missing = [key for key in tool["inputSchema"].get("required", []) if key not in arguments]
        if missing:
            raise DeckError("Missing arguments: " + ", ".join(missing))
        if name in {"list_added_models", "search_models", "list_endpoints"}:
            try:
                return getattr(self._model_read_service(name), name)(**arguments)
            except McpReadError as error:
                raise DeckError(str(error)) from None
        return getattr(self, name)(**arguments)


TOOLS = [
    {"name": "list_endpoints", "description": "Endpoints saved in Model Deck (OpenRouter, other providers, local servers) that models can be added to.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "search_models", "description": "Find models to add. On OpenRouter this searches its public catalog with list prices per million tokens, context size, and tool support; on another saved endpoint it lists that server's models.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Words matched against the model id, name, and description, e.g. 'deepseek', 'kimi k2', 'free coder'."},
         "endpoint": {"type": "string", "description": "Saved endpoint name. Defaults to OpenRouter."},
         "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Maximum results (1-50, default 20)."}},
         "required": ["query"], "additionalProperties": False}},
    {"name": "list_added_models", "description": "Models already added to Model Deck, with their endpoint, billing, and OpenRouter list price. These can be picked in Codex or spawned by exact id.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "add_model", "description": "Add a model to Model Deck on a saved endpoint so Codex can pick it and agents can spawn it by id on the next turn. gpt-* ids are refused; OpenRouter ids are provider/model.",
     "inputSchema": {"type": "object", "properties": {
         "model": {"type": "string", "description": "Exact model id, e.g. deepseek/deepseek-v4.1-flash or qwen2.5-0.5b-instruct-mlx."},
         "endpoint": {"type": "string", "description": "Saved endpoint name. Defaults to OpenRouter when it is the only one or the only OpenRouter endpoint."},
         "display_name": {"type": "string", "description": "Optional short name shown in Codex's picker (max 64 characters)."},
         "effort": {"type": "string", "description": "Default reasoning effort: default, low, medium, high, or xhigh."}},
         "required": ["model"], "additionalProperties": False}},
    {"name": "remove_model", "description": "Remove a model that was added through Model Deck. Its endpoint and key stay.",
     "inputSchema": {"type": "object", "properties": {"model": {"type": "string", "description": "Exact id of an added model."}},
                     "required": ["model"], "additionalProperties": False}},
    {"name": "set_display_name", "description": "Change the picker name of an added model. An empty name restores the automatic one.",
     "inputSchema": {"type": "object", "properties": {"model": {"type": "string"}, "name": {"type": "string"}},
                     "required": ["model", "name"], "additionalProperties": False}},
    {"name": "model_pricing", "description": "OpenRouter list price, context size, and capabilities for one model id.",
     "inputSchema": {"type": "object", "properties": {"model": {"type": "string"}}, "required": ["model"], "additionalProperties": False}},
]
def _tool(name, description, properties=None, required=None):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}}


_LIMIT = {"type": "integer", "minimum": 1, "maximum": 50}
_OFFSET = {"type": "integer", "minimum": 0, "maximum": 20000}
_MODEL = {"type": "string", "maxLength": 300}
_TASK = {"type": "string", "maxLength": 100, "description": "coding/intelligence/agentic for Artificial Analysis; an exact published category from benchmark_status for Design Arena."}
TOOLS.extend([
    _tool("cursor_status", "Read Cursor SDK setup status and subscription billing context. Does not install software or read keys."),
    _tool("benchmark_status", "Inspect cached benchmark coverage, supported tasks, source URLs, freshness, failures and unmatched identities. Does not fetch; use refresh_benchmarks to update."),
    _tool("refresh_benchmarks", "Refresh public OpenRouter catalog scores across Artificial Analysis and Design Arena. Optional authenticated=true adds publisher citation/timestamps from four official benchmark feeds using an existing saved OpenRouter key. No inference calls; bounded fetches, stale data retained on failure.",
          {"authenticated": {"type": "boolean"}, "endpoint": {"type": "string"}}),
    _tool("model_benchmarks", "Published scores for an exact OpenRouter catalog id with source, metric direction, identity match and freshness. No fuzzy joins to Cursor or display names. Missing scores remain missing.",
          {"model": _MODEL, "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "offset": _OFFSET}, ["model"]),
    _tool("compare_models", "Compare up to ten exact model ids separately within each published test. Missing models are explicit; no cross-benchmark average. Page comparisons with offset.",
          {"models": {"type": "array", "items": _MODEL, "minItems": 1, "maxItems": 10, "uniqueItems": True}, "task": _TASK, "limit": _LIMIT, "offset": _OFFSET}, ["models"]),
    _tool("rank_models", "Find benchmark-backed task candidates within ONE source/test. Includes provenance and gaps; catalog presence is not provider uptime or proven agent success. Inspect benchmark_status for categories; profile candidates before adding.",
          {"task": _TASK, "source": {"type": "string", "enum": ["artificial-analysis", "design-arena"]},
           "metric": {"type": "string", "enum": list(model_benchmarks.METRICS)},
           "arena": {"type": "string", "enum": ["models", "builders", "agents"]}, "limit": _LIMIT, "offset": _OFFSET}, ["task"]),
])
for _entry in TOOLS:
    _mutable = _entry["name"] in {"add_model", "remove_model", "set_display_name", "refresh_benchmarks"}
    _entry["annotations"] = {"readOnlyHint": not _mutable, "destructiveHint": _entry["name"] == "remove_model",
                             "idempotentHint": _entry["name"] != "add_model", "openWorldHint": _entry["name"] in {
                                 "search_models", "add_model", "model_pricing", "refresh_benchmarks", "model_benchmarks", "compare_models", "rank_models"}}
TOOL_INDEX = {tool["name"]: tool for tool in TOOLS}


def handle_message(deck, message):
    """One JSON-RPC message in, one reply (or None for notifications) out."""
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str) or isinstance(message.get("id"), (dict, list, bool)):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    method, identifier, params = message.get("method"), message.get("id"), message.get("params", {})
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32602, "message": "Invalid params"}} if identifier is not None else None
    if method == "initialize":
        requested = params.get("protocolVersion")
        result = {"protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                  "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO, "instructions": SERVER_INSTRUCTIONS}
    elif isinstance(method, str) and method.startswith("notifications/"):
        return None
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            value = deck.call(params.get("name"), params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(value, indent=1, ensure_ascii=False)}], "isError": False}
        except DeckError as error:
            result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
        except Exception:
            result = {"content": [{"type": "text", "text": "Model Deck could not complete that request."}], "isError": True}
    else:
        if identifier is None:
            return None
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": "Method not found"}}
    if identifier is None:
        return None
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def serve(stdin, stdout, deck):
    for line in stdin:
        if len(line) > MAX_LINE:
            continue
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = handle_message(deck, message)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def main():
    serve(sys.stdin, sys.stdout, Deck())


if __name__ == "__main__":
    main()
