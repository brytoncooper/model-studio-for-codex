#!/usr/bin/env python3
"""Validate contracts tree, resolve refs, verify inventory coverage."""
from __future__ import annotations

import argparse
import json
import re
import sys
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = REPO_ROOT / "contracts"
INVENTORY = CONTRACTS / "operations.inventory.json"
SUBSET = CONTRACTS / "schema-subset.json"

REF_RE = re.compile(r"#(/definitions/[^\"]+)?$")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def rel_contract_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path.resolve()).replace("\\", "/")


def allowed_keywords() -> set[str]:
    data = load_json(SUBSET)
    return set(data["allowed_keywords"])


def is_schema_object(node: dict) -> bool:
    markers = {"$ref", "oneOf", "allOf", "not", "type", "properties", "items", "definitions", "const", "enum"}
    return any(k in node for k in markers)


def walk_schema_nodes(node, path: str, errors: list[str], allowed: set[str]) -> None:
    if isinstance(node, list):
        for i, item in enumerate(node):
            walk_schema_nodes(item, f"{path}[{i}]", errors, allowed)
        return
    if not isinstance(node, dict):
        return
    if is_schema_object(node):
        if node.get("additionalProperties") is True:
            errors.append(f"{path}: additionalProperties:true forbidden")
        for key in node:
            if key not in allowed and key not in ("description", "default"):
                errors.append(f"{path}: unsupported keyword {key}")
        if "properties" in node and isinstance(node["properties"], dict):
            for pname, sub in node["properties"].items():
                walk_schema_nodes(sub, f"{path}.properties.{pname}", errors, allowed)
        if "definitions" in node and isinstance(node["definitions"], dict):
            for dname, sub in node["definitions"].items():
                walk_schema_nodes(sub, f"{path}.definitions.{dname}", errors, allowed)
        if "items" in node:
            walk_schema_nodes(node["items"], f"{path}.items", errors, allowed)
        for comb in ("oneOf", "allOf", "not"):
            if comb in node:
                walk_schema_nodes(node[comb], f"{path}.{comb}", errors, allowed)
        ap = node.get("additionalProperties")
        if isinstance(ap, dict):
            walk_schema_nodes(ap, f"{path}.additionalProperties", errors, allowed)
        return
    for key, value in node.items():
        walk_schema_nodes(value, f"{path}.{key}", errors, allowed)




def unescape_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def resolve_json_pointer(root: object, pointer: str) -> object:
    if not pointer.startswith("#"):
        raise ValueError(f"expected JSON pointer fragment, got {pointer!r}")
    if pointer == "#":
        return root
    tokens = pointer[1:].split("/")
    if tokens and tokens[0] == "":
        tokens = tokens[1:]
    node: object = root
    for raw in tokens:
        if raw == "":
            raise KeyError("empty JSON pointer token")
        key = unescape_pointer_token(raw)
        if isinstance(node, dict):
            if key not in node:
                raise KeyError(key)
            node = node[key]
            continue
        if isinstance(node, list):
            node = node[int(key)]
            continue
        raise KeyError(key)
    return node


def target_path_for_ref(ref: str, base_file: Path) -> Path:
    if ref.startswith("#"):
        return base_file.resolve()
    file_part = ref.split("#", 1)[0]
    if file_part.startswith("contracts/"):
        target = (REPO_ROOT / file_part).resolve()
    else:
        target = (base_file.parent / file_part).resolve()
    contracts_root = (REPO_ROOT / "contracts").resolve()
    if not target.is_relative_to(contracts_root):
        raise FileNotFoundError(f"ref outside contracts tree: {ref} from {base_file}")
    if not target.is_file():
        raise FileNotFoundError(f"unresolved ref {ref} from {base_file}")
    return target


def resolve_ref_string(ref: str, base_file: Path, *, root_doc: dict | None = None) -> Path:
    target = target_path_for_ref(ref, base_file)
    if ref.startswith("#"):
        doc = root_doc if root_doc is not None else load_json(target)
    else:
        doc = load_json(target)
    if "#" in ref:
        fragment = ref[ref.index("#") :]
        try:
            resolve_json_pointer(doc, fragment)
        except (KeyError, IndexError, ValueError) as exc:
            rel = rel_contract_path(base_file)
            raise FileNotFoundError(
                f"unresolved fragment {fragment} in {ref} from {rel}"
            ) from exc
    return target


