from __future__ import annotations

import os
import stat
import tempfile
import threading
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from model_deck_contracts.validator import validate_schema_ref
from model_deck.engine.host_settings.ports import (
    SettingsConflictError, SettingsDeniedError, SettingsInternalError,
    SettingsInvalidError, SettingsNotFoundError, SettingsDocumentPort,
)
from model_deck.integrations.hosts.codex.settings_document import (
    DocumentContext, FieldSpec, SectionSpec, sha256_hex,
)
from model_deck.integrations.hosts.codex.settings_file import CodexSettingsFile, SettingsFileSpecs
from model_deck.integrations.hosts.codex.settings_file import file as implementation


BASE = b'# retain comment\nmodel = "old"\nunknown = "keep" # retain tail\n'
CHANGED = BASE.replace(b'"old"', b'"new"')


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / "config"
        self.directory.mkdir()
        self.path = self.directory / "config.toml"
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.path.write_bytes(BASE)
        self.path.chmod(0o640)
        self.revision = "context-1"
        self.protected = frozenset()
        self.writable = True
        self.provider_calls = 0
        self.adapter = CodexSettingsFile(config_file=self.path, backup_dir=self.backups,
                                        context_specs=self.specs)

    def tearDown(self):
        self.temp.cleanup()

    def specs(self, source, exists):
        self.provider_calls += 1
        context = DocumentContext(
            host_id="codex.cli", document_id="config.toml",
            document_revision=sha256_hex(source) if exists else "absent", exists=exists,
            target={"display_name": "config", "display_path": "fixture/config.toml",
                    "scope": "user", "writable": self.writable},
            schema_profile={"schema_id": "codex", "schema_revision": "r1",
                            "host_version": "v1", "support_level": "supported"},
            precedence=[], context_revision=self.revision, protected_paths=self.protected,
        )
        return SettingsFileSpecs(context, (SectionSpec("general", "General", ("model",)),),
                                 (FieldSpec("model", "Model", "string", ("model",)),))

    def save(self, candidate=CHANGED, expected=None, revision="context-1"):
        return self.adapter.save("codex.cli", "config.toml", expected or sha256_hex(BASE),
                                 revision, sha256_hex(candidate), candidate.decode())

    def assert_safe(self, error):
        self.assertNotIn("SECRET_SENTINEL", "".join(traceback.format_exception(error)))
        self.assertNotIn(str(self.root), str(error))

    def test_context_callback_typed_errors_preserve_category_without_secret(self):
        from model_deck.engine.host_settings.ports import (
            SettingsExhaustedError, SettingsUnsupportedError, SettingsVersionMismatchError,
        )
        errors = [error_type("SECRET_SENTINEL") for error_type in (
            SettingsDeniedError, SettingsExhaustedError, SettingsInvalidError,
            SettingsNotFoundError, SettingsUnsupportedError, SettingsVersionMismatchError)]
        errors.extend([SettingsConflictError("SECRET_SENTINEL", reason="SECRET_SENTINEL"),
                       SettingsConflictError("SECRET_SENTINEL", reason="context_changed")])
        for original in errors:
            def failing_context(source, exists):
                raise original from ValueError("SECRET_SENTINEL")
            adapter = CodexSettingsFile(config_file=self.path, backup_dir=self.backups,
                                        context_specs=failing_context)
            with self.subTest(category=type(original)), self.assertRaises(type(original)) as caught:
                adapter.read("codex.cli")
            self.assert_safe(caught.exception)
            if isinstance(original, SettingsConflictError):
                expected = "context_changed" if original.reason == "context_changed" else "conflict"
                self.assertEqual(caught.exception.reason, expected)
        self.assertEqual(self.path.read_bytes(), BASE)
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_lossless_edit_backup_and_frozen_results(self):
        self.assertIsInstance(self.adapter, SettingsDocumentPort)
        snapshot = self.adapter.read("codex.cli")
        validate_schema_ref("contracts/engine.v1/methods/hosts.settings.read.result.schema.json", {"snapshot": snapshot})
        draft = {"kind": "structured", "changes": [{"field_id": "model", "operation": "set", "value": "new"}]}
        candidate = self.adapter.validate("codex.cli", "config.toml", sha256_hex(BASE), self.revision, draft)
        validate_schema_ref("contracts/engine.v1/methods/hosts.settings.validate.result.schema.json", candidate)
        self.assertEqual(candidate["candidate_raw_toml"].encode(), CHANGED)
        preview = self.adapter.preview("codex.cli", "config.toml", sha256_hex(BASE), self.revision, draft)
        preview["preview"]["preview_id"] = "engine-test-token"
        validate_schema_ref("contracts/engine.v1/methods/hosts.settings.preview.result.schema.json", preview)
        result = self.save()
        validate_schema_ref("contracts/engine.v1/methods/hosts.settings.save.result.schema.json", result)
        backup = Path(result["backup"]["display_path"])
        self.assertEqual(backup.read_bytes(), BASE)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(self.path.read_bytes(), CHANGED)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

    def test_noop_preserves_inode_mode_bytes_without_backup(self):
        before = self.path.stat()
        result = self.save(BASE)
        self.assertFalse(result["changed"])
        self.assertIsNone(result["backup"])
        self.assertEqual(self.path.read_bytes(), BASE)
        self.assertEqual((self.path.stat().st_ino, self.path.stat().st_mode), (before.st_ino, before.st_mode))
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_missing_and_empty_are_distinct_and_new_file_is_private(self):
        self.path.unlink()
        missing = self.adapter.read("codex.cli")
        self.assertEqual(missing["document_revision"], "absent")
        self.save(expected="absent")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.backups.iterdir()), [])
        self.path.write_bytes(b"")
        with self.assertRaises(SettingsConflictError):
            self.save(expected="absent")

    def test_stale_context_identity_hash_and_protection_reject(self):
        for expected, revision in [(sha256_hex(b"other"), self.revision), (sha256_hex(BASE), "old-context")]:
            with self.assertRaises(SettingsConflictError):
                self.save(expected=expected, revision=revision)
        with self.assertRaises(SettingsNotFoundError):
            self.adapter.read("foreign.host")
        with self.assertRaises(SettingsConflictError):
            self.adapter.save("codex.cli", "config.toml", sha256_hex(BASE), self.revision, sha256_hex(BASE), CHANGED.decode())
        self.protected = frozenset({"model"})
        with self.assertRaises(SettingsDeniedError):
            self.save()
        self.assertEqual(self.path.read_bytes(), BASE)
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_known_constraint_and_nonwritable_reject(self):
        with self.assertRaises(SettingsInvalidError):
            self.save(b"model = 4\n")
        self.writable = False
        with self.assertRaises(SettingsDeniedError):
            self.save()
        self.assertEqual(self.path.read_bytes(), BASE)

    def test_symlink_target_parent_backup_and_lock_reject(self):
        outside = self.root / "outside.toml"
        outside.write_bytes(b"SECRET_SENTINEL")
        self.path.unlink()
        self.path.symlink_to(outside)
        with self.assertRaises(SettingsInternalError) as caught:
            self.save()
        self.assert_safe(caught.exception)
        self.assertEqual(outside.read_bytes(), b"SECRET_SENTINEL")
        self.path.unlink()
        self.path.write_bytes(BASE)
        alias = self.root / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        adapter = CodexSettingsFile(config_file=alias / "config.toml", backup_dir=self.backups, context_specs=self.specs)
        with self.assertRaises(SettingsInternalError):
            adapter.read("codex.cli")
        backup_alias = self.root / "backup-alias"
        backup_alias.symlink_to(self.backups, target_is_directory=True)
        adapter = CodexSettingsFile(config_file=self.path, backup_dir=backup_alias, context_specs=self.specs)
        with self.assertRaises(SettingsInternalError):
            adapter.save("codex.cli", "config.toml", sha256_hex(BASE), self.revision, sha256_hex(CHANGED), CHANGED.decode())
        lock = self.directory / ".config.toml.model-deck.lock"
        lock.unlink()
        lock.symlink_to(outside)
        with self.assertRaises(SettingsInternalError):
            self.adapter.read("codex.cli")

    def test_backup_is_durable_before_atomic_replace(self):
        original_sync, original_replace = os.fsync, os.replace
        synced = set()
        def sync(fd):
            synced.add((os.fstat(fd).st_dev, os.fstat(fd).st_ino))
            return original_sync(fd)
        def replace_file(*args, **kwargs):
            backups = list(self.backups.iterdir())
            self.assertEqual(len(backups), 1)
            for path in (backups[0], self.backups):
                info = path.stat()
                self.assertIn((info.st_dev, info.st_ino), synced)
            self.assertEqual(backups[0].read_bytes(), BASE)
            return original_replace(*args, **kwargs)
        with patch.object(implementation.os, "fsync", side_effect=sync), patch.object(implementation.os, "replace", side_effect=replace_file):
            self.save()

    def test_backup_failure_preserves_original_and_cleans_partial(self):
        with patch.object(implementation.os, "fsync", side_effect=OSError("SECRET_SENTINEL")):
            with self.assertRaises(SettingsInternalError) as caught:
                self.save()
        self.assert_safe(caught.exception)
        self.assertEqual(self.path.read_bytes(), BASE)
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertEqual(list(self.directory.glob(".model-deck-settings-*")), [])

    def test_replace_failure_retains_recoverable_backup_and_cleans_temp(self):
        with patch.object(implementation.os, "replace", side_effect=OSError("SECRET_SENTINEL")):
            with self.assertRaises(SettingsInternalError) as caught:
                self.save()
        self.assert_safe(caught.exception)
        self.assertEqual(self.path.read_bytes(), BASE)
        self.assertEqual([p.read_bytes() for p in self.backups.iterdir()], [BASE])
        self.assertEqual(list(self.directory.glob(".model-deck-settings-*")), [])

    def test_missing_create_never_clobbers_racing_file(self):
        self.path.unlink()
        original_link = os.link
        def race(*args, **kwargs):
            self.path.write_bytes(b"external winner")
            return original_link(*args, **kwargs)
        with patch.object(implementation.os, "link", side_effect=race):
            with self.assertRaises(SettingsConflictError):
                self.save(expected="absent")
        self.assertEqual(self.path.read_bytes(), b"external winner")

    def test_source_and_context_rechecked_after_temp_write(self):
        original_write = self.adapter._write
        for change_context in (False, True):
            self.path.write_bytes(BASE)
            self.revision = "context-1"
            count = 0
            def write(fd, data):
                nonlocal count
                original_write(fd, data)
                count += 1
                if count == 2:
                    if change_context:
                        self.revision = "context-2"
                    else:
                        self.path.write_bytes(b"external = true\n")
            with patch.object(self.adapter, "_write", side_effect=write):
                with self.assertRaises(SettingsConflictError):
                    self.save()
            self.assertEqual(self.path.read_bytes(), BASE if change_context else b"external = true\n")

    def test_ancestor_swap_cannot_redirect_write_and_reports_uncertainty(self):
        outside = self.root / "outside"
        outside.mkdir()
        outsider = outside / "config.toml"
        outsider.write_bytes(b"external winner")
        moved = self.root / "moved"
        original_replace = os.replace
        def race(*args, **kwargs):
            self.directory.rename(moved)
            self.directory.symlink_to(outside, target_is_directory=True)
            return original_replace(*args, **kwargs)
        with patch.object(implementation.os, "replace", side_effect=race):
            with self.assertRaises(SettingsConflictError):
                self.save()
        self.assertEqual(outsider.read_bytes(), b"external winner")
        self.assertEqual((moved / "config.toml").read_bytes(), CHANGED)
        self.assertEqual([p.read_bytes() for p in self.backups.iterdir()], [BASE])

    def test_protection_rechecked_immediately_before_publication(self):
        original_write = self.adapter._write
        count = 0
        def write(fd, data):
            nonlocal count
            original_write(fd, data)
            count += 1
            if count == 2:
                self.protected = frozenset({"model"})
        with patch.object(self.adapter, "_write", side_effect=write):
            with self.assertRaises(SettingsDeniedError):
                self.save()
        self.assertEqual(self.path.read_bytes(), BASE)

    def test_post_publication_failure_never_restores_old_contents(self):
        original_sync = os.fsync
        target_identity = (self.directory.stat().st_dev, self.directory.stat().st_ino)
        def sync(fd):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == target_identity:
                raise OSError("SECRET_SENTINEL")
            return original_sync(fd)
        with patch.object(implementation.os, "fsync", side_effect=sync):
            with self.assertRaises(SettingsInternalError) as caught:
                self.save()
        self.assert_safe(caught.exception)
        self.assertEqual(self.path.read_bytes(), CHANGED)
        self.assertEqual([p.read_bytes() for p in self.backups.iterdir()], [BASE])

    def test_cleanup_does_not_delete_replaced_temporary_file(self):
        original_write = self.adapter._write
        count = 0
        def write(fd, data):
            nonlocal count
            original_write(fd, data)
            count += 1
            if count == 2:
                temporary = next(self.directory.glob(".model-deck-settings-*"))
                temporary.unlink()
                temporary.write_bytes(b"external winner")
        with patch.object(self.adapter, "_write", side_effect=write):
            with self.assertRaises(SettingsConflictError):
                self.save()
        self.assertEqual(self.path.read_bytes(), BASE)
        self.assertEqual([p.read_bytes() for p in self.directory.glob(".model-deck-settings-*")], [b"external winner"])

    def test_cooperating_writers_admit_only_one_base_revision(self):
        barrier = threading.Barrier(2)
        outcomes = []
        def worker(candidate):
            try:
                barrier.wait(timeout=5)
                outcomes.append(self.save(candidate))
            except Exception as error:
                outcomes.append(error)
        other = BASE.replace(b'"old"', b'"second"')
        threads = [threading.Thread(target=worker, args=(candidate,), daemon=True) for candidate in (CHANGED, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "cooperating writer did not finish")
        self.assertEqual(sum(isinstance(value, dict) for value in outcomes), 1)
        self.assertEqual(sum(isinstance(value, SettingsConflictError) for value in outcomes), 1)
        self.assertIn(self.path.read_bytes(), (CHANGED, other))
        self.assertEqual([p.read_bytes() for p in self.backups.iterdir()], [BASE])


if __name__ == "__main__":
    unittest.main()
