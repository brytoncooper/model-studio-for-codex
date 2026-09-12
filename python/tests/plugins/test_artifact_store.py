"""Acceptance tests for the B20 artifact staging slice."""
from __future__ import annotations

import hashlib
import io
import os
import stat
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from model_deck.plugins.archive_inspection import ArchiveInspectionError
from model_deck.plugins.artifact_store import (
    ArtifactCorruptedError,
    ArtifactStoreError,
    stage_archive,
)


def _build(files: dict[str, bytes], dirs: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for d in dirs:
            zf.writestr(d if d.endswith("/") else d + "/", b"")
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class ArtifactStoreTest(unittest.TestCase):
    def test_byte_parity_and_id(self) -> None:
        files = {"a.txt": b"hello", "sub/b.bin": bytes(range(256))}
        blob = _build(files)
        with tempfile.TemporaryDirectory() as td:
            res = stage_archive(blob, store_root=Path(td).resolve())
            self.assertEqual(res.artifact_id, hashlib.sha256(blob).hexdigest())
            self.assertEqual(res.artifact_path, str(Path(os.path.realpath(td)) / res.artifact_id))
            for name, data in files.items():
                with open(Path(res.artifact_path) / name, "rb") as f:
                    self.assertEqual(f.read(), data)
            st = os.lstat(Path(res.artifact_path) / "a.txt")
            self.assertTrue(stat.S_ISREG(st.st_mode))
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)

    def test_malicious_traversal_no_output(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("../../evil.txt", b"x")
        blob = buf.getvalue()
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises((ArchiveInspectionError, ArtifactStoreError)):
                stage_archive(blob, store_root=Path(td).resolve())
            self.assertEqual(os.listdir(td), [])

    def test_symlink_entry_no_output(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            info = zipfile.ZipInfo("link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, b"target")
        blob = buf.getvalue()
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises((ArchiveInspectionError, ArtifactStoreError)):
                stage_archive(blob, store_root=Path(td).resolve())
            self.assertEqual(os.listdir(td), [])

    def test_partial_write_failure_no_artifact(self) -> None:
        files = {f"f{i}.txt": b"y" * 100 for i in range(5)}
        blob = _build(files)
        with tempfile.TemporaryDirectory() as td:
            real_write = os.write
            calls = 0
            def flaky(fd, data):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError("injected write failure")
                return real_write(fd, data)
            with mock.patch("os.write", side_effect=flaky):
                with self.assertRaises(ArtifactStoreError):
                    stage_archive(blob, store_root=Path(td).resolve())
            self.assertEqual(os.listdir(td), [])

    def test_concurrent_identical_stage(self) -> None:
        blob = _build({"a.txt": b"data" * 100})
        with tempfile.TemporaryDirectory() as td:
            results: list = []
            errors: list = []
            def work() -> None:
                try:
                    results.append(stage_archive(blob, store_root=Path(td).resolve()))
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)
            threads = [threading.Thread(target=work) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 8)
            ids = {r.artifact_id for r in results}
            self.assertEqual(len(ids), 1)
            with open(Path(td) / results[0].artifact_id / "a.txt", "rb") as f:
                self.assertEqual(f.read(), b"data" * 100)

    def test_corruption_detected(self) -> None:
        blob = _build({"a.txt": b"good"})
        with tempfile.TemporaryDirectory() as td:
            res = stage_archive(blob, store_root=Path(td).resolve())
            with open(Path(res.artifact_path) / "a.txt", "wb") as f:
                f.write(b"evil!")
            with self.assertRaises(ArtifactCorruptedError):
                stage_archive(blob, store_root=Path(td).resolve())

    def test_symlink_store_root_refused(self) -> None:
        blob = _build({"a.txt": b"x"})
        with tempfile.TemporaryDirectory() as td:
            link = Path(td) / "linkroot"
            real = Path(td) / "real"
            real.mkdir()
            os.symlink(real, link)
            with self.assertRaises(ArtifactStoreError):
                stage_archive(blob, store_root=str(link))
            self.assertEqual(os.listdir(real), [])


class ArtifactStoreSecurityRegressionTests(unittest.TestCase):
    def test_implicit_tree_bounds_are_checked_before_writes(self):
        from model_deck.plugins.artifact_store import store
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            with self.assertRaises(ArtifactStoreError) as caught:
                stage_archive(_build({"/".join(["a"] * 65): b"ok"}), store_root=root)
            self.assertEqual(caught.exception.code, "tree_limit")
            with mock.patch.object(store, "_MAX_TREE_NODES", 3):
                with self.assertRaises(ArtifactStoreError) as caught:
                    stage_archive(_build({"a/b": b"ok", "a/c": b"ok"}), store_root=root)
            self.assertEqual(caught.exception.code, "tree_limit")
            self.assertEqual(list(root.iterdir()), [])

    def test_symlink_ancestor_is_not_resolved_away(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            (base / "real").mkdir()
            (base / "real/store").mkdir()
            (base / "link").symlink_to(base / "real", target_is_directory=True)
            with self.assertRaises(ArtifactStoreError):
                stage_archive(_build({"a": b"ok"}), store_root=base / "link/store")
            self.assertEqual(list((base / "real/store").iterdir()), [])

    def test_root_and_ancestor_swap_cannot_redirect_writes(self):
        for replace_ancestor in (False, True):
            with self.subTest(ancestor=replace_ancestor), tempfile.TemporaryDirectory() as td:
                base = Path(td).resolve()
                parent = base / "parent"
                parent.mkdir()
                root = parent / "store"
                root.mkdir()
                outside = base / "outside"
                outside.mkdir()
                (outside / "store").mkdir()
                original = os.mkdir
                swapped = False
                replaced = parent if replace_ancestor else root

                def swap(name, *args, **kwargs):
                    nonlocal swapped
                    if str(name).startswith(".staging-") and not swapped:
                        swapped = True
                        replaced.rename(base / "old")
                        replaced.symlink_to(outside, target_is_directory=True)
                    return original(name, *args, **kwargs)

                with mock.patch("os.mkdir", side_effect=swap):
                    with self.assertRaises(ArtifactStoreError):
                        stage_archive(_build({"a": b"ok"}), store_root=root)
                self.assertEqual(list(outside.iterdir()), [outside / "store"])
                self.assertEqual(list((outside / "store").iterdir()), [])
                original_root = base / "old/store" if replace_ancestor else base / "old"
                self.assertEqual(list(original_root.iterdir()), [])

    def test_atomic_publish_preserves_competing_empty_directory(self):
        from model_deck.plugins.artifact_store.publication import publish_directory_exclusive
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            blob = _build({"a": b"ok"})
            target = root / hashlib.sha256(blob).hexdigest()
            inode = None

            def compete(fd, source, destination):
                nonlocal inode
                os.mkdir(destination, 0o700, dir_fd=fd)
                inode = os.stat(destination, dir_fd=fd).st_ino
                publish_directory_exclusive(fd, source, destination)

            with self.assertRaises(ArtifactCorruptedError):
                stage_archive(blob, store_root=root, publisher=compete)
            self.assertEqual(target.stat().st_ino, inode)
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(list(root.iterdir()), [target])

    def test_staging_fsync_failure_removes_only_own_staging(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            preserved = stage_archive(_build({"prior": b"safe"}), store_root=root)
            original = os.fsync

            def fail_directory(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("injected directory fsync failure")
                return original(fd)

            with mock.patch("os.fsync", side_effect=fail_directory):
                with self.assertRaises(ArtifactStoreError):
                    stage_archive(_build({"sub/a": b"new"}), store_root=root)
            self.assertEqual(list(root.iterdir()), [Path(preserved.artifact_path)])
            self.assertEqual((Path(preserved.artifact_path) / "prior").read_bytes(), b"safe")

    def test_directory_open_failure_leaves_unidentified_directory(self):
        for nested in (False, True):
            with self.subTest(nested=nested), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                original = os.open
                def fail(name, *args, **kwargs):
                    matches = str(name) == "sub" if nested else str(name).startswith(".staging-")
                    if matches:
                        raise OSError("injected new directory open failure")
                    return original(name, *args, **kwargs)
                with mock.patch("os.open", side_effect=fail):
                    with self.assertRaises(ArtifactStoreError):
                        stage_archive(_build({"sub/a": b"ok"}), store_root=root)
                staging, = root.iterdir()
                self.assertTrue(staging.name.startswith(".staging-"))
                remaining = staging / "sub" if nested else staging
                self.assertEqual(list(remaining.iterdir()), [])

    def test_directory_identity_failure_closes_descriptor_and_leaves_name(self):
        for nested in (False, True):
            with self.subTest(nested=nested), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                original_open, original_fstat = os.open, os.fstat
                unidentified_fd = None
                def capture(name, *args, **kwargs):
                    nonlocal unidentified_fd
                    fd = original_open(name, *args, **kwargs)
                    matches = str(name) == "sub" if nested else str(name).startswith(".staging-")
                    if matches:
                        unidentified_fd = fd
                    return fd
                def fail(fd):
                    if fd == unidentified_fd:
                        raise OSError("injected descriptor identity failure")
                    return original_fstat(fd)
                with mock.patch("os.open", side_effect=capture), mock.patch("os.fstat", side_effect=fail):
                    with self.assertRaises(ArtifactStoreError):
                        stage_archive(_build({"sub/a": b"ok"}), store_root=root)
                self.assertIsNotNone(unidentified_fd)
                with self.assertRaises(OSError):
                    original_fstat(unidentified_fd)
                staging, = root.iterdir()
                remaining = staging / "sub" if nested else staging
                self.assertEqual(list(remaining.iterdir()), [])

    def test_directory_first_path_stat_failure_cleans_known_identity(self):
        for nested in (False, True):
            with self.subTest(nested=nested), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                original = os.stat
                failed = False
                def fail(name, *args, **kwargs):
                    nonlocal failed
                    matches = str(name) == "sub" if nested else str(name).startswith(".staging-")
                    if matches and not failed:
                        failed = True
                        raise OSError("injected first pathname stat failure")
                    return original(name, *args, **kwargs)
                with mock.patch("os.stat", side_effect=fail):
                    with self.assertRaises(ArtifactStoreError):
                        stage_archive(_build({"sub/a": b"ok"}), store_root=root)
                self.assertTrue(failed)
                self.assertEqual(list(root.iterdir()), [])

    def test_directory_failure_preserves_replacement_before_and_after_identity(self):
        for boundary in ("open", "stat"):
            for nested in (False, True):
                with self.subTest(boundary=boundary, nested=nested), tempfile.TemporaryDirectory() as td:
                    root = Path(td).resolve()
                    original = getattr(os, boundary)
                    replaced = False
                    replacement_identity = None
                    def replace(name, *args, **kwargs):
                        nonlocal replaced, replacement_identity
                        matches = str(name) == "sub" if nested else str(name).startswith(".staging-")
                        if matches and not replaced:
                            replaced = True
                            parent = kwargs["dir_fd"]
                            os.rename(name, str(name) + "-moved", src_dir_fd=parent, dst_dir_fd=parent)
                            os.mkdir(name, 0o700, dir_fd=parent)
                            replacement_identity = os.lstat(name, dir_fd=parent).st_ino
                            raise OSError("injected failure after replacement")
                        return original(name, *args, **kwargs)
                    with mock.patch("os." + boundary, side_effect=replace):
                        with self.assertRaises(ArtifactStoreError):
                            stage_archive(_build({"sub/a": b"ok"}), store_root=root)
                    self.assertTrue(replaced)
                    staging = next(path for path in root.iterdir() if not path.name.endswith("-moved"))
                    replacement = staging / "sub" if nested else staging
                    self.assertEqual(replacement.stat().st_ino, replacement_identity)
                    self.assertEqual(list(replacement.iterdir()), [])
                    moved = replacement.with_name(replacement.name + "-moved")
                    self.assertTrue(moved.is_dir())

    def test_implicit_parent_modes_and_reuse(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            blob = _build({"one/two/a": b"ok"})
            result = stage_archive(blob, store_root=root)
            artifact = Path(result.artifact_path)
            for directory in (artifact, artifact / "one", artifact / "one/two"):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertTrue(stage_archive(blob, store_root=root).already_present)

    def test_existing_extra_files_and_modes_rejected(self):
        for mutation in ("extra", "file_mode", "directory_mode", "hardlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                blob = _build({"sub/a": b"ok"})
                artifact = Path(stage_archive(blob, store_root=root).artifact_path)
                if mutation == "extra":
                    (artifact / "untracked").write_bytes(b"extra")
                elif mutation == "file_mode":
                    (artifact / "sub/a").chmod(0o644)
                elif mutation == "directory_mode":
                    (artifact / "sub").chmod(0o755)
                else:
                    os.link(artifact / "sub/a", root / "outside-link")
                with self.assertRaises(ArtifactCorruptedError):
                    stage_archive(blob, store_root=root)
                self.assertTrue(artifact.is_dir())

    def test_existing_symlink_implicit_parent_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            blob = _build({"sub/a": b"ok"})
            artifact = Path(stage_archive(blob, store_root=root).artifact_path)
            outside = root / "outside"
            (artifact / "sub").rename(outside)
            (artifact / "sub").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ArtifactCorruptedError):
                stage_archive(blob, store_root=root)
            self.assertEqual((outside / "a").read_bytes(), b"ok")

    def test_crc_collision_does_not_substitute_for_exact_bytes(self):
        import zlib
        wanted = b"goodgood"
        base = b"evil" + bytes(4)
        crc = zlib.crc32(base)
        basis = {}
        # Solve the 32-bit linear CRC map for an equal-length forged suffix.
        for bit in range(32):
            trial = bytearray(base)
            trial[4 + bit // 8] ^= 1 << (bit % 8)
            value, mask = zlib.crc32(trial) ^ crc, 1 << bit
            while value:
                top = value.bit_length() - 1
                if top not in basis:
                    basis[top] = (value, mask)
                    break
                old, combination = basis[top]
                value ^= old
                mask ^= combination
        value, mask = zlib.crc32(wanted) ^ crc, 0
        while value:
            old, combination = basis[value.bit_length() - 1]
            value ^= old
            mask ^= combination
        forged = b"evil" + mask.to_bytes(4, "little")
        self.assertNotEqual(forged, wanted)
        self.assertEqual(zlib.crc32(forged), zlib.crc32(wanted))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            blob = _build({"a": wanted})
            artifact = Path(stage_archive(blob, store_root=root).artifact_path)
            (artifact / "a").write_bytes(forged)
            with self.assertRaises(ArtifactCorruptedError):
                stage_archive(blob, store_root=root)
            self.assertEqual((artifact / "a").read_bytes(), forged)

    def test_zero_progress_write_fails_and_cleans(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            with mock.patch("os.write", return_value=0):
                with self.assertRaises(ArtifactStoreError):
                    stage_archive(_build({"a": b"ok"}), store_root=root)
            self.assertEqual(list(root.iterdir()), [])

    def test_postpublication_fsync_failure_preserves_published_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            identity = (root.stat().st_dev, root.stat().st_ino)
            original = os.fsync
            def fail_root(fd):
                observed = os.fstat(fd)
                if (observed.st_dev, observed.st_ino) == identity:
                    raise OSError("injected postpublish fsync failure")
                return original(fd)
            blob = _build({"a": b"ok"})
            with mock.patch("os.fsync", side_effect=fail_root):
                with self.assertRaises(ArtifactStoreError):
                    stage_archive(blob, store_root=root)
            self.assertEqual((root / hashlib.sha256(blob).hexdigest() / "a").read_bytes(), b"ok")
            self.assertTrue(stage_archive(blob, store_root=root).already_present)

    def test_accepted_zip_comment_with_eocd_signature_stages(self):
        import struct
        blob = bytearray(_build({"a": b"ok"}))
        comment = b"comment PK\x05\x06 marker"
        struct.pack_into("<H", blob, len(blob) - 2, len(comment))
        blob.extend(comment)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            result = stage_archive(blob, store_root=root)
            self.assertEqual((Path(result.artifact_path) / "a").read_bytes(), b"ok")

    def test_unsupported_publication_adapter_cleans_without_fallback(self):
        def unavailable(fd, source, target):
            raise NotImplementedError()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            with self.assertRaises(ArtifactStoreError) as caught:
                stage_archive(_build({"a": b"ok"}), store_root=root, publisher=unavailable)
            self.assertEqual(caught.exception.code, "unsupported_platform")
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
