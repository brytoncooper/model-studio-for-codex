"""Validated, descriptor-confined immutable artifact staging."""
from __future__ import annotations

import hashlib
import io
import os
import secrets
import stat
import struct
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from model_deck.plugins.archive_inspection import ArchiveLimits, inspect_archive
from .publication import DirectoryPublisher, publish_directory_exclusive

_CHUNK = 64 * 1024
_FILE_MODE = 0o600
_DIR_MODE = 0o700
_MAX_PATH_COMPONENTS = 64
_MAX_TREE_NODES = 16384
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class ArtifactStoreError(Exception):
    """Stable, content-free staging failure."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ArtifactCorruptedError(ArtifactStoreError):
    """A published artifact does not match the supplied archive exactly."""


@dataclass(frozen=True)
class StageResult:
    artifact_id: str
    artifact_path: str
    total_entries: int
    total_uncompressed_size: int
    already_present: bool

    @property
    def ok(self) -> bool:
        return True


def _corrupted() -> None:
    raise ArtifactCorruptedError("corrupted_artifact", "published artifact failed exact verification") from None


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _same_entry(parent: int, name: str, fd: int) -> bool:
    try:
        return _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == _identity(os.fstat(fd))
    except FileNotFoundError:
        return False


@dataclass
class _Root:
    path: Path
    fds: list[int]
    names: tuple[str, ...]

    @property
    def fd(self) -> int:
        return self.fds[-1]

    def check(self) -> None:
        for parent, name, child in zip(self.fds, self.names, self.fds[1:]):
            if not _same_entry(parent, name, child):
                raise ArtifactStoreError("root_changed", "store ancestry changed during staging")


def _open_root(stack: ExitStack, store_root: str | os.PathLike) -> _Root:
    try:
        supplied = os.fspath(store_root)
    except TypeError:
        raise ArtifactStoreError("invalid_store_root", "store_root must be an absolute directory path") from None
    if not isinstance(supplied, str) or "\x00" in supplied:
        raise ArtifactStoreError("invalid_store_root", "store_root must be an absolute directory path")
    path = Path(supplied)
    if not path.is_absolute() or ".." in path.parts:
        raise ArtifactStoreError("invalid_store_root", "store_root must be an absolute directory path")
    fds = [os.open("/", _DIR_FLAGS)]
    stack.callback(os.close, fds[0])
    names = path.parts[1:]
    for name in names:
        try:
            child = os.open(name, _DIR_FLAGS, dir_fd=fds[-1])
        except OSError:
            raise ArtifactStoreError("invalid_store_root", "store ancestor is missing, inaccessible, or not a real directory") from None
        stack.callback(os.close, child)
        fds.append(child)
    root = _Root(path, fds, names)
    root.check()
    return root


def _zip_reader(data: bytes) -> zipfile.ZipFile:
    # Inspection already verified the EOCD. Strip its comment only so an EOCD
    # signature inside that comment cannot confuse zipfile's own reverse scan.
    pos = data.rfind(b"PK\x05\x06")
    while pos >= 0:
        if pos + 22 <= len(data) and pos + 22 + struct.unpack_from("<H", data, pos + 20)[0] == len(data):
            return zipfile.ZipFile(io.BytesIO(data[:pos + 20] + b"\x00\x00"))
        pos = data.rfind(b"PK\x05\x06", 0, pos)
    raise ArtifactStoreError("invalid_archive", "validated ZIP end record unavailable")


def _expected_tree(inspection) -> tuple[dict[tuple[str, ...], dict[str, bool]], list[str]]:
    children: dict[tuple[str, ...], dict[str, bool]] = {(): {}}
    files = []
    for entry in inspection.entries:
        parts = tuple(entry.name.rstrip("/").split("/"))
        if len(parts) > _MAX_PATH_COMPONENTS:
            raise ArtifactStoreError("tree_limit", "artifact path exceeds component limit")
        for index, name in enumerate(parts):
            parent = parts[:index]
            directory = index < len(parts) - 1 or entry.is_dir
            children.setdefault(parent, {})[name] = directory
            if directory:
                children.setdefault(parts[:index + 1], {})
            if len(children) + len(files) > _MAX_TREE_NODES:
                raise ArtifactStoreError("tree_limit", "artifact tree exceeds node limit")
        if not entry.is_dir:
            files.append(entry.name)
        if len(children) + len(files) > _MAX_TREE_NODES:
            raise ArtifactStoreError("tree_limit", "artifact tree exceeds node limit")
    return children, files


def _read_exact(fd: int, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = os.read(fd, length - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def _verify_tree(directory: int, children, archive: zipfile.ZipFile) -> None:
    """Verify complete membership, modes, no links, and exact archive bytes."""
    with ExitStack() as stack:
        opened = {(): directory}
        for parts in sorted(children, key=len):
            fd = opened[parts]
            if stat.S_IMODE(os.fstat(fd).st_mode) != _DIR_MODE:
                _corrupted()
            if set(os.listdir(fd)) != set(children[parts]):
                _corrupted()
            for name, is_dir in children[parts].items():
                try:
                    child = os.open(name, _DIR_FLAGS if is_dir else _READ_FLAGS, dir_fd=fd)
                except OSError:
                    _corrupted()
                before = os.fstat(child)
                if is_dir:
                    stack.callback(os.close, child)
                    if not _same_entry(fd, name, child):
                        _corrupted()
                    opened[parts + (name,)] = child
                    continue
                try:
                    if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != _FILE_MODE or before.st_nlink != 1:
                        _corrupted()
                    info = archive.getinfo("/".join(parts + (name,)))
                    if before.st_size != info.file_size:
                        _corrupted()
                    with archive.open(info) as expected:
                        while chunk := expected.read(_CHUNK):
                            if _read_exact(child, len(chunk)) != chunk:
                                _corrupted()
                    if os.read(child, 1):
                        _corrupted()
                    after = os.fstat(child)
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        _corrupted()
                    if not _same_entry(fd, name, child):
                        _corrupted()
                finally:
                    os.close(child)
        # Check directory identities and membership again after reading files.
        for parts, fd in opened.items():
            if set(os.listdir(fd)) != set(children[parts]):
                _corrupted()
            if parts and not _same_entry(opened[parts[:-1]], parts[-1], fd):
                _corrupted()


def _verify_existing(root: _Root, name: str, children, archive: zipfile.ZipFile) -> bool:
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=root.fd)
    except FileNotFoundError:
        return False
    except OSError:
        _corrupted()
    try:
        if not _same_entry(root.fd, name, fd):
            _corrupted()
        _verify_tree(fd, children, archive)
        root.check()
        if not _same_entry(root.fd, name, fd):
            _corrupted()
    finally:
        os.close(fd)
    return True


def _extract(stack: ExitStack, root: _Root, directories, created_files, children, files,
             archive: zipfile.ZipFile) -> None:
    for parts in sorted(children, key=len):
        if not parts:
            continue
        root.check()
        parent = directories[parts[:-1]]
        child = _create_directory(parent, parts[-1])
        stack.callback(os.close, child)
        directories[parts] = child
        os.fchmod(child, _DIR_MODE)
    for filename in files:
        root.check()
        parts = tuple(filename.split("/"))
        parent = directories[parts[:-1]]
        fd = os.open(parts[-1], _WRITE_FLAGS, _FILE_MODE, dir_fd=parent)
        try:
            created_files[parts] = _identity(os.fstat(fd))
            with archive.open(filename) as stream:
                while chunk := stream.read(_CHUNK):
                    offset = 0
                    while offset < len(chunk):
                        count = os.write(fd, chunk[offset:])
                        if count <= 0:
                            raise ArtifactStoreError("write_failed", "artifact write made no progress")
                        offset += count
            os.fchmod(fd, _FILE_MODE)
            os.fsync(fd)
        finally:
            os.close(fd)
    for parts in sorted(directories, key=len, reverse=True):
        os.fsync(directories[parts])


def _remove_owned(parent: int, name: str, directories, created_files) -> None:
    """Remove only recorded owned objects; never recurse through foreign entries."""
    if not _same_entry(parent, name, directories[()]):
        return
    for parts, identity in created_files.items():
        fd = directories[parts[:-1]]
        try:
            observed = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if _identity(observed) == identity:
            os.unlink(parts[-1], dir_fd=fd)
    for parts in sorted(directories, key=len, reverse=True):
        parent_fd = directories[parts[:-1]] if parts else parent
        entry_name = parts[-1] if parts else name
        if _same_entry(parent_fd, entry_name, directories[parts]):
            # Unexpected entries make rmdir fail rather than deleting data
            # that this call did not create.
            os.rmdir(entry_name, dir_fd=parent_fd)


def _create_directory(parent: int, name: str) -> int:
    os.mkdir(name, _DIR_MODE, dir_fd=parent)
    # mkdir returns no identity. If opening or identifying the new directory
    # fails, leave its name alone: it may already name somebody else's object.
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    identity = None
    try:
        identity = _identity(os.fstat(fd))
        observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _identity(observed) != identity:
            raise ArtifactStoreError("staging_changed", "staging directory identity changed")
    except BaseException:
        try:
            if identity is not None:
                observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if _identity(observed) == identity:
                    os.rmdir(name, dir_fd=parent)
        except OSError:
            # Failure to prove identity or remove an empty directory is not
            # permission to delete unknown contents or a replacement.
            pass
        finally:
            os.close(fd)
        raise
    return fd


def stage_archive(archive_bytes: bytes | bytearray | memoryview, *,
                  store_root: str | os.PathLike, limits: ArchiveLimits | None = None,
                  publisher: DirectoryPublisher | None = None) -> StageResult:
    """Inspect first; extract through pinned directory fds; publish exclusively.

    The optional publisher is a trusted injected atomic no-replace adapter.
    The default implementation supports macOS, without an unsafe fallback.
    """
    if not isinstance(archive_bytes, (bytes, bytearray, memoryview)):
        raise ArtifactStoreError("not_bytes", "archive_bytes must be bytes-like")
    effective = limits if limits is not None else ArchiveLimits()
    # Bound mutable input before copying, then inspect the immutable copy that
    # is used for hashing, extraction, and exact reuse verification.
    size = archive_bytes.nbytes if isinstance(archive_bytes, memoryview) else len(archive_bytes)
    if size > effective.archive_bytes:
        inspect_archive(archive_bytes, limits=effective)
    data = bytes(archive_bytes)
    inspection = inspect_archive(data, limits=effective)
    artifact_id = hashlib.sha256(data).hexdigest()
    children, files = _expected_tree(inspection)
    publish = publisher if publisher is not None else publish_directory_exclusive
    try:
        with ExitStack() as stack, _zip_reader(data) as archive:
            root = _open_root(stack, store_root)
            def result(present: bool) -> StageResult:
                return StageResult(artifact_id, str(root.path / artifact_id), inspection.total_entries,
                                   inspection.total_uncompressed_size, present)
            if _verify_existing(root, artifact_id, children, archive):
                return result(True)
            staging = f".staging-{artifact_id[:16]}-{secrets.token_hex(16)}"
            root.check()
            stage = _create_directory(root.fd, staging)
            stack.callback(os.close, stage)
            directories = {(): stage}
            created_files = {}
            try:
                os.fchmod(stage, _DIR_MODE)
                _extract(stack, root, directories, created_files, children, files, archive)
                _verify_tree(stage, children, archive)
                root.check()
                if not _same_entry(root.fd, staging, stage):
                    raise ArtifactStoreError("staging_changed", "staging directory identity changed")
                try:
                    publish(root.fd, staging, artifact_id)
                except FileExistsError:
                    if not _verify_existing(root, artifact_id, children, archive):
                        raise ArtifactStoreError("publish_failed", "competing artifact disappeared")
                    return result(True)
                root.check()
                if not _same_entry(root.fd, artifact_id, stage):
                    _corrupted()
                _verify_tree(stage, children, archive)
                os.fsync(root.fd)
                root.check()
                return result(False)
            finally:
                _remove_owned(root.fd, staging, directories, created_files)
    except ArtifactStoreError:
        raise
    except NotImplementedError:
        raise ArtifactStoreError("unsupported_platform", "atomic directory publication unavailable") from None
    except (OSError, zipfile.BadZipFile, ValueError):
        raise ArtifactStoreError("stage_failed", "artifact staging failed") from None
