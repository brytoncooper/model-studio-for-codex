"""Focused hygiene tests for the external-plugin packer input walk.

These tests pin down the rule that ``pack_project_archive`` must ignore
Python bytecode cache residue (``__pycache__`` directories and any
``.pyc`` files anywhere in the tree) while still validating declared
resources and producing deterministic archives for equivalent intended
source trees. They exercise the public :func:`pack_project_archive`
helper directly so the failure surface is the packer itself, not the CLI
wrapper.
"""
from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path

from model_deck.plugins.authoring import pack_project_archive


_MINIMAL_MANIFEST: dict = {
    "manifest_version": 1,
    "id": "org.example.pack-hygiene",
    "version": "1.0.0",
    "plugin_api": {"major": 1, "minimum_minor": 0},
    "entrypoint": {"runtime": "python", "path": "plugin.py"},
    "permissions": [],
    "contributes": {},
}


@contextmanager
def _temp_dir(prefix: str):
    directory = tempfile.TemporaryDirectory(prefix=prefix, dir="/tmp")
    try:
        yield Path(directory.name).resolve()
    finally:
        directory.cleanup()


def _write_minimal_project(root: Path) -> None:
    (root / "manifest.json").write_text(
        json.dumps(_MINIMAL_MANIFEST), encoding="utf-8",
    )
    (root / "plugin.py").write_text("# pack hygiene fixture\n", encoding="utf-8")


def _write_pycache_tree(
    root: Path,
    *,
    include_top_level_pyc: bool = False,
) -> None:
    """Plant cache residue across the project tree."""
    top_level_cache = root / "__pycache__"
    top_level_cache.mkdir()
    (top_level_cache / "module.cpython-311.pyc").write_bytes(b"stale-cache")
    (top_level_cache / "manifest.json").write_bytes(b"never-load-this")

    nested = root / "pkg"
    nested.mkdir()
    nested_cache = nested / "__pycache__"
    nested_cache.mkdir()
    (nested_cache / "inner.cpython-311.pyc").write_bytes(b"deep-stale-cache")

    if include_top_level_pyc:
        (root / "loose.pyc").write_bytes(b"top-level-pyc")


def _read_entry_names(archive_path: Path) -> set[str]:
    with zipfile.ZipFile(archive_path, "r") as archive:
        return {info.filename for info in archive.infolist()}


class PackCacheHygieneTests(unittest.TestCase):
    """``__pycache__`` directories and ``.pyc`` files must be excluded."""

    def test_pack_skips_top_level_pycache_directory_and_pycache_payloads(self):
        with _temp_dir("pack-hygiene-top-") as project_dir, \
                _temp_dir("pack-hygiene-top-out-") as out_dir:
            _write_minimal_project(project_dir)
            _write_pycache_tree(project_dir)

            archive = out_dir / "plugin.zip"
            result = pack_project_archive(project_dir, output_path=archive)

            entry_names = _read_entry_names(archive)
            self.assertEqual(result.entry_count, len(entry_names))
            # No cache directory should appear, even by prefix.
            self.assertFalse(
                any(name.startswith("__pycache__") for name in entry_names),
                f"unexpected cache entry: {sorted(entry_names)}",
            )
            self.assertFalse(
                any(name.endswith(".pyc") for name in entry_names),
                f"unexpected bytecode entry: {sorted(entry_names)}",
            )
            # The intentional payload from inside __pycache__ must not leak.
            self.assertNotIn("__pycache__/manifest.json", entry_names)

    def test_pack_skips_nested_pycache_directory_recursively(self):
        with _temp_dir("pack-hygiene-nested-") as project_dir, \
                _temp_dir("pack-hygiene-nested-out-") as out_dir:
            _write_minimal_project(project_dir)
            _write_pycache_tree(project_dir)

            archive = out_dir / "plugin.zip"
            pack_project_archive(project_dir, output_path=archive)

            entry_names = _read_entry_names(archive)
            self.assertFalse(
                any(
                    name.startswith("pkg/__pycache__")
                    for name in entry_names
                ),
                f"unexpected nested cache entry: {sorted(entry_names)}",
            )

    def test_pack_skips_loose_pyc_file_outside_pycache(self):
        with _temp_dir("pack-hygiene-loose-") as project_dir, \
                _temp_dir("pack-hygiene-loose-out-") as out_dir:
            _write_minimal_project(project_dir)
            _write_pycache_tree(project_dir, include_top_level_pyc=True)

            archive = out_dir / "plugin.zip"
            pack_project_archive(project_dir, output_path=archive)

            entry_names = _read_entry_names(archive)
            self.assertNotIn("loose.pyc", entry_names)
            self.assertTrue(
                all(not name.endswith(".pyc") for name in entry_names),
                f"unexpected bytecode entry: {sorted(entry_names)}",
            )

    def test_pack_archive_matches_clean_project_when_cache_residue_added(self):
        with _temp_dir("pack-hygiene-equiv-") as project_dir, \
                _temp_dir("pack-hygiene-equiv-dirty-") as dirty_dir, \
                _temp_dir("pack-hygiene-equiv-out-") as out_dir:
            _write_minimal_project(project_dir)
            _write_minimal_project(dirty_dir)
            _write_pycache_tree(dirty_dir, include_top_level_pyc=True)

            clean_archive = out_dir / "clean.zip"
            dirty_archive = out_dir / "dirty.zip"

            clean_result = pack_project_archive(
                project_dir, output_path=clean_archive,
            )
            dirty_result = pack_project_archive(
                dirty_dir, output_path=dirty_archive,
            )

            self.assertEqual(clean_result.sha256, dirty_result.sha256)
            self.assertEqual(
                hashlib.sha256(clean_archive.read_bytes()).hexdigest(),
                hashlib.sha256(dirty_archive.read_bytes()).hexdigest(),
            )
            self.assertEqual(clean_result.entry_count, dirty_result.entry_count)
            self.assertEqual(clean_result.total_bytes, dirty_result.total_bytes)
            self.assertEqual(
                _read_entry_names(clean_archive),
                _read_entry_names(dirty_archive),
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
