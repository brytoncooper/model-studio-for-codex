"""Direct-inference provider presets, billing descriptions, and safe model discovery."""
import ipaddress
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

PRESETS_PATH = Path(__file__).with_name("provider_presets.json")
MAX_PRESET_BYTES = 64 * 1024
MAX_CATALOG_BYTES = 8 * 1024 * 1024
MAX_CATALOG_MODELS = 4096
CATALOG_TIMEOUT_SECONDS = 15
CATALOG_UNAVAILABLE_STATUSES = (404, 405, 501)


class ProviderConnectionError(ValueError):
    """A display-safe failure that never contains credentials or an upstream body."""


def _plain_text(value, maximum, required=True):
    if (not isinstance(value, str) or len(value) > maximum
            or (required and not value.strip()) or any(ord(character) < 32 for character in value)):
        raise ProviderConnectionError("The provider returned invalid model metadata.")
    return value


def _model_id(value):
    value = _plain_text(value, 256)
    if any(character.isspace() for character in value):
        raise ProviderConnectionError("The provider returned an invalid model identifier.")
    return value


def _local_host(host):
    if host == "localhost" or host.endswith((".local", ".localhost", ".lan", ".home", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or address.is_private
    except ValueError:
        return False


def _canonical_base_url(base_url):
    if (not isinstance(base_url, str) or not 0 < len(base_url) <= 512
            or any(character.isspace() or ord(character) < 32 for character in base_url)):
        raise ProviderConnectionError("Enter a valid provider endpoint URL.")
    try:
        parts = urllib.parse.urlsplit(base_url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ProviderConnectionError("Enter a valid provider endpoint URL.") from None
    if (parts.scheme not in ("http", "https") or not host or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment or "\\" in base_url
            or (parts.scheme == "http" and not _local_host(host))):
        raise ProviderConnectionError("Use an HTTPS endpoint without credentials, a query, or a fragment. Local servers may use HTTP.")
    authority = "[" + host.lower() + "]" if ":" in host else host.lower()
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        authority += ":" + str(port)
    return urllib.parse.urlunsplit((parts.scheme, authority, parts.path.rstrip("/"), "", ""))


def load_provider_presets():
    """Load the bundled connection choices; no remote data or credentials are involved."""
    try:
        if PRESETS_PATH.is_symlink():
            raise ProviderConnectionError("The bundled provider choices could not be loaded.")
        with PRESETS_PATH.open("rb") as stream:
            raw = stream.read(MAX_PRESET_BYTES + 1)
        if len(raw) > MAX_PRESET_BYTES:
            raise ProviderConnectionError("The bundled provider choices exceed the size limit.")
        document = json.loads(raw)
        if (not isinstance(document, dict) or type(document.get("version")) is not int or document["version"] != 1
                or not isinstance(document.get("providers"), list) or not 1 <= len(document["providers"]) <= 32):
            raise ProviderConnectionError("The bundled provider choices have an unsupported format.")
        providers, identifiers, urls = [], set(), set()
        for entry in document["providers"]:
            if not isinstance(entry, dict):
                raise ProviderConnectionError("The bundled provider choices have an invalid entry.")
            identifier = _plain_text(entry.get("id"), 32)
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", identifier) or identifier in identifiers:
                raise ProviderConnectionError("The bundled provider identifiers must be unique.")
            base_url = _canonical_base_url(entry.get("base_url"))
            if not base_url.startswith("https://") or base_url in urls:
                raise ProviderConnectionError("The bundled provider endpoints must be unique HTTPS URLs.")
            wire = entry.get("wire")
            if wire not in ("chat", "responses"):
                raise ProviderConnectionError("The bundled provider has an unsupported inference format.")
            key_url = _canonical_base_url(entry.get("key_url"))
            if not key_url.startswith("https://"):
                raise ProviderConnectionError("The provider's key setup page must use HTTPS.")
            color = _plain_text(entry.get("color"), 6)
            if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
                raise ProviderConnectionError("The bundled provider color is invalid.")
            models = entry.get("default_models")
            if not isinstance(models, list) or not 1 <= len(models) <= 32:
                raise ProviderConnectionError("The bundled provider model suggestions are invalid.")
            models = [_model_id(model) for model in models]
            if len(set(models)) != len(models):
                raise ProviderConnectionError("The bundled model suggestions must be unique.")
            providers.append({"id": identifier, "name": _plain_text(entry.get("name"), 64),
                              "base_url": base_url, "wire": wire,
                              "billing": _plain_text(entry.get("billing"), 128),
                              "billing_note": _plain_text(entry.get("billing_note"), 1024),
                              "symbol": _plain_text(entry.get("symbol"), 64), "color": color,
                              "key_url": key_url, "default_models": models})
            identifiers.add(identifier)
            urls.add(base_url)
        return providers
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ProviderConnectionError("The bundled provider choices could not be loaded.") from None


def provider_for_base_url(base_url):
    """Match the full canonical endpoint, never a substring or related hostname."""
    try:
        canonical = _canonical_base_url(base_url)
    except ProviderConnectionError:
        return None
    return next((entry for entry in load_provider_presets() if entry["base_url"] == canonical), None)


def provider_billing_description(base_url, has_key=True):
    provider = provider_for_base_url(base_url)
    if not has_key:
        if provider:
            return "No provider key is saved. Connect your account to use this provider."
        try:
            local = _local_host(urllib.parse.urlsplit(_canonical_base_url(base_url)).hostname)
        except ProviderConnectionError:
            local = False
        return "No API key; local server billing is managed separately." if local else "No API key is configured. This endpoint controls access and billing."
    return provider["billing_note"] if provider else "Uses the saved key for this endpoint. That provider controls billing."


class _RejectCatalogRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Even same-origin redirects are unnecessary for these fixed /models routes.
        return None


def fetch_catalog_json(url, headers=None, timeout=CATALOG_TIMEOUT_SECONDS):
    """Bounded JSON GET with redirect following disabled for authenticated discovery."""
    _canonical_base_url(url)
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ModelDeck/1.9", **(headers or {})})
    opener = urllib.request.build_opener(_RejectCatalogRedirects())
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(MAX_CATALOG_BYTES + 1)
    if len(raw) > MAX_CATALOG_BYTES:
        raise ProviderConnectionError("The endpoint's model list exceeds the size limit.")
    try:
        return json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise ProviderConnectionError("The endpoint returned an invalid JSON model list.") from None


def _declared_capabilities(entry, model):
    for field in ("tools", "reasoning"):
        if field in entry:
            if type(entry[field]) is not bool:
                raise ProviderConnectionError("The provider returned invalid model capabilities.")
            model[field] = entry[field]
    for source, target in (("context_length", "context"), ("inputTokenLimit", "context"),
                           ("outputTokenLimit", "output_limit")):
        if source in entry:
            value = entry[source]
            if type(value) is not int or not 0 < value <= 1_000_000_000:
                raise ProviderConnectionError("The provider returned invalid model limits.")
            model[target] = value
    architecture = entry.get("architecture")
    modalities = entry.get("modalities")
    if modalities is None and isinstance(architecture, dict):
        modalities = architecture.get("input_modalities")
    if modalities is not None:
        if not isinstance(modalities, list) or len(modalities) > 16:
            raise ProviderConnectionError("The provider returned invalid model modalities.")
        model["modalities"] = [_plain_text(value, 64) for value in modalities]
    parameters = entry.get("supported_parameters")
    if parameters is not None:
        if not isinstance(parameters, list) or len(parameters) > 128:
            raise ProviderConnectionError("The provider returned invalid supported parameters.")
        parameters = [_plain_text(value, 128) for value in parameters]
        model.setdefault("tools", "tools" in parameters)
        model.setdefault("reasoning", "reasoning" in parameters or "reasoning_effort" in parameters)


def _parse_endpoint_models(document):
    if not isinstance(document, dict):
        raise ProviderConnectionError("The endpoint returned an invalid model list.")
    google_format = "data" not in document and "models" in document
    rows = document.get("models" if google_format else "data")
    if not isinstance(rows, list) or len(rows) > MAX_CATALOG_MODELS:
        raise ProviderConnectionError("The endpoint returned an invalid or oversized model list.")
    if document.get("nextPageToken") or document.get("has_more"):
        raise ProviderConnectionError("The endpoint returned a paginated model list that cannot be displayed completely.")
    models, identifiers = [], set()
    for entry in rows:
        if not isinstance(entry, dict):
            raise ProviderConnectionError("The endpoint returned an invalid model entry.")
        identifier = _model_id(entry.get("name" if google_format else "id"))
        if google_format and identifier.startswith("models/"):
            identifier = _model_id(identifier[len("models/"):])
        if identifier in identifiers:
            raise ProviderConnectionError("The endpoint returned duplicate model identifiers.")
        identifiers.add(identifier)
        if google_format and "supportedGenerationMethods" in entry:
            methods = entry["supportedGenerationMethods"]
            if not isinstance(methods, list) or len(methods) > 128:
                raise ProviderConnectionError("The provider returned invalid model generation methods.")
            methods = [_plain_text(method, 128) for method in methods]
            if "generateContent" not in methods:
                continue
        name = entry.get("displayName" if google_format else "name")
        model = {"id": identifier, "name": _plain_text(name, 256) if name is not None else identifier}
        if "description" in entry:
            description = entry["description"]
            if not isinstance(description, str) or len(description) > 65536:
                raise ProviderConnectionError("The provider returned an invalid model description.")
            model["description"] = " ".join(description.split())[:4096]
        _declared_capabilities(entry, model)
        models.append(model)
    return sorted(models, key=lambda model: (model["name"].casefold(), model["id"]))


def fetch_endpoint_models(base_url, key=None, fetch_json=None):
    """Discover at this endpoint only. Suggestions never imply authenticated model access."""
    base_url = _canonical_base_url(base_url)
    provider = provider_for_base_url(base_url)
    if key is not None and (not isinstance(key, str) or not key or len(key) > 16384
                            or any(character.isspace() or ord(character) < 32 for character in key)):
        raise ProviderConnectionError("The saved provider key is invalid. Replace it in Connections.")
    headers = {"Authorization": "Bearer " + key} if key is not None else {}
    try:
        document = (fetch_json or fetch_catalog_json)(base_url + "/models", headers)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        if status in CATALOG_UNAVAILABLE_STATUSES and provider:
            return {"models": [{"id": model, "name": model} for model in provider["default_models"]],
                    "source": "suggested", "verified": False,
                    "note": "This endpoint does not expose a model catalog. These are suggested model IDs; account access and native agent compatibility are unverified."}
        if status in (401, 403):
            raise ProviderConnectionError("This endpoint rejected the saved key or account access. Check the connection credentials.") from None
        if status == 429:
            raise ProviderConnectionError("This endpoint is rate limited. Try loading its models again later.") from None
        if 300 <= status < 400:
            raise ProviderConnectionError("This endpoint redirected model discovery. No credentials were forwarded; check the exact base URL.") from None
        raise ProviderConnectionError("This endpoint could not list models (HTTP " + str(status) + ").") from None
    except ProviderConnectionError:
        raise
    except Exception:
        raise ProviderConnectionError("The endpoint's model list could not be reached. Check the connection and try again.") from None
    models = _parse_endpoint_models(document)
    return {"models": models, "source": "remote", "verified": True,
            "note": "Listed by this endpoint. Inference and native agent compatibility have not been verified."}
