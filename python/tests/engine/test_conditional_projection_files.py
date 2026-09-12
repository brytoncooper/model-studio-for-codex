from __future__ import annotations

import hashlib
import multiprocessing
import os
import unittest
from dataclasses import fields
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from model_deck.adapters.filesystem.conditional_projection_files import (
    FixtureConditionalProjectionFiles,
)
from model_deck.engine.projections.file_port import (
    PROJECTION_FILE_OPERATION_DELETE,
    PROJECTION_FILE_OPERATION_WRITE,
    PROJECTION_FILE_OUTCOME_APPLIED,
    PROJECTION_FILE_OUTCOME_CONFLICT,
    PROJECTION_FILE_OUTCOME_NOOP,
    PROJECTION_FILE_REASON_FOREIGN_OWNER,
    PROJECTION_FILE_REASON_HASH_MISMATCH,
    PROJECTION_FILE_REASON_MISSING,
    PROJECTION_FILE_REASON_POSTDELETE_INTERFERENCE,
    PROJECTION_FILE_REASON_POSTWRITE_INTERFERENCE,
    PROJECTION_FILE_REASON_SYMLINK,
    PROJECTION_FILE_REASON_UNEXPECTED_EXISTING,
    ConditionalProjectionFiles,
    ProjectionFileReceipt,
)

MARK = b"# model_deck_owned\n"
MODULE = "model_deck.adapters.filesystem.conditional_projection_files"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _owned(body: bytes = b"payload") -> bytes:
    return MARK + body


def _adapter(root: Path) -> FixtureConditionalProjectionFiles:
    root = Path(os.path.realpath(root))
    (root / "dir").mkdir(parents=True, exist_ok=True)
    return FixtureConditionalProjectionFiles(root, owned_prefix=MARK)


