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
    out_py, out_swift = generated_manifest_paths()
    inv = load_json(INVENTORY)
    text = json.dumps(inv, indent=2, sort_keys=True) + "\n"
    out_py.parent.mkdir(parents=True, exist_ok=True)
    out_swift.parent.mkdir(parents=True, exist_ok=True)
    out_py.write_text(text, encoding="utf-8")
    out_swift.write_text(text, encoding="utf-8")


def render_generated_manifest_bytes(inventory_path: Path) -> bytes:
    """Return the exact bytes `write_generated_manifest` emits for the
    given canonical inventory file. The bundle-parity check compares
    each generated manifest against this rendered byte sequence so the
    compare matches what the writer actually produces (sorted keys,
    indent=2, trailing newline)."""
    inv = load_json(inventory_path)
    return (json.dumps(inv, indent=2, sort_keys=True) + "\n").encode("utf-8")


def bundle_roots() -> tuple[Path, Path, Path, Path, Path]:
    """Canonical -> bundle relative roots used by sync and by check.

    The intentionally generated resource set is exactly: every
    `*.schema.json` under `contracts/`, every file under
    `contracts/fixtures/`, the `contracts/operations.inventory.json`
    file, and the generated Swift/Python manifest files written from
    the inventory. Non-generated files such as `README.md` and the
    `schema-subset.json` policy file remain in `contracts/` only.
    """
    py_root = REPO_ROOT / "python" / "src" / "model_deck_contracts" / "schemas"
    swift_root = REPO_ROOT / "macos" / "Sources" / "ModelDeckContracts" / "Resources"
    return (
        REPO_ROOT / "contracts",
        py_root / "contracts",
        swift_root / "contracts",
        py_root / "fixtures",
        swift_root / "fixtures",
    )


def relative_bundle_path(absolute: Path, bundle_root: Path) -> str:
    rel = absolute.relative_to(bundle_root)
    return rel.as_posix()


def generated_manifest_paths() -> tuple[Path, Path]:
    """Return the absolute paths of the Python and Swift generated
    inventory manifests. Extracted as a helper so tests can monkeypatch
    it for isolated layouts."""
    return (
        REPO_ROOT / "python" / "src" / "model_deck_contracts" / "_generated_inventory.json",
        REPO_ROOT / "macos" / "Sources" / "ModelDeckContracts" / "generated_inventory.json",
    )


