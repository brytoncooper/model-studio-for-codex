"""Bounded deterministic project-tree-to-ZIP packing for B23."""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from model_deck.plugins.archive_inspection import (
    DEFAULT_ARCHIVE_BYTES,
    DEFAULT_ENTRY_COUNT,
    DEFAULT_TOTAL_UNCOMPRESSED_BYTES,
    ArchiveErrorCode,
    ArchiveInspectionError,
)
from .errors import AuthoringError, AuthoringErrorCode
from .validation import (
    _inspect_manifest_for_authoring,
    entrypoint_present,
    validate_project_archive,
)

_MANIFEST_ENTRY_NAME = "manifest.json"
_DETERMINISTIC_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_DETERMINISTIC_MODE = (0o644 & 0xFFFF) << 16
_READ_CHUNK_BYTES = 64 * 1024
_PYCACHE_DIRECTORY_NAME = "__pycache__"
_PYCACHE_FILE_SUFFIX = ".pyc"


def _should_skip_cache_entry(entry_name: str) -> bool:
    """Return ``True`` for Python bytecode cache artifacts.

    The packer must ignore both the ``__pycache__`` directory at any
    depth and any loose ``.pyc`` file. Both are build-time artifacts
    that do not belong in a published plugin archive; including them
    would also break archive determinism across Python versions.
    """
    if entry_name == _PYCACHE_DIRECTORY_NAME:
        return True
    return entry_name.endswith(_PYCACHE_FILE_SUFFIX)


@dataclass(frozen=True)
class PackLimits:
    """Bound traversal, decoded input bytes, and finished archive bytes."""

    file_count: int = DEFAULT_ENTRY_COUNT
    total_bytes: int = DEFAULT_TOTAL_UNCOMPRESSED_BYTES
    archive_bytes: int = DEFAULT_ARCHIVE_BYTES


@dataclass(frozen=True)
class PackResult:
    """Detached result for one successfully published archive."""

    output_path: Path
    sha256: str
    entry_count: int
    total_bytes: int


@dataclass(frozen=True)
class _ProjectFile:
    archive_name: str
    payload: bytes


@dataclass(frozen=True)
class _DirectoryToScan:
    path: Path
    relative_parts: tuple[str, ...]
    metadata: os.stat_result


def _input_error(code: str, detail: str, field: str | None = None) -> AuthoringError:
    return AuthoringError(code=code, detail=detail, field=field)


def _validate_limits(limits: PackLimits) -> None:
    for field_name, value in (
        ("file_count", limits.file_count),
        ("total_bytes", limits.total_bytes),
        ("archive_bytes", limits.archive_bytes),
    ):
        if type(value) is not int or value < 1:
            raise _input_error(
                AuthoringErrorCode.INPUT_OUT_OF_BUDGET,
                f"{field_name} must be a positive integer",
            )


def _same_file_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_mode == second.st_mode
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _open_directory(directory: _DirectoryToScan) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(directory.path, flags)
        opened_metadata = os.fstat(descriptor)
    except OSError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise _input_error(
            AuthoringErrorCode.INPUT_NOT_READABLE,
            "project directory could not be opened safely",
            "/".join(directory.relative_parts) or None,
        ) from None
    if not stat.S_ISDIR(opened_metadata.st_mode) or not _same_file_snapshot(
        directory.metadata, opened_metadata
    ):
        os.close(descriptor)
        raise _input_error(
            AuthoringErrorCode.INPUT_SYMLINK_OR_SPECIAL,
            "project directory changed during traversal",
            "/".join(directory.relative_parts) or None,
        )
    return descriptor