def _mp_hold_lock_worker(
    root_text: str,
    acquired: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    adapter = FixtureConditionalProjectionFiles(Path(os.path.realpath(root_text)), owned_prefix=MARK)
    with adapter._exclusive_lock():
        acquired.set()
        release.wait(timeout=10)


def _mp_blocked_write_worker(
    root_text: str,
    results: multiprocessing.queues.Queue,
) -> None:
    adapter = FixtureConditionalProjectionFiles(Path(os.path.realpath(root_text)), owned_prefix=MARK)
    receipt = adapter.compare_and_write(Path("dir/blocked.toml"), _owned(b"second"), None)
    results.put(receipt.outcome)


def _mp_write_worker(
    root_text: str,
    body: bytes,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    adapter = FixtureConditionalProjectionFiles(Path(os.path.realpath(root_text)), owned_prefix=MARK)
    start.wait()
    receipt = adapter.compare_and_write(Path("dir/shared.toml"), _owned(body), None)
    results.put((receipt.outcome, body))


class ConditionalProjectionFilesTests(unittest.TestCase):
    def test_adapter_satisfies_port(self) -> None:
        with TemporaryDirectory() as temporary:
            adapter = _adapter(Path(temporary))
            self.assertIsInstance(adapter, ConditionalProjectionFiles)

    def test_absent_only_create(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = _adapter(root)
            data = _owned(b"one")
            relative_path = Path("dir/new.toml")

            receipt = adapter.compare_and_write(relative_path, data, None)

            self.assertEqual(receipt.operation, PROJECTION_FILE_OPERATION_WRITE)
            self.assertEqual(receipt.outcome, PROJECTION_FILE_OUTCOME_APPLIED)
            self.assertIsNone(receipt.observed_sha256)
            self.assertEqual(receipt.result_sha256, _sha(data))
            self.assertEqual((root / relative_path).read_bytes(), data)

    def test_existing_identical_still_conflicts_with_absent_precondition(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = _adapter(root)
            relative_path = Path("dir/existing.toml")
            data = _owned(b"same")
            (root / relative_path).write_bytes(data)

            receipt = adapter.compare_and_write(relative_path, data, None)

            self.assertEqual(receipt.outcome, PROJECTION_FILE_OUTCOME_CONFLICT)
            self.assertEqual(receipt.reason, PROJECTION_FILE_REASON_UNEXPECTED_EXISTING)
            self.assertEqual(receipt.observed_sha256, _sha(data))
            self.assertEqual((root / relative_path).read_bytes(), data)

    def test_initial_create_race_never_clobbers_foreign_file(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = _adapter(root)
            relative_path = Path("dir/race.toml")
            foreign = b"external writer\n"
            original_link = os.link

            def race_link(source: str | Path, target: str | Path, *args, **kwargs) -> None:
                dir_fd = kwargs.get("dst_dir_fd")
                race_fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=dir_fd)
                with os.fdopen(race_fd, "wb") as handle:
                    handle.write(foreign)
                original_link(source, target, *args, **kwargs)

            with mock.patch(f"{MODULE}.os.link", side_effect=race_link):
                receipt = adapter.compare_and_write(relative_path, _owned(b"ours"), None)

            self.assertEqual(receipt.outcome, PROJECTION_FILE_OUTCOME_CONFLICT)
            self.assertEqual(receipt.reason, PROJECTION_FILE_REASON_UNEXPECTED_EXISTING)
            self.assertEqual(receipt.observed_sha256, _sha(foreign))
            self.assertEqual((root / relative_path).read_bytes(), foreign)

    def test_expected_hash_replace_and_matching_write_noop(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = _adapter(root)
            relative_path = Path("dir/replace.toml")
            old = _owned(b"old")
            new = _owned(b"new")
            (root / relative_path).write_bytes(old)

            replaced = adapter.compare_and_write(relative_path, new, _sha(old))
            noop = adapter.compare_and_write(relative_path, new, _sha(new))

            self.assertEqual(replaced.outcome, PROJECTION_FILE_OUTCOME_APPLIED)
            self.assertEqual(replaced.observed_sha256, _sha(old))
            self.assertEqual(replaced.result_sha256, _sha(new))
            self.assertEqual(noop.outcome, PROJECTION_FILE_OUTCOME_NOOP)
            self.assertEqual((root / relative_path).read_bytes(), new)

    def test_delete_and_absent_delete_noop(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = _adapter(root)
            relative_path = Path("dir/gone.toml")
            data = _owned(b"bye")
            (root / relative_path).write_bytes(data)

            deleted = adapter.compare_and_delete(relative_path, _sha(data))
            missing = adapter.compare_and_delete(relative_path, None)

            self.assertEqual(deleted.operation, PROJECTION_FILE_OPERATION_DELETE)
            self.assertEqual(deleted.outcome, PROJECTION_FILE_OUTCOME_APPLIED)
            self.assertEqual(deleted.observed_sha256, _sha(data))
            self.assertIsNone(deleted.result_sha256)
            self.assertEqual(missing.outcome, PROJECTION_FILE_OUTCOME_NOOP)

    def test_foreign_file_is_preserved(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = _adapter(root)
            relative_path = Path("dir/foreign.toml")
            foreign = b"user-owned\n"
            (root / relative_path).write_bytes(foreign)

            receipt = adapter.compare_and_write(relative_path, _owned(b"x"), _sha(foreign))

            self.assertEqual(receipt.outcome, PROJECTION_FILE_OUTCOME_CONFLICT)
            self.assertEqual(receipt.reason, PROJECTION_FILE_REASON_FOREIGN_OWNER)
            self.assertEqual((root / relative_path).read_bytes(), foreign)

    def test_foreign_directory_is_not_treated_as_absent(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = _adapter(root)
            relative_path = Path("dir/existing-directory")
            (root / relative_path).mkdir()

            receipt = adapter.compare_and_write(relative_path, _owned(b"x"), None)

            self.assertEqual(receipt.outcome, PROJECTION_FILE_OUTCOME_CONFLICT)
            self.assertEqual(receipt.reason, PROJECTION_FILE_REASON_FOREIGN_OWNER)
            self.assertTrue((root / relative_path).is_dir())

    def test_external_hash_drift_before_mutation_is_preserved(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = _adapter(root)
            relative_path = Path("dir/drift.toml")
            original = _owned(b"original")
            external = _owned(b"external edit")
            (root / relative_path).write_bytes(original)
            expected_hash = _sha(original)
            (root / relative_path).write_bytes(external)

            receipt = adapter.compare_and_write(
                relative_path,
                _owned(b"requested"),
                expected_hash,
            )

            self.assertEqual(receipt.reason, PROJECTION_FILE_REASON_HASH_MISMATCH)
            self.assertEqual(receipt.observed_sha256, _sha(external))
            self.assertEqual((root / relative_path).read_bytes(), external)

    def test_missing_target_conflicts_with_expected_hash(self) -> None:
        with TemporaryDirectory() as temporary:
            adapter = _adapter(Path(temporary))
            relative_path = Path("dir/missing.toml")

            write = adapter.compare_and_write(relative_path, _owned(b"new"), "a" * 64)
            delete = adapter.compare_and_delete(relative_path, "b" * 64)

            self.assertEqual(write.reason, PROJECTION_FILE_REASON_MISSING)
            self.assertEqual(delete.reason, PROJECTION_FILE_REASON_MISSING)

    def test_invalid_paths_hashes_and_payloads_are_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = _adapter(root)
            data = _owned(b"ok")
            invalid_paths = (
                Path("."),
                Path("../escape.toml"),
                Path("dir/../escape.toml"),
                Path("/absolute.toml"),
                Path("dir/nul\x00.toml"),
            )
            for invalid_path in invalid_paths:
                with self.subTest(path=invalid_path):
                    with self.assertRaises(ValueError):
                        adapter.compare_and_write(invalid_path, data, None)
            with self.assertRaises(ValueError):
                adapter.compare_and_write(Path("missing-parent/x.toml"), data, None)
            with self.assertRaises(TypeError):
                adapter.compare_and_write("dir/x.toml", data, None)
            with self.assertRaises(TypeError):
                adapter.compare_and_write(Path("dir/x.toml"), bytearray(data), None)
            with self.assertRaises(ValueError):
                adapter.compare_and_write(Path("dir/x.toml"), b"no marker", None)
            for invalid_hash in ("a" * 63, "A" * 64, "g" * 64):
                with self.subTest(expected_sha256=invalid_hash):
                    with self.assertRaises(ValueError):
                        adapter.compare_and_delete(Path("dir/x.toml"), invalid_hash)
            with self.assertRaises(TypeError):
                adapter.compare_and_delete(Path("dir/x.toml"), 64)

    def test_root_ancestor_and_target_symlinks_are_rejected_without_following(self) -> None:
        with TemporaryDirectory() as temporary:
            container = Path(temporary)
            real = container / "real"
            fixture = real / "fixture"
            fixture.mkdir(parents=True)
            alias = container / "alias"
            os.symlink(real, alias)
            with self.assertRaises(ValueError):
                FixtureConditionalProjectionFiles(alias / "fixture", owned_prefix=MARK)

            adapter = _adapter(container)
            outside = container / "outside"
            outside.mkdir()
            os.symlink(outside, container / "dir_link")
            ancestor_receipt = adapter.compare_and_write(
                Path("dir_link/file.toml"),
                _owned(b"x"),
                None,
            )
            self.assertEqual(ancestor_receipt.reason, PROJECTION_FILE_REASON_SYMLINK)
            self.assertFalse((outside / "file.toml").exists())

            target = container / "dir" / "target.toml"
            os.symlink(outside / "missing.toml", target)
            target_receipt = adapter.compare_and_write(
                Path("dir/target.toml"),
                _owned(b"y"),
                None,
            )
            self.assertEqual(target_receipt.reason, PROJECTION_FILE_REASON_SYMLINK)

    def test_replaced_root_is_rejected_before_lock_or_target_creation(self) -> None:
        with TemporaryDirectory() as temporary:
            container = Path(temporary)
            root = container / "fixture"
            external = container / "external"
            root.mkdir()
            external.mkdir()
            adapter = _adapter(root)
            moved_root = container / "moved-fixture"
            os.rename(root, moved_root)
            os.symlink(external, root)
            (external / "dir").mkdir()

            with self.assertRaises(ValueError):
                adapter.compare_and_write(
                    Path("dir/redirected.toml"),
                    _owned(b"must stay confined"),
                    None,
                )

            self.assertFalse(
                (external / ".model_deck_conditional_projection.lock").exists()
            )
            self.assertFalse((external / "dir/redirected.toml").exists())

    def test_swap_after_root_verify_stays_confined_to_verified_directory(self) -> None:
        with TemporaryDirectory() as temporary:
            container = Path(os.path.realpath(temporary))
            root = container / "fixture"
            external = container / "external"
            root.mkdir()
            external.mkdir()
            (external / "dir").mkdir()
            adapter = _adapter(root)
            moved_root = container / "moved-fixture"
            real_open = os.open

            fired: list[bool] = []

            def swapping_open(path, *args, **kwargs):
                fd = real_open(path, *args, **kwargs)
                try:
                    if not fired and root.exists() and not moved_root.exists():
                        os.rename(root, moved_root)
                        os.symlink(external, root)
                        fired.append(True)
                except OSError:
                    pass
                return fd

            with mock.patch(f"{MODULE}.os.open", side_effect=swapping_open):
                try:
                    receipt = adapter.compare_and_write(
                        Path("dir/swapped.toml"),
                        _owned(b"must stay confined"),
                        None,
                    )
                except ValueError:
                    receipt = None

            self.assertFalse(
                (external / ".model_deck_conditional_projection.lock").exists()
            )
            self.assertFalse((external / "dir/swapped.toml").exists())
            self.assertTrue(moved_root.is_dir() and not moved_root.is_symlink())
            self.assertTrue(root.is_symlink())
            if receipt is not None:
                self.assertEqual(receipt.outcome, PROJECTION_FILE_OUTCOME_APPLIED)
                self.assertEqual(
                    (moved_root / "dir/swapped.toml").read_bytes(),
                    _owned(b"must stay confined"),
                )

    def test_temp_files_are_cleaned_after_create_replace_and_write_failures(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = _adapter(root)
            relative_path = Path("dir/failure.toml")
            with mock.patch(f"{MODULE}.os.link", side_effect=OSError("link failed")):
                with self.assertRaises(OSError):
                    adapter.compare_and_write(relative_path, _owned(b"create"), None)
            self.assertEqual(list((root / "dir").glob(f"{'.model_deck_conditional_projection.tmp.'}*")), [])

            base = _owned(b"base")
            (root / relative_path).write_bytes(base)
            with mock.patch(f"{MODULE}.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    adapter.compare_and_write(relative_path, _owned(b"replace"), _sha(base))
            self.assertEqual(list((root / "dir").glob(f"{'.model_deck_conditional_projection.tmp.'}*")), [])

            (root / relative_path).unlink()
            with mock.patch(f"{MODULE}.os.fsync", side_effect=OSError("fsync failed")):
                with self.assertRaises(OSError):
                    adapter.compare_and_write(relative_path, _owned(b"write"), None)
            self.assertEqual(list((root / "dir").glob(f"{'.model_deck_conditional_projection.tmp.'}*")), [])

    def test_process_writers_share_one_exclusive_lock_and_no_clobber_create(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "dir").mkdir()
            start = multiprocessing.Event()
            results = multiprocessing.Queue()
            processes = [
                multiprocessing.Process(
                    target=_mp_write_worker,
                    args=(str(root), body, start, results),
                )
                for body in (b"first", b"second")
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            received = [results.get(timeout=2) for _ in processes]

            self.assertEqual(
                sorted(outcome for outcome, _body in received),
                sorted(
                    (
                        PROJECTION_FILE_OUTCOME_APPLIED,
                        PROJECTION_FILE_OUTCOME_CONFLICT,
                    )
                ),
            )
            applied_body = next(body for outcome, body in received if outcome == PROJECTION_FILE_OUTCOME_APPLIED)
            self.assertEqual((root / "dir/shared.toml").read_bytes(), _owned(applied_body))

    def test_postwrite_interference_reports_and_preserves_external_result(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = _adapter(root)
            relative_path = Path("dir/postwrite.toml")
            original = _owned(b"before")
            external = b"external postwrite\n"
            (root / relative_path).write_bytes(original)
            original_replace = adapter._replace_via_temp

            def replace_then_interfere(parent_fd: int, name: str, data: bytes) -> None:
                original_replace(parent_fd, name, data)
                interfere_fd = os.open(name, os.O_WRONLY | os.O_TRUNC, dir_fd=parent_fd)
                with os.fdopen(interfere_fd, "wb") as handle:
                    handle.write(external)

            with mock.patch.object(
                adapter,
                "_replace_via_temp",
                side_effect=replace_then_interfere,
            ):
                receipt = adapter.compare_and_write(
                    relative_path,
                    _owned(b"requested"),
                    _sha(original),
                )

            self.assertEqual(receipt.outcome, PROJECTION_FILE_OUTCOME_CONFLICT)
            self.assertEqual(receipt.reason, PROJECTION_FILE_REASON_POSTWRITE_INTERFERENCE)
            self.assertEqual(receipt.observed_sha256, _sha(original))
            self.assertEqual(receipt.result_sha256, _sha(external))
            self.assertEqual((root / relative_path).read_bytes(), external)

    def test_postdelete_interference_reports_and_preserves_external_result(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = _adapter(root)
            relative_path = Path("dir/postdelete.toml")
            original = _owned(b"before")
            external = b"external postdelete\n"
            (root / relative_path).write_bytes(original)
            original_unlink = os.unlink

            def unlink_then_interfere(target: str | Path, *args, **kwargs) -> None:
                original_unlink(target, *args, **kwargs)
                parent_fd = kwargs.get("dir_fd")
                if parent_fd is None:
                    Path(str(target)).write_bytes(external)
                else:
                    interfere_fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=parent_fd)
                    with os.fdopen(interfere_fd, "wb") as handle:
                        handle.write(external)

            with mock.patch(f"{MODULE}.os.unlink", side_effect=unlink_then_interfere):
                receipt = adapter.compare_and_delete(relative_path, _sha(original))

            self.assertEqual(receipt.outcome, PROJECTION_FILE_OUTCOME_CONFLICT)
            self.assertEqual(receipt.reason, PROJECTION_FILE_REASON_POSTDELETE_INTERFERENCE)
            self.assertEqual(receipt.observed_sha256, _sha(original))
            self.assertEqual(receipt.result_sha256, _sha(external))
            self.assertEqual((root / relative_path).read_bytes(), external)

    def test_parent_fd_closed_on_missing_later_component_write_and_delete(self) -> None:
        for operation in ("write", "delete"):
            with TemporaryDirectory() as temporary:
                root = Path(os.path.realpath(temporary))
                adapter = _adapter(root)
                (root / "dir" / "valid").mkdir(parents=True, exist_ok=True)
                real_open = os.open
                real_close = os.close
                opened: list[int] = []
                closed: list[int] = []

                def track_open(path, flags, *args, **kwargs):
                    fd = real_open(path, flags, *args, **kwargs)
                    opened.append(fd)
                    return fd

                def track_close(fd):
                    closed.append(fd)
                    return real_close(fd)

                with mock.patch(f"{MODULE}.os.open", side_effect=track_open):
                    with mock.patch(f"{MODULE}.os.close", side_effect=track_close):
                        if operation == "write":
                            with self.assertRaises(ValueError):
                                adapter.compare_and_write(Path("dir/valid/missing/deep.toml"), _owned(b"x"), None)
                        else:
                            with self.assertRaises(ValueError):
                                adapter.compare_and_delete(Path("dir/valid/missing/deep.toml"), None)
                self.assertTrue(opened)
                self.assertEqual(set(opened), set(closed))

    def test_parent_fd_closed_on_symlink_later_component_write_and_delete(self) -> None:
        for operation in ("write", "delete"):
            with TemporaryDirectory() as temporary:
                root = Path(os.path.realpath(temporary))
                adapter = _adapter(root)
                (root / "dir" / "valid").mkdir(parents=True, exist_ok=True)
                (root / "dir" / "linktarget").mkdir(parents=True, exist_ok=True)
                os.symlink(root / "dir" / "linktarget", root / "dir" / "valid" / "link")
                real_open = os.open
                real_close = os.close
                opened: list[int] = []
                closed: list[int] = []

                def track_open(path, flags, *args, **kwargs):
                    fd = real_open(path, flags, *args, **kwargs)
                    opened.append(fd)
                    return fd

                def track_close(fd):
                    closed.append(fd)
                    return real_close(fd)

                with mock.patch(f"{MODULE}.os.open", side_effect=track_open):
                    with mock.patch(f"{MODULE}.os.close", side_effect=track_close):
                        if operation == "write":
                            receipt = adapter.compare_and_write(Path("dir/valid/link/deep.toml"), _owned(b"x"), None)
                        else:
                            receipt = adapter.compare_and_delete(Path("dir/valid/link/deep.toml"), None)
                        self.assertEqual(receipt.outcome, PROJECTION_FILE_OUTCOME_CONFLICT)
                        self.assertEqual(receipt.reason, PROJECTION_FILE_REASON_SYMLINK)
                self.assertTrue(opened)
                self.assertEqual(set(opened), set(closed))

    def test_ancestor_swap_before_call_rejects_without_traversing_symlink(self) -> None:
        for operation in ("write", "delete"):
            with TemporaryDirectory() as temporary:
                base = Path(os.path.realpath(temporary))
                ancestor = base / "ancestor"
                root = ancestor / "root"
                (root / "dir").mkdir(parents=True, exist_ok=True)
                adapter = FixtureConditionalProjectionFiles(root, owned_prefix=MARK)
                if operation == "delete":
                    target = _owned(b"victim")
                    (root / "dir" / "victim.toml").write_bytes(target)
                    wanted = Path("dir/victim.toml")
                    payload = _sha(target)
                else:
                    wanted = Path("dir/swapped.toml")
                    payload = _owned(b"must not escape")
                moved = base / "moved"
                os.rename(ancestor, moved)
                os.symlink(moved, ancestor)
                if operation == "write":
                    with self.assertRaises(ValueError):
                        adapter.compare_and_write(wanted, payload, None)
                else:
                    with self.assertRaises(ValueError):
                        adapter.compare_and_delete(wanted, payload)
                self.assertFalse((moved / "root" / ".model_deck_conditional_projection.lock").exists())
                if operation == "write":
                    self.assertFalse((moved / "root" / wanted).exists())
                else:
                    self.assertEqual((moved / "root" / wanted).read_bytes(), target)

    def test_second_process_blocks_until_lock_holder_releases(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(os.path.realpath(temporary))
            (root / "dir").mkdir()
            acquired = multiprocessing.Event()
            release = multiprocessing.Event()
            results = multiprocessing.Queue()
            holder = multiprocessing.Process(target=_mp_hold_lock_worker, args=(str(root), acquired, release))
            holder.start()
            try:
                self.assertTrue(acquired.wait(timeout=10))
                worker = multiprocessing.Process(target=_mp_blocked_write_worker, args=(str(root), results))
                worker.start()
                worker.join(timeout=1.0)
                self.assertTrue(worker.is_alive())
                release.set()
                worker.join(timeout=10)
                self.assertEqual(worker.exitcode, 0)
                self.assertEqual(results.get(timeout=5), PROJECTION_FILE_OUTCOME_APPLIED)
                self.assertEqual((root / "dir" / "blocked.toml").read_bytes(), _owned(b"second"))
            finally:
                release.set()
                holder.join(timeout=10)
                self.assertEqual(holder.exitcode, 0)

    def test_receipt_has_only_content_free_contract_fields(self) -> None:
        with TemporaryDirectory() as temporary:
            adapter = _adapter(Path(temporary))
            receipt = adapter.compare_and_write(Path("dir/receipt.toml"), _owned(b"r"), None)
            allowed = {field.name for field in fields(ProjectionFileReceipt)}

            self.assertEqual(
                allowed,
                {
                    "operation",
                    "path",
                    "outcome",
                    "expected_sha256",
                    "observed_sha256",
                    "result_sha256",
                    "reason",
                },
            )
            for field_name in allowed:
                value = getattr(receipt, field_name)
                if isinstance(value, str):
                    self.assertNotIn("\n", value)


if __name__ == "__main__":
    unittest.main()
