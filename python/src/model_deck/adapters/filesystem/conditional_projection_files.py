from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

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
    ProjectionFileReceipt,
)

_LOCK_BASENAME = ".model_deck_conditional_projection.lock"
_TEMP_PREFIX = ".model_deck_conditional_projection.tmp."
_SHA256_HEX_LEN = 64

_ROOT_OPEN_FLAGS = os.O_RDONLY
if hasattr(os, "O_DIRECTORY"):
    _ROOT_OPEN_FLAGS |= os.O_DIRECTORY
if hasattr(os, "O_NOFOLLOW"):
    _ROOT_OPEN_FLAGS |= os.O_NOFOLLOW

_FILE_READ_FLAGS = os.O_RDONLY
if hasattr(os, "O_NONBLOCK"):
    _FILE_READ_FLAGS |= os.O_NONBLOCK
if hasattr(os, "O_NOFOLLOW"):
    _FILE_READ_FLAGS |= os.O_NOFOLLOW

_TEMP_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    _TEMP_OPEN_FLAGS |= os.O_NOFOLLOW

_LOCK_OPEN_FLAGS = os.O_CREAT | os.O_RDWR
if hasattr(os, "O_NOFOLLOW"):
    _LOCK_OPEN_FLAGS |= os.O_NOFOLLOW


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    exists: bool
    digest: str | None = None
    data: bytes | None = None
    device: int | None = None
    inode: int | None = None
    reason: str | None = None


class _SymlinkAncestor(Exception):
    pass


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_expected_sha256(expected_sha256: str | None) -> None:
    if expected_sha256 is None:
        return
    if not isinstance(expected_sha256, str):
        raise TypeError("expected_sha256 must be str or None")
    if len(expected_sha256) != _SHA256_HEX_LEN or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        raise ValueError("expected_sha256 must be lowercase 64-character hex")


def _validate_caller_path(path: Path) -> Path:
    if not isinstance(path, Path):
        raise TypeError("path must be pathlib.Path")
    if path.is_absolute():
        raise ValueError("path must be relative")
    if not path.parts or path == Path("."):
        raise ValueError("path must be nonempty")
    if any(part in (".", "..") for part in path.parts):
        raise ValueError("path must not contain . or ..")
    if "\x00" in path.as_posix():
        raise ValueError("path must not contain NUL")
    return path


def _absolute_without_following(path: Path) -> Path:
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.normpath(path))


def _path_contains_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            return True
    return False