def _read_regular_file(
    directory_descriptor: int,
    entry_name: str,
    archive_name: str,
    expected_metadata: os.stat_result,
    byte_budget: int,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(entry_name, flags, dir_fd=directory_descriptor)
    except OSError:
        raise _input_error(
            AuthoringErrorCode.INPUT_NOT_READABLE,
            "project file could not be opened safely",
            archive_name,
        ) from None
    try:
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode) or not _same_file_snapshot(
            expected_metadata, opened_metadata
        ):
            raise _input_error(
                AuthoringErrorCode.INPUT_SYMLINK_OR_SPECIAL,
                "project file changed before it could be read",
                archive_name,
            )
        if opened_metadata.st_size > byte_budget:
            raise _input_error(
                AuthoringErrorCode.INPUT_OUT_OF_BUDGET,
                "project exceeds the decoded input byte budget",
                archive_name,
            )

        chunks: list[bytes] = []
        bytes_read = 0
        while True:
            read_size = min(_READ_CHUNK_BYTES, byte_budget + 1 - bytes_read)
            chunk = os.read(descriptor, read_size)
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)
            if bytes_read > byte_budget:
                raise _input_error(
                    AuthoringErrorCode.INPUT_OUT_OF_BUDGET,
                    "project exceeds the decoded input byte budget",
                    archive_name,
                )

        finished_metadata = os.fstat(descriptor)
        if not _same_file_snapshot(opened_metadata, finished_metadata):
            raise _input_error(
                AuthoringErrorCode.INPUT_NOT_READABLE,
                "project file changed while it was read",
                archive_name,
            )
        payload = b"".join(chunks)
        if len(payload) != finished_metadata.st_size:
            raise _input_error(
                AuthoringErrorCode.INPUT_NOT_READABLE,
                "project file size changed while it was read",
                archive_name,
            )
        return payload
    except OSError:
        raise _input_error(
            AuthoringErrorCode.INPUT_NOT_READABLE,
            "project file could not be read",
            archive_name,
        ) from None
    finally:
        os.close(descriptor)


def _read_project_files(
    project_root: Path,
    root_metadata: os.stat_result,
    limits: PackLimits,
) -> tuple[_ProjectFile, ...]:
    pending_directories = [_DirectoryToScan(project_root, (), root_metadata)]
    directory_count = 0
    total_bytes = 0
    files: list[_ProjectFile] = []

    while pending_directories:
        directory = pending_directories.pop()
        directory_descriptor = _open_directory(directory)
        try:
            try:
                entries = os.scandir(directory_descriptor)
            except OSError:
                raise _input_error(
                    AuthoringErrorCode.INPUT_NOT_READABLE,
                    "project directory could not be listed",
                    "/".join(directory.relative_parts) or None,
                ) from None
            with entries:
                for entry in entries:
                    if _should_skip_cache_entry(entry.name):
                        continue
                    relative_parts = (*directory.relative_parts, entry.name)
                    archive_name = "/".join(relative_parts)
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError:
                        raise _input_error(
                            AuthoringErrorCode.INPUT_NOT_READABLE,
                            "project entry could not be inspected",
                            archive_name,
                        ) from None
                    if stat.S_ISDIR(metadata.st_mode):
                        directory_count += 1
                        if directory_count > limits.file_count:
                            raise _input_error(
                                AuthoringErrorCode.INPUT_OUT_OF_BUDGET,
                                "project traversal exceeds the directory budget",
                            )
                        pending_directories.append(
                            _DirectoryToScan(
                                path=directory.path / entry.name,
                                relative_parts=relative_parts,
                                metadata=metadata,
                            )
                        )
                        continue
                    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(
                        metadata.st_mode
                    ):
                        raise _input_error(
                            AuthoringErrorCode.INPUT_SYMLINK_OR_SPECIAL,
                            "project contains a symlink or special file",
                            archive_name,
                        )
                    if len(files) >= limits.file_count:
                        raise _input_error(
                            AuthoringErrorCode.INPUT_OUT_OF_BUDGET,
                            "project exceeds the file-count budget",
                        )
                    payload = _read_regular_file(
                        directory_descriptor,
                        entry.name,
                        archive_name,
                        metadata,
                        limits.total_bytes - total_bytes,
                    )
                    total_bytes += len(payload)
                    files.append(_ProjectFile(archive_name, payload))
        finally:
            os.close(directory_descriptor)

    files.sort(key=lambda project_file: project_file.archive_name)
    return tuple(files)


