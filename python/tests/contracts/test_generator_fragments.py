import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.generate_contracts as generator


class GeneratorFragmentTests(unittest.TestCase):
    def test_missing_local_fragment_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "probe.schema.json"
            schema_path.write_text(
                json.dumps(
                    {
                        "definitions": {"present": {"type": "string"}},
                        "$ref": "#/definitions/FINALIZER_MISSING",
                    }
                ),
                encoding="utf-8",
            )
            doc = json.loads(schema_path.read_text(encoding="utf-8"))
            with self.assertRaises(FileNotFoundError) as ctx:
                generator.resolve_ref_string(
                    "#/definitions/FINALIZER_MISSING",
                    schema_path,
                    root_doc=doc,
                )
            self.assertIn("FINALIZER_MISSING", str(ctx.exception))

    def test_external_fragment_with_escaped_pointer(self) -> None:
        doc = {
            "definitions": {
                "a~b/c": {"type": "string"},
            }
        }
        node = generator.resolve_json_pointer(doc, "#/definitions/a~0b~1c")
        self.assertEqual(node, {"type": "string"})


class GeneratorCheckAndWriteTests(unittest.TestCase):
    """Three compact tests covering the `--check` / `--write` split.

    Each test patches `scripts.generate_contracts` module attrs to a
    private temp tree, then registers `addCleanup` to restore the
    originals — so a failure mid-test cannot leak patched state into
    sibling tests or downstream code.
    """

    def setUp(self) -> None:
        import scripts.generate_contracts as gen

        self._gen = gen
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        root = Path(self._tmp.name)
        layout = self._build_layout(root)

        for name in (
            "REPO_ROOT",
            "CONTRACTS",
            "INVENTORY",
            "bundle_roots",
            "generated_manifest_paths",
            "sync_schema_bundle",
            "write_generated_manifest",
        ):
            self.addCleanup(setattr, gen, name, getattr(gen, name))

        self._layout = layout
        gen.REPO_ROOT = root
        gen.CONTRACTS = layout["contracts"]
        gen.INVENTORY = layout["inventory"]
        gen.bundle_roots = lambda: (
            layout["contracts"],
            layout["py_contracts"],
            layout["swift_contracts"],
            layout["py_fixtures"],
            layout["swift_fixtures"],
        )
        gen.generated_manifest_paths = lambda: (
            layout["py_gen"],
            layout["swift_gen"],
        )
        gen.sync_schema_bundle = lambda: self._sync_bundles(layout)
        gen.write_generated_manifest = lambda: self._write_manifests(layout)

    def _build_layout(self, root: Path) -> dict:
        contracts = root / "contracts"
        fixtures = contracts / "fixtures"
        py_contracts = root / "python" / "schemas" / "contracts"
        py_fixtures = root / "python" / "schemas" / "fixtures"
        swift_contracts = root / "macos" / "contracts"
        swift_fixtures = root / "macos" / "fixtures"
        inventory = contracts / "operations.inventory.json"
        for p in (
            contracts,
            fixtures,
            fixtures / "valid",
            fixtures / "invalid",
            py_contracts,
            py_fixtures,
            swift_contracts,
            swift_fixtures,
        ):
            p.mkdir(parents=True, exist_ok=True)
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        }
        (contracts / "probe.schema.json").write_text(json.dumps(schema), encoding="utf-8")
        (fixtures / "valid" / "probe_ok.json").write_text(json.dumps({"x": 1}), encoding="utf-8")
        (fixtures / "invalid" / "probe_bad.json").write_text(
            json.dumps({"x": "not-an-integer"}), encoding="utf-8"
        )
        inventory.write_text(
            json.dumps(
                {
                    "valid": [
                        {"file": "valid/probe_ok.json", "schema": "contracts/probe.schema.json"},
                    ],
                    "invalid": [
                        {"file": "invalid/probe_bad.json", "schema": "contracts/probe.schema.json"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return {
            "contracts": contracts,
            "py_contracts": py_contracts,
            "swift_contracts": swift_contracts,
            "py_fixtures": py_fixtures,
            "swift_fixtures": swift_fixtures,
            "py_gen": root / "python" / "_generated_inventory.json",
            "swift_gen": root / "macos" / "generated_inventory.json",
            "inventory": inventory,
        }

    def _sync_bundles(self, layout: dict) -> None:
        for dest in (layout["py_contracts"], layout["swift_contracts"]):
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True, exist_ok=True)
        for sf in sorted(layout["contracts"].rglob("*.schema.json")):
            rel = sf.relative_to(layout["contracts"])
            for dest in (layout["py_contracts"], layout["swift_contracts"]):
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sf, out)
        fixtures_src = layout["contracts"] / "fixtures"
        for dest_root in (layout["py_fixtures"], layout["swift_fixtures"]):
            if dest_root.exists():
                shutil.rmtree(dest_root)
            shutil.copytree(fixtures_src, dest_root)
        for dest_root in (layout["py_contracts"], layout["swift_contracts"]):
            shutil.copy2(layout["inventory"], dest_root / "operations.inventory.json")

    def _write_manifests(self, layout: dict) -> None:
        inv = self._gen.load_json(layout["inventory"])
        text = json.dumps(inv, indent=2, sort_keys=True) + "\n"
        for out in (layout["py_gen"], layout["swift_gen"]):
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")

    def test_write_then_check_passes(self) -> None:
        self.assertEqual(self._gen.run_write(), 0)
        self.assertEqual(self._gen.run_check(), 0)

    def test_check_detects_stale_and_mismatch(self) -> None:
        self.assertEqual(self._gen.run_write(), 0)
        # Stale schema in the Swift bundle only.
        (self._layout["swift_contracts"] / "stale.schema.json").write_text(
            '{"type": "object"}', encoding="utf-8"
        )
        # Differing generated manifest in the Python bundle.
        self._layout["py_gen"].write_text("{}\n", encoding="utf-8")
        self.assertEqual(self._gen.run_check(), 1)

    def test_check_does_not_write(self) -> None:
        self.assertEqual(self._gen.run_write(), 0)
        targets = [
            self._layout["py_contracts"] / "probe.schema.json",
            self._layout["swift_contracts"] / "probe.schema.json",
            self._layout["py_contracts"] / "operations.inventory.json",
            self._layout["swift_contracts"] / "operations.inventory.json",
            self._layout["py_fixtures"] / "valid" / "probe_ok.json",
            self._layout["swift_fixtures"] / "valid" / "probe_ok.json",
            self._layout["py_gen"],
            self._layout["swift_gen"],
        ]
        before = {p: p.read_bytes() for p in targets}
        self.assertEqual(self._gen.run_check(), 0)
        self.assertEqual(before, {p: p.read_bytes() for p in targets})
