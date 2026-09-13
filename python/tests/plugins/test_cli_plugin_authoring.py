"""Acceptance tests for the B23 author-side archive commands.

These tests exercise the ``model-deck plugin validate`` and
``model-deck plugin pack`` subcommands through the CLI entry point. They
build synthetic project trees and packed archives on disk in temporary
directories, feed them through ``cli_main.main([...])``, and inspect the
captured stdout/stderr. They never touch the live engine, never spawn
processes beyond the CLI subprocess, and never make a network call.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from model_deck.cli import main as cli_main
from model_deck.plugins.archive_inspection import DEFAULT_ARCHIVE_BYTES
from model_deck.plugins.authoring import (
    AuthoringError,
    PackLimits,
    pack_project_archive,
    validate_project_archive,
)
from model_deck.plugins.authoring import packing as authoring_packing


_MINIMAL_MANIFEST: dict[str, object] = {
    "manifest_version": 1,
    "id": "org.example.cli-authoring",
    "version": "1.0.0",
    "plugin_api": {"major": 1, "minimum_minor": 0},
    "entrypoint": {"runtime": "python", "path": "plugin.py"},
    "permissions": [],
    "contributes": {},
}


def _invoke_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(sys, "stdout", stdout), \
            mock.patch.object(sys, "stderr", stderr):
        exit_code = cli_main.main(argv)
    return exit_code, stdout.getvalue(), stderr.getvalue()


@contextmanager
def _temp_dir(prefix: str) -> object:
    directory = tempfile.TemporaryDirectory(prefix=prefix)
    try:
        yield Path(directory.name).resolve()
    finally:
        directory.cleanup()


def _write_minimal_project(root: Path) -> None:
    (root / "manifest.json").write_text(
        json.dumps(_MINIMAL_MANIFEST), encoding="utf-8",
    )
    (root / "plugin.py").write_text("# cli authoring fixture\n", encoding="utf-8")


class PluginValidateCliTests(unittest.TestCase):
    def test_validate_minimal_archive_returns_structured_summary(self) -> None:
        with _temp_dir("cli-plugin-validate-") as project_dir, \
                _temp_dir("cli-plugin-out-") as out_dir:
            _write_minimal_project(project_dir)
            archive = out_dir / "plugin.zip"
            pack_exit, pack_stdout, pack_stderr = _invoke_cli([
                "plugin", "pack", str(project_dir), "--output", str(archive),
            ])
            self.assertEqual(pack_exit, 0, msg=pack_stderr)

            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "validate", str(archive),
            ])
            self.assertEqual(exit_code, 0, msg=stderr)
            summary = json.loads(stdout)
            self.assertTrue(summary["archive"]["ok"])
            self.assertEqual(summary["archive"]["total_entries"], 2)
            self.assertTrue(summary["manifest"]["ok"])
            self.assertEqual(summary["manifest"]["identity"]["id"],
                             "org.example.cli-authoring")
            self.assertTrue(summary["manifest"]["entrypoint"]["present"])
            self.assertEqual(summary["manifest"]["entrypoint"]["path"],
                             "plugin.py")
            self.assertEqual(summary["manifest"]["api"]["major"], 1)
            self.assertEqual(summary["manifest"]["api"]["minimum_minor"], 0)
            self.assertEqual(stderr, "")

    def test_validate_rejects_hostile_archive_with_path_traversal(self) -> None:
        with _temp_dir("cli-plugin-validate-traversal-") as out_dir:
            archive_path = out_dir / "evil.zip"
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("../manifest.json",
                            json.dumps(_MINIMAL_MANIFEST))
            archive_path.write_bytes(buf.getvalue())

            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "validate", str(archive_path),
            ])
            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("invalid_path", stderr.lower())

    def test_validate_rejects_archive_missing_manifest(self) -> None:
        with _temp_dir("cli-plugin-validate-missing-manifest-") as out_dir:
            archive_path = out_dir / "no-manifest.zip"
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("README.md", b"hi")
            archive_path.write_bytes(buf.getvalue())

            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "validate", str(archive_path),
            ])
            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("missing_manifest", stderr)

    def test_validate_rejects_malformed_manifest_json(self) -> None:
        with _temp_dir("cli-plugin-validate-malformed-") as out_dir:
            archive_path = out_dir / "malformed.zip"
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", b"{not-json}")
            archive_path.write_bytes(buf.getvalue())

            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "validate", str(archive_path),
            ])
            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("manifest_read_failed", stderr)

    def test_validate_rejects_manifest_with_missing_entrypoint_file(
        self,
    ) -> None:
        with _temp_dir("cli-plugin-validate-no-entrypoint-") as out_dir:
            archive_path = out_dir / "no-entrypoint.zip"
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", json.dumps(_MINIMAL_MANIFEST))
            archive_path.write_bytes(buf.getvalue())

            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "validate", str(archive_path),
            ])
            self.assertEqual(exit_code, 1)
            # The archive passes its own inspection and the manifest passes
            # inspection, but the entrypoint file is missing from the
            # archive entries; the structured summary on stdout must
            # surface that explicitly.
            summary = json.loads(stdout)
            self.assertFalse(summary["manifest"]["entrypoint"]["present"])
            self.assertEqual(
                summary["manifest"]["entrypoint"]["path"], "plugin.py",
            )
            self.assertEqual(stderr, "")

    def test_validate_requires_absolute_archive_path(self) -> None:
        exit_code, stdout, stderr = _invoke_cli([
            "plugin", "validate", "relative/archive.zip",
        ])
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("absolute", stderr)

    def test_public_report_is_not_ok_when_entrypoint_is_missing(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(_MINIMAL_MANIFEST))

        report = validate_project_archive(buffer.getvalue())

        self.assertTrue(report.archive.ok)
        self.assertTrue(report.manifest.ok)
        self.assertFalse(report.ok)

    def test_validate_caps_archive_read_before_inspection(self) -> None:
        with _temp_dir("cli-plugin-validate-large-") as out_dir:
            archive_path = out_dir / "oversized.zip"
            with archive_path.open("wb") as archive_file:
                archive_file.truncate(DEFAULT_ARCHIVE_BYTES + 1)

            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "validate", str(archive_path),
            ])

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("input_too_large", stderr)

    def test_importing_cli_does_not_import_engine_modules(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                (
                    "import sys; import model_deck.cli.main; "
                    "blocked = [name for name in sys.modules "
                    "if name == 'model_deck.bootstrap' "
                    "or name == 'model_deck.engine' "
                    "or name.startswith('model_deck.engine.')]; "
                    "raise SystemExit(1 if blocked else 0)"
                ),
            ],
            cwd=Path(__file__).parents[2],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())

    def test_public_validator_normalizes_schema_invalid_manifest(self) -> None:
        invalid_manifest = {
            key: value
            for key, value in _MINIMAL_MANIFEST.items()
            if key != "id"
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(invalid_manifest))
            archive.writestr("plugin.py", b"# fixture\n")

        with self.assertRaises(AuthoringError) as captured:
            validate_project_archive(buffer.getvalue())

        self.assertEqual(captured.exception.code, "manifest_read_failed")
        self.assertEqual(captured.exception.field, "manifest.json")
        self.assertIn("schema_invalid", captured.exception.detail)

    def test_public_validator_normalizes_incompatible_plugin_api(self) -> None:
        incompatible_manifest = {
            **_MINIMAL_MANIFEST,
            "plugin_api": {"major": 2, "minimum_minor": 0},
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(incompatible_manifest))
            archive.writestr("plugin.py", b"# fixture\n")

        with self.assertRaises(AuthoringError) as captured:
            validate_project_archive(buffer.getvalue())

        self.assertEqual(captured.exception.code, "manifest_read_failed")
        self.assertEqual(captured.exception.field, "manifest.json")
        self.assertIn("incompatible_api_version", captured.exception.detail)


class PluginPackCliTests(unittest.TestCase):
    def test_pack_normalizes_schema_invalid_manifest_without_output(self) -> None:
        with _temp_dir("cli-plugin-pack-schema-invalid-") as project_dir, \
                _temp_dir("cli-plugin-pack-schema-invalid-out-") as out_dir:
            invalid_manifest = {
                key: value
                for key, value in _MINIMAL_MANIFEST.items()
                if key != "id"
            }
            (project_dir / "manifest.json").write_text(
                json.dumps(invalid_manifest), encoding="utf-8",
            )
            (project_dir / "plugin.py").write_text(
                "# fixture\n", encoding="utf-8",
            )
            archive = out_dir / "plugin.zip"

            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "pack", str(project_dir), "--output", str(archive),
            ])

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("manifest_read_failed", stderr)
            self.assertIn("schema_invalid", stderr)
            self.assertFalse(archive.exists())

    def test_pack_normalizes_incompatible_plugin_api_without_output(self) -> None:
        with _temp_dir("cli-plugin-pack-api-invalid-") as project_dir, \
                _temp_dir("cli-plugin-pack-api-invalid-out-") as out_dir:
            incompatible_manifest = {
                **_MINIMAL_MANIFEST,
                "plugin_api": {"major": 2, "minimum_minor": 0},
            }
            (project_dir / "manifest.json").write_text(
                json.dumps(incompatible_manifest), encoding="utf-8",
            )
            (project_dir / "plugin.py").write_text(
                "# fixture\n", encoding="utf-8",
            )
            archive = out_dir / "plugin.zip"

            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "pack", str(project_dir), "--output", str(archive),
            ])

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("manifest_read_failed", stderr)
            self.assertIn("incompatible_api_version", stderr)
            self.assertFalse(archive.exists())

    def test_pack_round_trip_is_deterministic_sha256_stable(self) -> None:
        with _temp_dir("cli-plugin-pack-deterministic-") as project_dir, \
                _temp_dir("cli-plugin-pack-out-") as out_dir:
            _write_minimal_project(project_dir)
            archive1 = out_dir / "first.zip"
            archive2 = out_dir / "second.zip"
            exit_a, stdout_a, stderr_a = _invoke_cli([
                "plugin", "pack", str(project_dir), "--output", str(archive1),
            ])
            self.assertEqual(exit_a, 0, msg=stderr_a)
            summary_a = json.loads(stdout_a)
            exit_b, stdout_b, stderr_b = _invoke_cli([
                "plugin", "pack", str(project_dir), "--output", str(archive2),
            ])
            self.assertEqual(exit_b, 0, msg=stderr_b)
            summary_b = json.loads(stdout_b)
            self.assertEqual(summary_a["sha256"], summary_b["sha256"])
            self.assertEqual(
                hashlib.sha256(archive1.read_bytes()).hexdigest(),
                hashlib.sha256(archive2.read_bytes()).hexdigest(),
            )
            self.assertEqual(summary_a["entry_count"],
                             summary_b["entry_count"])
            self.assertEqual(summary_a["total_bytes"],
                             summary_b["total_bytes"])

    def test_pack_refuses_to_overwrite_existing_output(self) -> None:
        with _temp_dir("cli-plugin-pack-overwrite-") as project_dir, \
                _temp_dir("cli-plugin-pack-overwrite-out-") as out_dir:
            _write_minimal_project(project_dir)
            archive = out_dir / "plugin.zip"
            archive.write_bytes(b"placeholder-bytes")
            original_bytes = archive.read_bytes()
            original_size = len(original_bytes)

            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "pack", str(project_dir), "--output", str(archive),
            ])
            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("output_exists", stderr)
            self.assertEqual(archive.read_bytes(), original_bytes)
            self.assertEqual(len(archive.read_bytes()), original_size)

    def test_pack_refuses_output_nested_under_project_root(self) -> None:
        with _temp_dir("cli-plugin-pack-nested-") as project_dir:
            _write_minimal_project(project_dir)
            nested = project_dir / "subdir" / "out.zip"
            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "pack", str(project_dir), "--output", str(nested),
            ])
            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("output_nested_in_input", stderr)

    def test_pack_refuses_project_with_missing_manifest(self) -> None:
        with _temp_dir("cli-plugin-pack-no-manifest-") as project_dir, \
                _temp_dir("cli-plugin-pack-no-manifest-out-") as out_dir:
            (project_dir / "plugin.py").write_text("# no manifest\n",
                                                   encoding="utf-8")
            archive = out_dir / "plugin.zip"
            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "pack", str(project_dir), "--output", str(archive),
            ])
            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("missing_manifest", stderr)
            self.assertFalse(archive.exists())

    def test_pack_refuses_project_with_missing_entrypoint_file(self) -> None:
        with _temp_dir("cli-plugin-pack-missing-entrypoint-") as project_dir, \
                _temp_dir("cli-plugin-pack-missing-entrypoint-out-") as out_dir:
            (project_dir / "manifest.json").write_text(
                json.dumps(_MINIMAL_MANIFEST), encoding="utf-8",
            )
            archive = out_dir / "plugin.zip"
            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "pack", str(project_dir), "--output", str(archive),
            ])
            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("entrypoint_file_missing", stderr)
            self.assertFalse(archive.exists())

    def test_pack_refuses_project_with_symlink(self) -> None:
        with _temp_dir("cli-plugin-pack-symlink-") as project_dir, \
                _temp_dir("cli-plugin-pack-symlink-out-") as out_dir:
            _write_minimal_project(project_dir)
            target = project_dir / "plugin.py"
            link = project_dir / "plugin_link.py"
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unsupported: {exc}")
            archive = out_dir / "plugin.zip"
            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "pack", str(project_dir), "--output", str(archive),
            ])
            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("input_symlink_or_special", stderr)
            self.assertFalse(archive.exists())

    def test_pack_leaves_no_output_after_failure(self) -> None:
        with _temp_dir("cli-plugin-pack-no-leftover-") as project_dir, \
                _temp_dir("cli-plugin-pack-no-leftover-out-") as out_dir:
            (project_dir / "manifest.json").write_text(
                json.dumps(_MINIMAL_MANIFEST), encoding="utf-8",
            )
            archive = out_dir / "plugin.zip"
            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "pack", str(project_dir), "--output", str(archive),
            ])
            self.assertEqual(exit_code, 1)
            self.assertFalse(archive.exists())
            leftovers = [
                path.name for path in out_dir.iterdir()
                if path.name.endswith(".zip.tmp")
            ]
            self.assertEqual(leftovers, [])

    def test_pack_does_not_overwrite_competitor_winning_publish_race(self) -> None:
        with _temp_dir("cli-plugin-pack-race-") as project_dir, \
                _temp_dir("cli-plugin-pack-race-out-") as out_dir:
            _write_minimal_project(project_dir)
            archive = out_dir / "plugin.zip"
            real_link = authoring_packing.os.link

            def competing_publish(temporary, destination):
                Path(destination).write_bytes(b"competitor")
                return real_link(temporary, destination)

            with mock.patch.object(
                authoring_packing.os, "link", side_effect=competing_publish
            ):
                exit_code, stdout, stderr = _invoke_cli([
                    "plugin", "pack", str(project_dir), "--output", str(archive),
                ])

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("output_exists", stderr)
            self.assertEqual(archive.read_bytes(), b"competitor")
            self.assertEqual(list(out_dir.glob(".plugin-pack-*.zip.tmp")), [])

    def test_pack_revalidates_compression_ratio_before_publication(self) -> None:
        with _temp_dir("cli-plugin-pack-ratio-") as project_dir, \
                _temp_dir("cli-plugin-pack-ratio-out-") as out_dir:
            _write_minimal_project(project_dir)
            (project_dir / "plugin.py").write_bytes(b"0" * (2 * 1024 * 1024))
            archive = out_dir / "plugin.zip"

            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "pack", str(project_dir), "--output", str(archive),
            ])

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("input_out_of_budget", stderr)
            self.assertFalse(archive.exists())

    def test_pack_revalidates_backslash_entry_name_before_publication(self) -> None:
        with _temp_dir("cli-plugin-pack-backslash-") as project_dir, \
                _temp_dir("cli-plugin-pack-backslash-out-") as out_dir:
            _write_minimal_project(project_dir)
            (project_dir / "bad\\name.py").write_text("# invalid archive path\n")
            archive = out_dir / "plugin.zip"

            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "pack", str(project_dir), "--output", str(archive),
            ])

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("input_symlink_or_special", stderr)
            self.assertFalse(archive.exists())

    def test_pack_stops_incremental_traversal_at_directory_budget(self) -> None:
        with _temp_dir("cli-plugin-pack-walk-") as project_dir, \
                _temp_dir("cli-plugin-pack-walk-out-") as out_dir:
            _write_minimal_project(project_dir)
            for name in ("empty-a", "empty-b", "empty-c"):
                (project_dir / name).mkdir()
            archive = out_dir / "plugin.zip"

            with self.assertRaises(AuthoringError) as captured:
                pack_project_archive(
                    project_dir,
                    output_path=archive,
                    limits=PackLimits(file_count=2),
                )

            self.assertEqual(captured.exception.code, "input_out_of_budget")
            self.assertIn("directory budget", captured.exception.detail)
            self.assertFalse(archive.exists())

    def test_pack_caps_actual_read_when_file_grows_after_stat(self) -> None:
        with _temp_dir("cli-plugin-pack-grow-") as project_dir, \
                _temp_dir("cli-plugin-pack-grow-out-") as out_dir:
            _write_minimal_project(project_dir)
            plugin_path = project_dir / "plugin.py"
            original_total = sum(
                path.stat().st_size for path in project_dir.iterdir()
            )
            archive = out_dir / "plugin.zip"
            real_read = authoring_packing.os.read
            grew_file = False

            def grow_before_read(descriptor, byte_count):
                nonlocal grew_file
                is_plugin_file = (
                    os.fstat(descriptor).st_ino == plugin_path.stat().st_ino
                )
                if not grew_file and is_plugin_file:
                    with plugin_path.open("ab") as plugin_file:
                        plugin_file.write(b"x" * 1024)
                    grew_file = True
                return real_read(descriptor, byte_count)

            with mock.patch.object(
                authoring_packing.os, "read", side_effect=grow_before_read
            ):
                with self.assertRaises(AuthoringError) as captured:
                    pack_project_archive(
                        project_dir,
                        output_path=archive,
                        limits=PackLimits(total_bytes=original_total + 100),
                    )

            self.assertEqual(captured.exception.code, "input_out_of_budget")
            self.assertIn("byte budget", captured.exception.detail)
            self.assertTrue(grew_file)
            self.assertFalse(archive.exists())

    def test_pack_does_not_follow_file_swapped_to_symlink(self) -> None:
        with _temp_dir("cli-plugin-pack-swap-") as project_dir, \
                _temp_dir("cli-plugin-pack-swap-out-") as out_dir:
            _write_minimal_project(project_dir)
            plugin_path = project_dir / "plugin.py"
            outside_path = out_dir / "outside.py"
            outside_path.write_text("# outside\n")
            archive = out_dir / "plugin.zip"
            real_open = authoring_packing.os.open
            swapped_file = False

            def swap_before_open(path, flags, *args, **kwargs):
                nonlocal swapped_file
                if not swapped_file and path == "plugin.py":
                    plugin_path.unlink()
                    os.symlink(outside_path, plugin_path)
                    swapped_file = True
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                authoring_packing.os, "open", side_effect=swap_before_open
            ):
                exit_code, stdout, stderr = _invoke_cli([
                    "plugin", "pack", str(project_dir), "--output", str(archive),
                ])

            self.assertTrue(swapped_file)
            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("input_not_readable", stderr)
            self.assertFalse(archive.exists())

    def test_pack_normalizes_directory_and_temp_creation_errors(self) -> None:
        with _temp_dir("cli-plugin-pack-fs-") as project_dir, \
                _temp_dir("cli-plugin-pack-fs-out-") as out_dir:
            _write_minimal_project(project_dir)
            cases = (
                (
                    mock.patch.object(Path, "mkdir", side_effect=OSError("private")),
                    out_dir / "missing" / "plugin.zip",
                ),
                (
                    mock.patch.object(
                        authoring_packing.tempfile,
                        "mkstemp",
                        side_effect=OSError("private"),
                    ),
                    out_dir / "plugin.zip",
                ),
            )
            for injected_failure, archive in cases:
                with self.subTest(archive=archive), injected_failure:
                    exit_code, stdout, stderr = _invoke_cli([
                        "plugin", "pack", str(project_dir),
                        "--output", str(archive),
                    ])
                    self.assertEqual(exit_code, 1)
                    self.assertEqual(stdout, "")
                    self.assertIn("output_unwritable", stderr)
                    self.assertNotIn("private", stderr)
                    self.assertFalse(archive.exists())

    def test_pack_requires_absolute_paths(self) -> None:
        with _temp_dir("cli-plugin-pack-relative-") as project_dir:
            _write_minimal_project(project_dir)
            exit_code, stdout, stderr = _invoke_cli([
                "plugin", "pack", "relative/project",
                "--output", str(project_dir / "out.zip"),
            ])
            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("absolute", stderr)