def _read_snapshot_at(parent_fd: int, name: str) -> _FileSnapshot:
    try:
        fd = os.open(name, _FILE_READ_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return _FileSnapshot(exists=False)
    except OSError as error:
        if error.errno == errno.ELOOP:
            return _FileSnapshot(exists=True, reason=PROJECTION_FILE_REASON_SYMLINK)
        raise
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return _FileSnapshot(exists=True, reason=PROJECTION_FILE_REASON_FOREIGN_OWNER)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    before_fields = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    after_fields = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    reason = None
    if before_fields != after_fields:
        reason = PROJECTION_FILE_REASON_HASH_MISMATCH
    return _FileSnapshot(exists=True, digest=_sha256_hex(data), data=data, device=after.st_dev, inode=after.st_ino, reason=reason)


def _unlink_at_quiet(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError:
        return


class FixtureConditionalProjectionFiles:
    """Conditional projection files restricted to an injected fixture root."""

    def __init__(self, root: Path, *, owned_prefix: bytes) -> None:
        if not isinstance(root, Path):
            raise TypeError("root must be pathlib.Path")
        if not isinstance(owned_prefix, bytes) or not owned_prefix:
            raise ValueError("owned_prefix must be nonempty bytes")
        normalized_root = _absolute_without_following(root)
        if _path_contains_symlink(normalized_root):
            raise ValueError("root path must not contain a symlink")
        try:
            root_stat = os.lstat(normalized_root)
        except FileNotFoundError:
            raise ValueError("root must be an existing directory") from None
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError("root must be an existing directory")
        self._root = normalized_root
        self._root_device = root_stat.st_dev
        self._root_inode = root_stat.st_ino
        self._owned_prefix = owned_prefix

    def compare_and_write(self, path: Path, data: bytes, expected_sha256: str | None) -> ProjectionFileReceipt:
        relative_path = _validate_caller_path(path)
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        if not data.startswith(self._owned_prefix):
            raise ValueError("data must begin with owned_prefix")
        _validate_expected_sha256(expected_sha256)
        with self._exclusive_lock() as root_fd:
            return self._compare_and_write_locked(root_fd, relative_path, data, expected_sha256)

    def compare_and_delete(self, path: Path, expected_sha256: str | None) -> ProjectionFileReceipt:
        relative_path = _validate_caller_path(path)
        _validate_expected_sha256(expected_sha256)
        with self._exclusive_lock() as root_fd:
            return self._compare_and_delete_locked(root_fd, relative_path, expected_sha256)

    def _open_verified_root_fd(self) -> int:
        parts = self._root.parts
        try:
            anchor = os.open(self._root.anchor, _ROOT_OPEN_FLAGS)
        except FileNotFoundError:
            raise ValueError("fixture root changed after adapter construction") from None
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ValueError("fixture root changed after adapter construction") from None
            raise
        if len(parts) <= 1:
            try:
                current = os.fstat(anchor)
                if not stat.S_ISDIR(current.st_mode):
                    raise ValueError("fixture root changed after adapter construction")
                if current.st_dev != self._root_device or current.st_ino != self._root_inode:
                    raise ValueError("fixture root changed after adapter construction")
                return anchor
            except Exception:
                os.close(anchor)
                raise
        current = anchor
        try:
            for component in parts[1:]:
                try:
                    nxt = os.open(component, _ROOT_OPEN_FLAGS, dir_fd=current)
                except FileNotFoundError:
                    raise ValueError("fixture root changed after adapter construction") from None
                except OSError as error:
                    if error.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise ValueError("fixture root changed after adapter construction") from None
                    raise
                os.close(current)
                current = nxt
            verified = os.fstat(current)
            if not stat.S_ISDIR(verified.st_mode):
                raise ValueError("fixture root changed after adapter construction")
            if verified.st_dev != self._root_device or verified.st_ino != self._root_inode:
                raise ValueError("fixture root changed after adapter construction")
            return current
        except Exception:
            try:
                os.close(current)
            except OSError:
                pass
            raise

    @contextmanager
    def _exclusive_lock(self) -> Iterator[int]:
        root_fd = self._open_verified_root_fd()
        try:
            tries = 0
            while True:
                try:
                    lock_fd = os.open(_LOCK_BASENAME, _LOCK_OPEN_FLAGS, 0o600, dir_fd=root_fd)
                    break
                except FileNotFoundError:
                    tries += 1
                    if tries > 32:
                        raise
                except OSError as error:
                    if error.errno == errno.ELOOP:
                        raise ValueError("projection lock must be a regular file") from None
                    raise
            try:
                lock_is_regular = stat.S_ISREG(os.fstat(lock_fd).st_mode)
            except OSError:
                os.close(lock_fd)
                raise
            if not lock_is_regular:
                os.close(lock_fd)
                raise ValueError("projection lock must be a regular file")
            try:
                current = os.fstat(root_fd)
                if current.st_dev != self._root_device or current.st_ino != self._root_inode:
                    raise ValueError("fixture root changed after adapter construction")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                yield root_fd
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        finally:
            os.close(root_fd)

    def _open_parent_fd(self, root_fd: int, relative_path: Path) -> tuple[int, bool]:
        parts = relative_path.parts
        if len(parts) == 1:
            return root_fd, False
        current = root_fd
        owned = False
        try:
            for component in parts[:-1]:
                try:
                    new_fd = os.open(component, _ROOT_OPEN_FLAGS, dir_fd=current)
                except FileNotFoundError:
                    raise ValueError("parent directory must exist") from None
                except OSError as error:
                    if error.errno == errno.ELOOP:
                        raise _SymlinkAncestor() from None
                    if error.errno == errno.ENOTDIR:
                        try:
                            offender = os.lstat(component, dir_fd=current).st_mode
                        except OSError:
                            raise ValueError("parent directory must exist and must not be a symlink") from None
                        if stat.S_ISLNK(offender):
                            raise _SymlinkAncestor() from None
                        raise ValueError("parent directory must exist and must not be a symlink") from None
                    raise
                if owned:
                    os.close(current)
                current = new_fd
                owned = True
        except Exception:
            if owned:
                try:
                    os.close(current)
                except OSError:
                    pass
            raise
        try:
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise ValueError("parent directory must exist and must not be a symlink")
        except Exception:
            if owned:
                os.close(current)
            raise
        return current, owned

    def _receipt(self, operation: str, path: Path, outcome: str, expected_sha256: str | None, observed_sha256: str | None, result_sha256: str | None, reason: str | None) -> ProjectionFileReceipt:
        return ProjectionFileReceipt(operation=operation, path=path, outcome=outcome, expected_sha256=expected_sha256, observed_sha256=observed_sha256, result_sha256=result_sha256, reason=reason)

    def _temp_name(self) -> str:
        return f"{_TEMP_PREFIX}{os.getpid()}.{secrets.token_hex(8)}"

    def _write_temp_at(self, parent_fd: int, data: bytes) -> str:
        for _ in range(16):
            name = self._temp_name()
            try:
                fd = os.open(name, _TEMP_OPEN_FLAGS, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                continue
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                _unlink_at_quiet(parent_fd, name)
                raise
            return name
        raise FileExistsError("could not allocate a private projection temp file")

    def _atomic_create_at(self, parent_fd: int, name: str, data: bytes) -> None:
        temp = self._write_temp_at(parent_fd, data)
        try:
            os.link(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            _unlink_at_quiet(parent_fd, temp)
            os.fsync(parent_fd)
        except Exception:
            _unlink_at_quiet(parent_fd, temp)
            raise

    def _replace_via_temp_at(self, parent_fd: int, name: str, data: bytes) -> None:
        temp = self._write_temp_at(parent_fd, data)
        try:
            os.replace(temp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        except Exception:
            _unlink_at_quiet(parent_fd, temp)
            raise
        finally:
            _unlink_at_quiet(parent_fd, temp)

    def _replace_via_temp(self, parent_fd: int, name: str, data: bytes) -> None:
        self._replace_via_temp_at(parent_fd, name, data)

    def _postwrite_check_at(self, parent_fd: int, name: str, desired_hash: str) -> tuple[str | None, str | None]:
        snapshot = _read_snapshot_at(parent_fd, name)
        if not snapshot.exists or snapshot.reason is not None or snapshot.digest != desired_hash:
            return snapshot.digest, PROJECTION_FILE_REASON_POSTWRITE_INTERFERENCE
        return snapshot.digest, None

    def _pre_mutation_identity_ok_at(self, parent_fd: int, name: str, expected_sha256: str, observed: _FileSnapshot) -> tuple[bool, str | None, str | None]:
        current = _read_snapshot_at(parent_fd, name)
        if not current.exists:
            return False, None, PROJECTION_FILE_REASON_MISSING
        if current.reason is not None:
            return False, current.digest, current.reason
        if current.data is None or not current.data.startswith(self._owned_prefix):
            return False, current.digest, PROJECTION_FILE_REASON_FOREIGN_OWNER
        if current.digest != expected_sha256:
            return False, current.digest, PROJECTION_FILE_REASON_HASH_MISMATCH
        if current.device != observed.device or current.inode != observed.inode or current.digest != observed.digest:
            return False, current.digest, PROJECTION_FILE_REASON_HASH_MISMATCH
        return True, current.digest, None

    def _compare_and_write_locked(self, root_fd: int, relative_path: Path, data: bytes, expected_sha256: str | None) -> ProjectionFileReceipt:
        operation = PROJECTION_FILE_OPERATION_WRITE
        target_name = relative_path.parts[-1]
        try:
            parent_fd, owns_parent = self._open_parent_fd(root_fd, relative_path)
        except _SymlinkAncestor:
            return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_CONFLICT, expected_sha256, None, None, PROJECTION_FILE_REASON_SYMLINK)
        try:
            desired_hash = _sha256_hex(data)
            observed = _read_snapshot_at(parent_fd, target_name)
            if observed.reason == PROJECTION_FILE_REASON_SYMLINK:
                return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_CONFLICT, expected_sha256, None, None, PROJECTION_FILE_REASON_SYMLINK)
            if expected_sha256 is None:
                if observed.exists:
                    reason = PROJECTION_FILE_REASON_UNEXPECTED_EXISTING
                    if observed.reason == PROJECTION_FILE_REASON_FOREIGN_OWNER:
                        reason = PROJECTION_FILE_REASON_FOREIGN_OWNER
                    return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_CONFLICT, None, observed.digest, observed.digest, reason)
                try:
                    self._atomic_create_at(parent_fd, target_name, data)
                except FileExistsError:
                    raced = _read_snapshot_at(parent_fd, target_name)
                    reason = PROJECTION_FILE_REASON_UNEXPECTED_EXISTING
                    if raced.reason == PROJECTION_FILE_REASON_SYMLINK:
                        reason = PROJECTION_FILE_REASON_SYMLINK
                    return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_CONFLICT, None, raced.digest, raced.digest, reason)
                result_hash, interference = self._postwrite_check_at(parent_fd, target_name, desired_hash)
                outcome = PROJECTION_FILE_OUTCOME_APPLIED if interference is None else PROJECTION_FILE_OUTCOME_CONFLICT
                return self._receipt(operation, relative_path, outcome, None, None, result_hash, interference)
            if not observed.exists:
                return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_CONFLICT, expected_sha256, None, None, PROJECTION_FILE_REASON_MISSING)
            if observed.reason is not None:
                return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_CONFLICT, expected_sha256, observed.digest, observed.digest, observed.reason)
            ok, current_hash, conflict_reason = self._pre_mutation_identity_ok_at(parent_fd, target_name, expected_sha256, observed)
            if not ok:
                return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_CONFLICT, expected_sha256, observed.digest, current_hash, conflict_reason)
            if observed.digest == desired_hash:
                return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_NOOP, expected_sha256, observed.digest, observed.digest, None)
            self._replace_via_temp(parent_fd, target_name, data)
            result_hash, interference = self._postwrite_check_at(parent_fd, target_name, desired_hash)
            outcome = PROJECTION_FILE_OUTCOME_APPLIED if interference is None else PROJECTION_FILE_OUTCOME_CONFLICT
            return self._receipt(operation, relative_path, outcome, expected_sha256, observed.digest, result_hash, interference)
        finally:
            if owns_parent:
                os.close(parent_fd)

    def _compare_and_delete_locked(self, root_fd: int, relative_path: Path, expected_sha256: str | None) -> ProjectionFileReceipt:
        operation = PROJECTION_FILE_OPERATION_DELETE
        target_name = relative_path.parts[-1]
        try:
            parent_fd, owns_parent = self._open_parent_fd(root_fd, relative_path)
        except _SymlinkAncestor:
            return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_CONFLICT, expected_sha256, None, None, PROJECTION_FILE_REASON_SYMLINK)
        try:
            observed = _read_snapshot_at(parent_fd, target_name)
            if observed.reason == PROJECTION_FILE_REASON_SYMLINK:
                return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_CONFLICT, expected_sha256, None, None, PROJECTION_FILE_REASON_SYMLINK)
            if expected_sha256 is None:
                if not observed.exists:
                    return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_NOOP, None, None, None, None)
                reason = PROJECTION_FILE_REASON_UNEXPECTED_EXISTING
                if observed.reason == PROJECTION_FILE_REASON_FOREIGN_OWNER:
                    reason = PROJECTION_FILE_REASON_FOREIGN_OWNER
                return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_CONFLICT, None, observed.digest, observed.digest, reason)
            if not observed.exists:
                return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_CONFLICT, expected_sha256, None, None, PROJECTION_FILE_REASON_MISSING)
            if observed.reason is not None:
                return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_CONFLICT, expected_sha256, observed.digest, observed.digest, observed.reason)
            ok, current_hash, conflict_reason = self._pre_mutation_identity_ok_at(parent_fd, target_name, expected_sha256, observed)
            if not ok:
                return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_CONFLICT, expected_sha256, observed.digest, current_hash, conflict_reason)
            os.unlink(target_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            result = _read_snapshot_at(parent_fd, target_name)
            if not result.exists:
                return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_APPLIED, expected_sha256, observed.digest, None, None)
            return self._receipt(operation, relative_path, PROJECTION_FILE_OUTCOME_CONFLICT, expected_sha256, observed.digest, result.digest, PROJECTION_FILE_REASON_POSTDELETE_INTERFERENCE)
        finally:
            if owns_parent:
                os.close(parent_fd)
