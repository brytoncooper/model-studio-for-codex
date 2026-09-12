from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Layer(str, Enum):
    CONTRACTS = "contracts"
    KERNEL = "kernel"
    ENGINE = "engine"
    ADAPTERS = "adapters"
    INTEGRATIONS = "integrations"
    PROVIDERS = "providers"
    HOSTS = "hosts"
    CLIENTS = "clients"
    BOOTSTRAP = "bootstrap"
    PLUGIN = "plugin"
    LEGACY_BASELINE = "legacy_baseline"
    UNKNOWN = "unknown"


CONTRACTS_PREFIXES = ("model_deck_contracts.", "model_deck_contracts")
KERNEL_PREFIX = "model_deck.kernel"
ENGINE_PREFIX = "model_deck.engine"
ADAPTERS_PREFIX = "model_deck.adapters"
INTEGRATIONS_PREFIX = "model_deck.integrations"
PROVIDERS_PREFIX = "model_deck.integrations.providers"
HOSTS_PREFIX = "model_deck.integrations.hosts"
CLIENTS_PREFIX = "model_deck.integrations.clients"
BOOTSTRAP_PREFIX = "model_deck.bootstrap"
PLUGIN_PREFIXES = ("model_deck_plugin.", "model_deck_sdk.")


@dataclass(frozen=True)
class ModuleBinding:
    logical_name: str
    layer: Layer


ALLOWED_LAYER_IMPORTS: dict[Layer, frozenset[Layer]] = {
    Layer.CONTRACTS: frozenset({Layer.CONTRACTS}),
    Layer.KERNEL: frozenset({Layer.CONTRACTS, Layer.KERNEL}),
    Layer.ENGINE: frozenset({Layer.CONTRACTS, Layer.KERNEL, Layer.ENGINE}),
    Layer.ADAPTERS: frozenset({Layer.CONTRACTS, Layer.ENGINE, Layer.ADAPTERS}),
    Layer.INTEGRATIONS: frozenset({Layer.CONTRACTS, Layer.ENGINE, Layer.INTEGRATIONS}),
    Layer.PROVIDERS: frozenset({Layer.CONTRACTS, Layer.ENGINE, Layer.PROVIDERS}),
    Layer.HOSTS: frozenset({Layer.CONTRACTS, Layer.ENGINE, Layer.HOSTS, Layer.INTEGRATIONS}),
    Layer.CLIENTS: frozenset({Layer.CONTRACTS, Layer.ENGINE, Layer.CLIENTS, Layer.INTEGRATIONS}),
    Layer.BOOTSTRAP: frozenset(
        {
            Layer.CONTRACTS,
            Layer.KERNEL,
            Layer.ENGINE,
            Layer.ADAPTERS,
            Layer.INTEGRATIONS,
            Layer.PROVIDERS,
            Layer.HOSTS,
            Layer.CLIENTS,
            Layer.BOOTSTRAP,
        }
    ),
    Layer.PLUGIN: frozenset({Layer.CONTRACTS, Layer.PLUGIN}),
    Layer.LEGACY_BASELINE: frozenset({Layer.LEGACY_BASELINE, Layer.CONTRACTS}),
    Layer.UNKNOWN: frozenset({Layer.UNKNOWN}),
}


PROVIDER_ROUTER_FORBIDDEN = frozenset(
    {
        "local_router",
        "routing_registry",
        "provider_bridge",
    }
)


def layer_for_module(module_name: str) -> Layer:
    name = module_name.strip()
    if not name:
        return Layer.UNKNOWN
    for prefix in PLUGIN_PREFIXES:
        if name == prefix.rstrip(".") or name.startswith(prefix):
            return Layer.PLUGIN
    if name == "model_deck_contracts" or name.startswith(CONTRACTS_PREFIXES[0]):
        return Layer.CONTRACTS
    if name == KERNEL_PREFIX or name.startswith(KERNEL_PREFIX + "."):
        return Layer.KERNEL
    if name == ENGINE_PREFIX or name.startswith(ENGINE_PREFIX + "."):
        return Layer.ENGINE
    if name == ADAPTERS_PREFIX or name.startswith(ADAPTERS_PREFIX + "."):
        return Layer.ADAPTERS
    if name == PROVIDERS_PREFIX or name.startswith(PROVIDERS_PREFIX + "."):
        return Layer.PROVIDERS
    if name == HOSTS_PREFIX or name.startswith(HOSTS_PREFIX + "."):
        return Layer.HOSTS
    if name == CLIENTS_PREFIX or name.startswith(CLIENTS_PREFIX + "."):
        return Layer.CLIENTS
    if name == INTEGRATIONS_PREFIX or name.startswith(INTEGRATIONS_PREFIX + "."):
        return Layer.INTEGRATIONS
    if name == BOOTSTRAP_PREFIX or name.startswith(BOOTSTRAP_PREFIX + "."):
        return Layer.BOOTSTRAP
    return Layer.UNKNOWN


def engine_feature_root(module_name: str) -> str | None:
    prefix = ENGINE_PREFIX + "."
    if not module_name.startswith(prefix):
        return None
    rest = module_name[len(prefix) :]
    if not rest:
        return ""
    return rest.split(".", 1)[0]


def is_private_module(module_name: str) -> bool:
    parts = module_name.split(".")
    return any(part.startswith("_") and part != "_" for part in parts)




def is_intended_product_tree_module(module_name: str) -> bool:
    name = module_name.strip()
    if not name:
        return False
    if name == "model_deck" or name.startswith("model_deck."):
        return True
    for prefix in PLUGIN_PREFIXES:
        if name == prefix.rstrip(".") or name.startswith(prefix):
            return True
    return False


def is_model_deck_namespace_module(module_name: str) -> bool:
    name = module_name.strip()
    if not name:
        return False
    return name == "model_deck" or name.startswith("model_deck.")

def is_relative_import_level(level: int) -> bool:
    return level > 0