def collect_refs(
    node,
    base_file: Path,
    refs: list[tuple[str, Path]],
    *,
    root_doc: dict | None = None,
    visited: set[Path] | None = None,
) -> None:
    if visited is None:
        visited = set()
    base_resolved = base_file.resolve()
    doc = root_doc if root_doc is not None else load_json(base_resolved)

    def walk(value) -> None:
        if isinstance(value, dict):
            if "$ref" in value:
                ref = value["$ref"]
                target = resolve_ref_string(ref, base_resolved, root_doc=doc)
                refs.append((ref, base_resolved))
                if not ref.startswith("#"):
                    target_resolved = target.resolve()
                    if target_resolved not in visited:
                        visited.add(target_resolved)
                        target_doc = load_json(target_resolved)
                        collect_refs(
                            target_doc,
                            target_resolved,
                            refs,
                            root_doc=target_doc,
                            visited=visited,
                        )
            for item in value.values():
                walk(item)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)

    walk(node)


def method_schema_paths(method: str) -> tuple[str, str]:
    if method.startswith("engine.v1."):
        tail = method[len("engine.v1.") :]
        base = f"contracts/engine.v1/methods/{tail}"
    elif method.startswith("plugin.v1.broker."):
        tail = method[len("plugin.v1.broker.") :]
        base = f"contracts/plugin.v1/broker/{tail}"
    elif method.startswith("plugin.v1.provider."):
        tail = method[len("plugin.v1.provider.") :]
        base = f"contracts/plugin.v1/provider/{tail}"
    elif method.startswith("plugin.v1."):
        tail = method[len("plugin.v1.") :]
        base = f"contracts/plugin.v1/lifecycle/{tail}"
    else:
        raise ValueError(method)
    return f"{base}.params.schema.json", f"{base}.result.schema.json"


def verify_inventory() -> list[str]:
    errors: list[str] = []
    inv = load_json(INVENTORY)
    groups = [
        inv.get("engine_v1", []),
        inv.get("plugin_v1_lifecycle", []),
        inv.get("plugin_v1_broker", []),
        inv.get("plugin_v1_provider", []),
    ]
    for group in groups:
        for entry in group:
            method = entry["method"]
            p, r = method_schema_paths(method)
            for rel in (p, r):
                if not (REPO_ROOT / rel).is_file():
                    errors.append(f"inventory missing schema for {method}: {rel}")
    return errors


def verify_all_schema_files() -> list[str]:
    errors: list[str] = []
    allowed = allowed_keywords()
    schema_files = sorted(CONTRACTS.rglob("*.schema.json"))
    for sf in schema_files:
        data = load_json(sf)
        walk_schema_nodes(data, rel_contract_path(sf), errors, allowed)
        refs: list[tuple[str, Path]] = []
        try:
            collect_refs(data, sf, refs)
            for ref, base in refs:
                resolve_ref_string(ref, base)
        except FileNotFoundError as exc:
            errors.append(str(exc))
    return errors




def sync_schema_bundle() -> None:
    py_root = REPO_ROOT / "python" / "src" / "model_deck_contracts" / "schemas" / "contracts"
    swift_root = (
        REPO_ROOT
        / "macos"
        / "Sources"
        / "ModelDeckContracts"
        / "Resources"
        / "contracts"
    )
    for dest in (py_root, swift_root):
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
    for sf in sorted(CONTRACTS.rglob("*.schema.json")):
        rel = sf.relative_to(CONTRACTS)
        for dest in (py_root, swift_root):
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sf, out)
    fixtures_src = CONTRACTS / "fixtures"
    for dest_root in (
        REPO_ROOT / "python" / "src" / "model_deck_contracts" / "schemas" / "fixtures",
        REPO_ROOT / "macos" / "Sources" / "ModelDeckContracts" / "Resources" / "fixtures",
    ):
        if dest_root.exists():
            shutil.rmtree(dest_root)
        shutil.copytree(fixtures_src, dest_root)
    inv_src = CONTRACTS / "operations.inventory.json"
    for dest_root in (
        REPO_ROOT / "python" / "src" / "model_deck_contracts" / "schemas" / "contracts",
        REPO_ROOT / "macos" / "Sources" / "ModelDeckContracts" / "Resources" / "contracts",
    ):
        shutil.copy2(inv_src, dest_root / "operations.inventory.json")

def write_generated_manifest() -> None:
    out_py = REPO_ROOT / "python" / "src" / "model_deck_contracts" / "_generated_inventory.json"
    out_swift = REPO_ROOT / "macos" / "Sources" / "ModelDeckContracts" / "generated_inventory.json"
    inv = load_json(INVENTORY)
    text = json.dumps(inv, indent=2, sort_keys=True) + "\n"
    out_py.parent.mkdir(parents=True, exist_ok=True)
    out_swift.parent.mkdir(parents=True, exist_ok=True)
    out_py.write_text(text, encoding="utf-8")
    out_swift.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", default=True)
    args = parser.parse_args()
    errors = []
    errors.extend(verify_inventory())
    errors.extend(verify_all_schema_files())
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    sync_schema_bundle()
    write_generated_manifest()
    print(f"OK: validated {len(list(CONTRACTS.rglob('*.schema.json')))} schema files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
