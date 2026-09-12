
from __future__ import annotations

import json
from typing import Iterator

from model_deck_contracts.paths import contracts_root


def load_inventory() -> dict:
    path = contracts_root() / "operations.inventory.json"
    if not path.is_file():
        raise FileNotFoundError(
            "bundled operations.inventory.json missing; run scripts/generate_contracts.py"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def iter_inventory_methods() -> Iterator[str]:
    inv = load_inventory()
    for key in ("engine_v1", "plugin_v1_lifecycle", "plugin_v1_broker", "plugin_v1_provider"):
        for entry in inv.get(key, []):
            yield entry["method"]
