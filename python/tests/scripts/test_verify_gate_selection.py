"""Focused tests for the G4 (`migration`) filename filter in scripts/verify.py.

The migration gate selects modules by filename keyword rather than by
directory, so the selection rule is asserted directly: it must be
case-insensitive, must ignore the package prefix, and must only reach the
`tests/engine` and `tests/integrations` trees.
"""
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verify import (  # noqa: E402
    MIGRATION_FILENAME_KEYWORDS,
    is_migration_module,
    migration_test_modules,
)


class MigrationFilenameFilterTests(unittest.TestCase):
    def test_every_keyword_selects_a_module(self) -> None:
        for keyword in MIGRATION_FILENAME_KEYWORDS:
            with self.subTest(keyword=keyword):
                self.assertTrue(is_migration_module(f"tests.engine.test_{keyword}_case"))

    def test_keyword_match_is_case_insensitive(self) -> None:
        self.assertTrue(is_migration_module("tests.engine.test_Outbox_Recovery"))

    def test_unrelated_filename_is_not_selected(self) -> None:
        self.assertFalse(is_migration_module("tests.engine.test_run_use_cases"))

    def test_package_prefix_alone_never_selects(self) -> None:
        # The keyword must appear in the filename, not in a parent package.
        self.assertFalse(is_migration_module("tests.projection.test_run_use_cases"))


class MigrationModuleSelectionTests(unittest.TestCase):
    def test_selection_is_limited_to_engine_and_integration_trees(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package_root = Path(raw)
            for relative in (
                "tests/engine/test_sqlite_projection_outbox.py",
                "tests/engine/test_run_use_cases.py",
                "tests/integrations/hosts/codex/test_migration_preview.py",
                "tests/plugins/test_schema_bundle.py",
            ):
                path = package_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")

            self.assertEqual(
                migration_test_modules(package_root),
                (
                    "tests.engine.test_sqlite_projection_outbox",
                    "tests.integrations.hosts.codex.test_migration_preview",
                ),
            )


if __name__ == "__main__":
    unittest.main()