def check_bundle_parity() -> list[str]:
    """Byte-compare the intentionally generated resources between the
    canonical tree and each packaged bundle.

    Compares:
      * every `*.schema.json` under `contracts/` against the same path
        under each bundle's `contracts/` tree,
      * every file under `contracts/fixtures/` against the same path
        under each bundle's `fixtures/` tree,
      * `contracts/operations.inventory.json` against each bundle copy,
      * the Python `_generated_inventory.json` and Swift
        `generated_inventory.json` against the rendered canonical
        inventory (sorted, indent=2, trailing newline — the exact
        bytes `write_generated_manifest` emits).

    The intentionally generated resource set is exactly the schema
    files, fixture files, the inventory, and the two generated
    manifests. Non-generated files (README, schema-subset) only live
    under `contracts/` and are NOT required under the bundles.

    Stale extra files under the bundles' schema/fixture trees are
    reported as parity errors so a stale leftover from a previous
    write cannot silently survive a non-mutating check.

    Returns a list of mismatch descriptions; empty means parity.
    """
    errors: list[str] = []
    _, py_contracts, swift_contracts, py_fixtures, swift_fixtures = bundle_roots()

    canonical_schemas = sorted(CONTRACTS.rglob("*.schema.json"))
    canonical_schema_rels = {
        canonical.relative_to(CONTRACTS).as_posix() for canonical in canonical_schemas
    }
    for canonical in canonical_schemas:
        rel = canonical.relative_to(CONTRACTS).as_posix()
        for bundle_contracts_root in (py_contracts, swift_contracts):
            target = bundle_contracts_root / rel
            label = "python" if bundle_contracts_root == py_contracts else "swift"
            if not target.is_file():
                errors.append(
                    f"bundle parity: {label} bundle missing schema {rel}"
                )
                continue
            if target.read_bytes() != canonical.read_bytes():
                errors.append(
                    f"bundle parity: {label} bundle schema content differs for {rel}"
                )

    fixtures_root = CONTRACTS / "fixtures"
    canonical_fixture_rels: set[str] = set()
    if fixtures_root.is_dir():
        canonical_fixtures = sorted(
            p for p in fixtures_root.rglob("*") if p.is_file()
        )
        canonical_fixture_rels = {
            canonical.relative_to(fixtures_root).as_posix()
            for canonical in canonical_fixtures
        }
        for canonical in canonical_fixtures:
            rel = canonical.relative_to(fixtures_root).as_posix()
            for bundle_fixtures_root in (py_fixtures, swift_fixtures):
                target = bundle_fixtures_root / rel
                label = "python" if bundle_fixtures_root == py_fixtures else "swift"
                if not target.is_file():
                    errors.append(
                        f"bundle parity: {label} bundle missing fixture {rel}"
                    )
                    continue
                if target.read_bytes() != canonical.read_bytes():
                    errors.append(
                        f"bundle parity: {label} bundle fixture content differs for {rel}"
                    )

    inv_canonical = CONTRACTS / "operations.inventory.json"
    for bundle_contracts_root, label in (
        (py_contracts, "python"),
        (swift_contracts, "swift"),
    ):
        target = bundle_contracts_root / "operations.inventory.json"
        if not target.is_file():
            errors.append(
                f"bundle parity: {label} bundle missing operations.inventory.json"
            )
            continue
        if target.read_bytes() != inv_canonical.read_bytes():
            errors.append(
                f"bundle parity: {label} bundle operations.inventory.json differs"
            )

    inv_rendered = render_generated_manifest_bytes(inv_canonical)
    py_gen, swift_gen = generated_manifest_paths()
    if not py_gen.is_file():
        errors.append("bundle parity: missing generated python manifest")
    elif py_gen.read_bytes() != inv_rendered:
        errors.append(
            "bundle parity: python _generated_inventory.json differs from rendered canonical inventory"
        )
    if not swift_gen.is_file():
        errors.append("bundle parity: missing generated swift manifest")
    elif swift_gen.read_bytes() != inv_rendered:
        errors.append(
            "bundle parity: swift generated_inventory.json differs from rendered canonical inventory"
        )

    # Detect stale extra files in the bundle schemas/fixtures trees
    # that are not part of the intentionally generated set. Compare
    # relative-file sets so a non-mutating check cannot let a stale
    # leftover from a previous write silently survive.
    for bundle_contracts_root, label in (
        (py_contracts, "python"),
        (swift_contracts, "swift"),
    ):
        if not bundle_contracts_root.is_dir():
            continue
        actual = {
            p.relative_to(bundle_contracts_root).as_posix()
            for p in bundle_contracts_root.rglob("*.schema.json")
        }
        for rel in sorted(actual - canonical_schema_rels):
            errors.append(
                f"bundle parity: {label} bundle has stale schema {rel}"
            )

    for bundle_fixtures_root, label in (
        (py_fixtures, "python"),
        (swift_fixtures, "swift"),
    ):
        if not bundle_fixtures_root.is_dir():
            continue
        actual = {
            p.relative_to(bundle_fixtures_root).as_posix()
            for p in bundle_fixtures_root.rglob("*")
            if p.is_file()
        }
        for rel in sorted(actual - canonical_fixture_rels):
            errors.append(
                f"bundle parity: {label} bundle has stale fixture {rel}"
            )

    return errors


def run_check() -> int:
    """Verify the contracts tree and prove bundle parity without writing."""
    errors: list[str] = []
    errors.extend(verify_inventory())
    errors.extend(verify_all_schema_files())
    errors.extend(check_bundle_parity())
    schema_count = len(list(CONTRACTS.rglob("*.schema.json")))
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        print(
            f"contracts check FAILED: {len(errors)} issue(s) across {schema_count} schemas",
            file=sys.stderr,
        )
        return 1
    print(
        f"OK: contracts check passed; {schema_count} schemas and bundled copies in sync"
    )
    return 0


def run_write() -> int:
    """Intentionally propagate canonical contracts to packaged bundles."""
    errors: list[str] = []
    errors.extend(verify_inventory())
    errors.extend(verify_all_schema_files())
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    sync_schema_bundle()
    write_generated_manifest()
    schema_count = len(list(CONTRACTS.rglob("*.schema.json")))
    print(
        f"OK: propagated {schema_count} schemas, fixtures, and manifests to bundles"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_const",
        dest="mode",
        const="check",
        help="verify and prove bundle parity without writing (default)",
    )
    mode.add_argument(
        "--write",
        action="store_const",
        dest="mode",
        const="write",
        help="propagate canonical contracts into packaged bundles",
    )
    parser.set_defaults(mode="check")
    args = parser.parse_args()
    if args.mode == "write":
        return run_write()
    return run_check()


if __name__ == "__main__":
    raise SystemExit(main())
