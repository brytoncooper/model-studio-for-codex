"""Focused tests for the G4 (`migration`) module manifest in scripts/verify.py.

G4 does not discover its modules by filename keyword: it runs an explicit
manifest grouped by the evidence items VERIFICATION.md section 1 names for the
gate. These tests assert that the manifest covers every evidence item, that it
leaves out the host-projection rendering tests (which prove rendering, not
migration or recovery, and already run in G2), and that every module it names
still exists on disk.
"""
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verify import (  # noqa: E402
    MIGRATION_EVIDENCE_MODULES,
    migration_test_modules,
    missing_migration_modules,
    module_source_path,
)

PACKAGE_ROOT = REPO_ROOT / "python"

# The evidence items VERIFICATION.md section 1 lists for G4, each paired with a
# module that must be selected for it. The anchor is the module whose tests most
# directly prove the item, so a rename cannot quietly empty an item out.
EVIDENCE_ANCHORS = {
    "V2 authority/outbox recovery": (
        "tests.engine.test_plugin_authority",
        "tests.engine.test_sqlite_projection_outbox",
    ),
    "CAS conflicts": (
        "tests.contracts.test_model_revision_cas",
        "tests.engine.test_sqlite_plugin_data",
    ),
    "schema-version bookkeeping": (
        "tests.engine.test_plugin_data_versioning_contract",
        "tests.engine.test_sqlite_versioned_plugin_data",
    ),
    "transactional upgrades": (
        "tests.engine.test_extension_lifecycle_versioned_data",
        "tests.engine.test_sqlite_plugin_jobs",
    ),
    "newer-schema refusal": (
        "tests.engine.test_sqlite_projection_receipts",
        "tests.engine.test_sqlite_session_run_repository",
    ),
    "V2 data recovery": (
        "tests.engine.test_run_startup_recovery",
        "tests.integrations.hosts.codex.test_migration_preview",
    ),
}

# Host-projection rendering tests. They match the words "projection" and
# "recovery" loosely but prove how a projected file is rendered, not migration,
# CAS or schema bookkeeping, so G4 must not claim them.
RENDERING_MODULES = (
    "tests.engine.test_projection_policy",
    "tests.engine.test_conditional_projection_files",
    "tests.engine.test_model_projection_invalidation",
    "tests.engine.test_projection_dependency_expansions",
)


class MigrationEvidenceCoverageTests(unittest.TestCase):
    def test_manifest_covers_exactly_the_documented_evidence_items(self) -> None:
        listed = tuple(evidence for evidence, _modules in MIGRATION_EVIDENCE_MODULES)
        self.assertEqual(sorted(listed), sorted(EVIDENCE_ANCHORS))
        self.assertEqual(len(listed), len(set(listed)))

    def test_every_evidence_item_selects_at_least_one_module(self) -> None:
        selected = migration_test_modules()
        for evidence, modules in MIGRATION_EVIDENCE_MODULES:
            with self.subTest(evidence=evidence):
                self.assertTrue(modules, f"{evidence} selects no module")
                for module in modules:
                    self.assertIn(module, selected)

    def test_every_evidence_item_keeps_its_anchor_module(self) -> None:
        grouped = dict(MIGRATION_EVIDENCE_MODULES)
        for evidence, anchors in EVIDENCE_ANCHORS.items():
            with self.subTest(evidence=evidence):
                for anchor in anchors:
                    self.assertIn(anchor, grouped[evidence])


class MigrationModuleSelectionTests(unittest.TestCase):
    def test_rendering_modules_are_excluded(self) -> None:
        selected = migration_test_modules()
        for module in RENDERING_MODULES:
            with self.subTest(module=module):
                self.assertNotIn(module, selected)

    def test_selection_is_deduplicated_and_keeps_manifest_order(self) -> None:
        selected = migration_test_modules()
        self.assertEqual(len(selected), len(set(selected)))
        expected: list[str] = []
        for _evidence, modules in MIGRATION_EVIDENCE_MODULES:
            for module in modules:
                if module not in expected:
                    expected.append(module)
        self.assertEqual(selected, tuple(expected))

    def test_a_module_proving_two_evidence_items_is_selected_once(self) -> None:
        # test_sqlite_projection_receipts proves outbox recovery, schema-version
        # bookkeeping and newer-schema refusal; it must still run once.
        selected = migration_test_modules()
        receipts = "tests.engine.test_sqlite_projection_receipts"
        self.assertEqual(selected.count(receipts), 1)

    def test_every_selected_module_exists_in_the_tree(self) -> None:
        self.assertEqual(missing_migration_modules(PACKAGE_ROOT), ())

    def test_missing_modules_are_reported_rather_than_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            empty_root = Path(raw)
            self.assertEqual(
                missing_migration_modules(empty_root), migration_test_modules()
            )


class ModuleSourcePathTests(unittest.TestCase):
    def test_dotted_name_maps_to_the_test_file(self) -> None:
        self.assertEqual(
            module_source_path(PACKAGE_ROOT, "tests.engine.test_plugin_authority"),
            PACKAGE_ROOT / "tests" / "engine" / "test_plugin_authority.py",
        )


if __name__ == "__main__":
    unittest.main()