def _decode_manifest(files: tuple[_ProjectFile, ...]) -> Mapping[str, Any]:
    manifest_payload = next(
        (item.payload for item in files if item.archive_name == _MANIFEST_ENTRY_NAME),
        None,
    )
    if manifest_payload is None:
        raise _input_error(
            AuthoringErrorCode.MISSING_MANIFEST,
            "project root does not contain manifest.json",
            _MANIFEST_ENTRY_NAME,
        )
    try:
        decoded = json.loads(manifest_payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _input_error(
            AuthoringErrorCode.MANIFEST_READ_FAILED,
            "manifest.json is not valid UTF-8 JSON",
            _MANIFEST_ENTRY_NAME,
        ) from None
    if not isinstance(decoded, Mapping):
        raise _input_error(
            AuthoringErrorCode.MANIFEST_READ_FAILED,
            "manifest.json must decode to a JSON object",
            _MANIFEST_ENTRY_NAME,
        )
    return decoded


def _validate_manifest(
    decoded: Mapping[str, Any],
    *,
    caller_plugin_api_major: int,
    caller_plugin_api_minor: int,
) -> str:
    manifest = _inspect_manifest_for_authoring(
        decoded,
        caller_plugin_api_major=caller_plugin_api_major,
        caller_plugin_api_minor=caller_plugin_api_minor,
    )
    if not manifest.ok:
        first = manifest.errors[0]
        raise _input_error(
            AuthoringErrorCode.MANIFEST_READ_FAILED,
            f"manifest failed inspection: {first.code} ({first.field})",
            first.field or _MANIFEST_ENTRY_NAME,
        )
    return manifest.entrypoint.path


def _write_archive_entry(
    archive_file: zipfile.ZipFile,
    archive_name: str,
    payload: bytes,
) -> None:
    info = zipfile.ZipInfo(archive_name)
    info.date_time = _DETERMINISTIC_TIMESTAMP
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = _DETERMINISTIC_MODE
    archive_file.writestr(info, payload, zipfile.ZIP_DEFLATED, 6)


def _build_archive_bytes(
    files: tuple[_ProjectFile, ...],
    decoded_manifest: Mapping[str, Any],
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        normalized_manifest = json.dumps(
            dict(decoded_manifest),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        _write_archive_entry(archive, _MANIFEST_ENTRY_NAME, normalized_manifest)
        for project_file in files:
            if project_file.archive_name != _MANIFEST_ENTRY_NAME:
                _write_archive_entry(
                    archive, project_file.archive_name, project_file.payload
                )
    return buffer.getvalue()


_ARCHIVE_BUDGET_ERRORS = frozenset(
    {
        ArchiveErrorCode.ARCHIVE_TOO_LARGE,
        ArchiveErrorCode.ENTRY_TOO_LARGE,
        ArchiveErrorCode.TOO_MANY_ENTRIES,
        ArchiveErrorCode.TOTAL_UNCOMPRESSED_TOO_LARGE,
        ArchiveErrorCode.COMPRESSION_RATIO_EXCEEDED,
    }
)


def _validate_finished_archive(
    archive_bytes: bytes,
    *,
    caller_plugin_api_major: int,
    caller_plugin_api_minor: int,
) -> None:
    try:
        report = validate_project_archive(
            archive_bytes,
            caller_plugin_api_major=caller_plugin_api_major,
            caller_plugin_api_minor=caller_plugin_api_minor,
        )
    except ArchiveInspectionError as failure:
        code = (
            AuthoringErrorCode.INPUT_OUT_OF_BUDGET
            if failure.code in _ARCHIVE_BUDGET_ERRORS
            else AuthoringErrorCode.INPUT_SYMLINK_OR_SPECIAL
        )
        raise _input_error(
            code,
            f"finished archive failed inspection: {failure.code}",
            failure.entry_name,
        ) from None
    if not report.manifest.ok:
        first = report.manifest.errors[0]
        raise _input_error(
            AuthoringErrorCode.MANIFEST_READ_FAILED,
            f"finished manifest failed inspection: {first.code}",
            first.field,
        )
    if not entrypoint_present(report):
        raise _input_error(
            AuthoringErrorCode.ENTRYPOINT_FILE_MISSING,
            "finished archive does not contain its declared entrypoint",
            report.manifest.entrypoint.path,
        )


def _check_output_safety(project_root: Path, output_path: Path) -> None:
    if not output_path.is_absolute():
        raise _input_error(
            AuthoringErrorCode.OUTPUT_NOT_ABSOLUTE,
            "output path must be absolute",
        )
    if output_path.exists() or output_path.is_symlink():
        raise _input_error(
            AuthoringErrorCode.OUTPUT_EXISTS,
            "output path already exists",
        )
    try:
        output_path.resolve(strict=False).relative_to(project_root)
    except ValueError:
        return
    except OSError:
        raise _input_error(
            AuthoringErrorCode.OUTPUT_UNWRITABLE,
            "output path could not be resolved",
        ) from None
    raise _input_error(
        AuthoringErrorCode.OUTPUT_NESTED_IN_INPUT,
        "output path is nested under the project root",
    )


def _publish_without_overwrite(output_path: Path, archive_bytes: bytes) -> None:
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise _input_error(
            AuthoringErrorCode.OUTPUT_UNWRITABLE,
            "output directory could not be created",
        ) from None

    try:
        temp_descriptor, temp_name = tempfile.mkstemp(
            prefix=".plugin-pack-",
            suffix=".zip.tmp",
            dir=str(output_path.parent),
        )
    except OSError:
        raise _input_error(
            AuthoringErrorCode.OUTPUT_UNWRITABLE,
            "temporary archive could not be created",
        ) from None

    descriptor_is_open = True
    try:
        try:
            temp_file = os.fdopen(temp_descriptor, "wb")
            descriptor_is_open = False
            with temp_file:
                written = temp_file.write(archive_bytes)
                if written != len(archive_bytes):
                    raise OSError("short archive write")
                temp_file.flush()
                os.fsync(temp_file.fileno())
        except OSError:
            raise _input_error(
                AuthoringErrorCode.OUTPUT_UNWRITABLE,
                "temporary archive could not be written",
            ) from None

        try:
            os.link(temp_name, output_path)
        except FileExistsError:
            raise _input_error(
                AuthoringErrorCode.OUTPUT_EXISTS,
                "output path already exists",
            ) from None
        except OSError:
            raise _input_error(
                AuthoringErrorCode.OUTPUT_UNWRITABLE,
                "archive could not be published",
            ) from None
    finally:
        if descriptor_is_open:
            try:
                os.close(temp_descriptor)
            except OSError:
                pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def pack_project_archive(
    project_root: Path,
    *,
    output_path: Path,
    limits: PackLimits | None = None,
    caller_plugin_api_major: int = 1,
    caller_plugin_api_minor: int = 0,
) -> PackResult:
    """Validate, pack, revalidate, and atomically publish one project archive."""
    if not isinstance(project_root, Path) or not project_root.is_absolute():
        raise _input_error(
            AuthoringErrorCode.INPUT_NOT_READABLE,
            "project root must be an absolute Path",
        )
    if not isinstance(output_path, Path):
        raise _input_error(
            AuthoringErrorCode.OUTPUT_NOT_ABSOLUTE,
            "output path must be an absolute Path",
        )
    resolved_limits = limits if limits is not None else PackLimits()
    _validate_limits(resolved_limits)

    try:
        root_metadata = project_root.lstat()
    except FileNotFoundError:
        raise _input_error(
            AuthoringErrorCode.INPUT_NOT_A_DIRECTORY,
            "project root does not exist",
        ) from None
    except OSError:
        raise _input_error(
            AuthoringErrorCode.INPUT_NOT_READABLE,
            "project root could not be inspected",
        ) from None
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise _input_error(
            AuthoringErrorCode.INPUT_NOT_A_DIRECTORY,
            "project root must be a real directory",
        )
    try:
        project_resolved = project_root.resolve(strict=True)
    except OSError:
        raise _input_error(
            AuthoringErrorCode.INPUT_NOT_READABLE,
            "project root could not be resolved",
        ) from None

    _check_output_safety(project_resolved, output_path)
    files = _read_project_files(project_resolved, root_metadata, resolved_limits)
    decoded_manifest = _decode_manifest(files)
    entrypoint_path = _validate_manifest(
        decoded_manifest,
        caller_plugin_api_major=caller_plugin_api_major,
        caller_plugin_api_minor=caller_plugin_api_minor,
    )
    names = {project_file.archive_name for project_file in files}
    if entrypoint_path not in names:
        raise _input_error(
            AuthoringErrorCode.ENTRYPOINT_FILE_MISSING,
            "manifest entrypoint is not present in the project tree",
            entrypoint_path,
        )

    archive_bytes = _build_archive_bytes(files, decoded_manifest)
    if len(archive_bytes) > resolved_limits.archive_bytes:
        raise _input_error(
            AuthoringErrorCode.INPUT_OUT_OF_BUDGET,
            "packed archive exceeds the archive byte budget",
        )
    _validate_finished_archive(
        archive_bytes,
        caller_plugin_api_major=caller_plugin_api_major,
        caller_plugin_api_minor=caller_plugin_api_minor,
    )
    _publish_without_overwrite(output_path, archive_bytes)

    return PackResult(
        output_path=output_path,
        sha256=hashlib.sha256(archive_bytes).hexdigest(),
        entry_count=len(files),
        total_bytes=sum(len(project_file.payload) for project_file in files),
    )
