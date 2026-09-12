from __future__ import annotations

from pathlib import Path


def package_root() -> Path:
    return Path(__file__).resolve().parent


def repo_root() -> Path:
    return package_root().parents[3]


def schemas_root() -> Path:
    return package_root() / "schemas"


def contracts_root() -> Path:
    return schemas_root() / "contracts"


def fixtures_root() -> Path:
    return schemas_root() / "fixtures"


def schema_path_for_method(method: str, role: str) -> Path:
    if role not in ("params", "result"):
        raise ValueError(role)
    if method.startswith("engine.v1."):
        tail = method[len("engine.v1.") :]
        rel = f"engine.v1/methods/{tail}.{role}.schema.json"
    elif method.startswith("plugin.v1.broker."):
        tail = method[len("plugin.v1.broker.") :]
        rel = f"plugin.v1/broker/{tail}.{role}.schema.json"
    elif method.startswith("plugin.v1.provider."):
        tail = method[len("plugin.v1.provider.") :]
        rel = f"plugin.v1/provider/{tail}.{role}.schema.json"
    elif method.startswith("plugin.v1."):
        tail = method[len("plugin.v1.") :]
        rel = f"plugin.v1/lifecycle/{tail}.{role}.schema.json"
    else:
        raise ValueError(method)
    return contracts_root() / rel
