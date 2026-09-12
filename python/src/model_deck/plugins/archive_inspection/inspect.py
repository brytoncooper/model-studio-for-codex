"""Bounded, in-memory ZIP inspection with exact metadata and stream validation."""
from __future__ import annotations

import io
import stat
import struct
import zipfile
import zlib
from dataclasses import dataclass
from typing import Final


# ---------------------------------------------------------------------------
# Public limits and their defaults
# ---------------------------------------------------------------------------

DEFAULT_ARCHIVE_BYTES: Final[int] = 64 * 1024 * 1024
"""Maximum size in bytes for the supplied archive byte string."""

DEFAULT_TOTAL_UNCOMPRESSED_BYTES: Final[int] = 256 * 1024 * 1024
"""Maximum cumulative uncompressed bytes across all regular entries."""

DEFAULT_ENTRY_UNCOMPRESSED_BYTES: Final[int] = 64 * 1024 * 1024
"""Maximum uncompressed bytes for a single regular entry."""

DEFAULT_ENTRY_COUNT: Final[int] = 4096
"""Maximum number of central-directory entries to accept."""

DEFAULT_COMPRESSION_RATIO: Final[int] = 1000
"""Maximum allowed ``uncompressed / max(compressed, 1)`` integer ratio."""

_STREAM_CHUNK_BYTES: Final[int] = 64 * 1024
"""Streaming chunk size used when validating CRC and length."""

_DATA_DESCRIPTOR_SIG: Final[bytes] = b"PK\x07\x08"
"""Optional signature prefix on a ZIP data-descriptor record."""

_ALLOWED_COMPRESS_TYPES: Final[tuple[int, ...]] = (
    zipfile.ZIP_STORED,
    zipfile.ZIP_DEFLATED,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ArchiveErrorCode:
    """Stable string codes returned on :class:`ArchiveInspectionError`.

    Each code is a stable identifier; callers branch on these strings.
    """

    ARCHIVE_TOO_LARGE = "archive_too_large"
    NOT_A_ZIP = "not_a_zip"
    UNSUPPORTED_COMPRESSION = "unsupported_compression"
    ENCRYPTED_ENTRY = "encrypted_entry"
    SYMLINK_OR_SPECIAL_ENTRY = "symlink_or_special_entry"
    INVALID_PATH = "invalid_path"
    DUPLICATE_PATH = "duplicate_path"
    FILE_PARENT_COLLISION = "file_parent_collision"
    ENTRY_TOO_LARGE = "entry_too_large"
    TOO_MANY_ENTRIES = "too_many_entries"
    TOTAL_UNCOMPRESSED_TOO_LARGE = "total_uncompressed_too_large"
    COMPRESSION_RATIO_EXCEEDED = "compression_ratio_exceeded"
    CRC_MISMATCH = "crc_mismatch"
    LENGTH_MISMATCH = "length_mismatch"


@dataclass(frozen=True)
class ArchiveInspectionError(Exception):
    """Raised when an archive fails inspection.

    Attributes:
        code: Stable error code from :class:`ArchiveErrorCode`.
        detail: Human-readable detail; safe for logs, not for user copy.
        entry_name: Optional normalised entry name, when the defect is
            tied to a specific entry.
    """

    code: str
    detail: str
    entry_name: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.entry_name is None:
            return f"{self.code}: {self.detail}"
        return f"{self.code} ({self.entry_name}): {self.detail}"


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveLimits:
    """Caller-overridable inspection limits.

    Every field must be a strict positive integer. ``bool`` is rejected
    even though ``bool`` is a subclass of ``int`` in Python.
    """

    archive_bytes: int = DEFAULT_ARCHIVE_BYTES
    total_uncompressed_bytes: int = DEFAULT_TOTAL_UNCOMPRESSED_BYTES
    entry_uncompressed_bytes: int = DEFAULT_ENTRY_UNCOMPRESSED_BYTES
    entry_count: int = DEFAULT_ENTRY_COUNT
    compression_ratio: int = DEFAULT_COMPRESSION_RATIO

    def __post_init__(self) -> None:
        for attr, value in (
            ("archive_bytes", self.archive_bytes),
            ("total_uncompressed_bytes", self.total_uncompressed_bytes),
            ("entry_uncompressed_bytes", self.entry_uncompressed_bytes),
            ("entry_count", self.entry_count),
            ("compression_ratio", self.compression_ratio),
        ):
            # bool is a subclass of int in Python; reject it explicitly
            # so a caller cannot accidentally pass True/False for any field.
            if isinstance(value, bool):
                raise ValueError(
                    f"{attr} must be a strict positive int, got bool"
                )
            if not isinstance(value, int):
                raise ValueError(
                    f"{attr} must be a strict positive int, got "
                    f"{type(value).__name__}"
                )
            if value <= 0:
                raise ValueError(
                    f"{attr} must be a strict positive int, got {value}"
                )


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveEntry:
    """A single inspected archive entry.

    ``name`` is the normalised POSIX-style relative path with forward
    slashes. ``size`` and ``compressed_size`` are the validated metadata
    values for the entry after streaming verification (regular entries
    carry the metadata ``file_size`` and ``compress_size``; empty directory
    entries may have a nonzero DEFLATE compressed size). ``is_dir``
    is true when the entry's name ends with ``/`` and the inspector
    detected the entry as a directory.
    """

    name: str
    size: int
    compressed_size: int
    is_dir: bool


@dataclass(frozen=True)
class ArchiveInspectionResult:
    """Detached, immutable inspection outcome.

    ``entries`` is the tuple of entries in central-directory order. The
    result exposes only values that survived inspection; it holds no
    reference to the input bytes. ``ok`` is true for any archive that
    passed every check.
    """

    entries: tuple[ArchiveEntry, ...]
    total_entries: int
    total_uncompressed_size: int

    @property
    def ok(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------


def _normalise_entry_name(raw: str) -> str:
    """Return the entry name on success; raise otherwise.

    ``raw`` must be the decoded ``orig_filename``: passing the
    NUL-truncated ``filename`` would silently drop everything after the
    first NUL byte.

    Rules:

    - Non-empty string.
    - No NUL bytes.
    - No backslashes (POSIX-only paths).
    - No leading ``/``.
    - No drive prefix ``X:``.
    - Each ``/``-separated segment must be non-empty and not ``.`` or
      ``..``.
    """
    if not isinstance(raw, str):
        raise ArchiveInspectionError(
            code=ArchiveErrorCode.INVALID_PATH,
            detail="entry name is not a string",
        )
    if raw == "":
        raise ArchiveInspectionError(
            code=ArchiveErrorCode.INVALID_PATH,
            detail="entry name is empty",
            entry_name="",
        )
    if "\x00" in raw:
        raise ArchiveInspectionError(
            code=ArchiveErrorCode.INVALID_PATH,
            detail="entry name contains NUL",
            entry_name=raw,
        )
    if "\\" in raw:
        raise ArchiveInspectionError(
            code=ArchiveErrorCode.INVALID_PATH,
            detail="entry name contains a backslash",
            entry_name=raw,
        )
    if raw.startswith("/"):
        raise ArchiveInspectionError(
            code=ArchiveErrorCode.INVALID_PATH,
            detail="entry name is absolute",
            entry_name=raw,
        )
    # Reject drive prefix (C:); UNC is already covered by the backslash
    # check above. The drive prefix uses ``:`` as the second character.
    if len(raw) >= 2 and raw[1] == ":":
        raise ArchiveInspectionError(
            code=ArchiveErrorCode.INVALID_PATH,
            detail="entry name has a drive prefix",
            entry_name=raw,
        )
    # Strip a single trailing slash before segment checks so directory
    # entries like ``plugin/`` validate against their non-slash segments
    # without raising "empty segment".
    scan = raw[:-1] if raw.endswith("/") else raw
    for segment in scan.split("/"):
        if segment == "":
            raise ArchiveInspectionError(
                code=ArchiveErrorCode.INVALID_PATH,
                detail="entry name has an empty segment",
                entry_name=raw,
            )
        if segment == ".":
            raise ArchiveInspectionError(
                code=ArchiveErrorCode.INVALID_PATH,
                detail="entry name references the current directory",
                entry_name=raw,
            )
        if segment == "..":
            raise ArchiveInspectionError(
                code=ArchiveErrorCode.INVALID_PATH,
                detail="entry name contains parent traversal",
                entry_name=raw,
            )
    return raw


# ---------------------------------------------------------------------------
# Collision and unix-mode helpers
# ---------------------------------------------------------------------------


def _identity(name: str) -> str:
    """Return the casefolded identity used for collision detection.

    A single trailing ``/`` is stripped so that ``foo`` and ``foo/``
    share an identity. Case is folded so that ``Foo`` and ``foo``
    collide.
    """
    return name.casefold().rstrip("/")


def _check_collision(
    candidate: str,
    candidate_kind: str,
    accepted: list[tuple[str, str]],
) -> None:
    """Raise on duplicate path or file-parent collision involving ``candidate``.

    The rule covers all three collision modes:

    - same identity, same kind (``DUPLICATE_PATH``),
    - same identity, different kind (``FILE_PARENT_COLLISION``),
    - strict-prefix descent in either direction, where the deeper
      side is any candidate kind whose ancestor is a file
      (``FILE_PARENT_COLLISION``). A directory at ``a/`` does **not**
      block descendants like ``a/file.txt``; a file at ``a`` does
      block every entry at ``a/...``.
    """
    cand_id = _identity(candidate)
    for prev_name, prev_kind in accepted:
        prev_id = _identity(prev_name)
        if prev_id == cand_id:
            if prev_kind == candidate_kind:
                raise ArchiveInspectionError(
                    code=ArchiveErrorCode.DUPLICATE_PATH,
                    detail=f"duplicate entry name {candidate!r}",
                    entry_name=candidate,
                )
            raise ArchiveInspectionError(
                code=ArchiveErrorCode.FILE_PARENT_COLLISION,
                detail=(
                    f"entry {candidate!r} collides with previous "
                    f"{prev_name!r} (file/directory identity mismatch)"
                ),
                entry_name=candidate,
            )
        # Bidirectional prefix rule: if either side is a file, the
        # other side cannot be its descendant, because a file cannot
        # occupy the same path as a directory entry would need.
        if prev_kind == "file" and cand_id.startswith(prev_id + "/"):
            raise ArchiveInspectionError(
                code=ArchiveErrorCode.FILE_PARENT_COLLISION,
                detail=(
                    f"entry {candidate!r} descends from existing "
                    f"file {prev_name!r}"
                ),
                entry_name=candidate,
            )
        if (
            candidate_kind == "file"
            and prev_id != cand_id
            and prev_id.startswith(cand_id + "/")
        ):
            raise ArchiveInspectionError(
                code=ArchiveErrorCode.FILE_PARENT_COLLISION,
                detail=(
                    f"entry {prev_name!r} descends from new file "
                    f"{candidate!r}"
                ),
                entry_name=candidate,
            )


def _kind_from_mode(name: str, mode: int) -> tuple[bool, str]:
    """Explicit Unix file type and directory suffix must agree."""
    is_dir = name.endswith("/")
    file_type = stat.S_IFMT(mode)
    if file_type and ((file_type == stat.S_IFDIR) != is_dir):
        raise ArchiveInspectionError(
            code=ArchiveErrorCode.SYMLINK_OR_SPECIAL_ENTRY,
            detail="directory suffix and Unix file type disagree",
            entry_name=name,
        )
    return is_dir, "dir" if is_dir else "file"


def _is_symlink_or_special(name: str, mode: int) -> bool:
    # Permission-only metadata is conventional; no file type means infer
    # regular file or directory from the already-validated path suffix.
    return stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)


def _reject(code: str, detail: str) -> None:
    raise ArchiveInspectionError(code=code, detail=detail) from None


def _directory_bounds(data: bytes, limits: ArchiveLimits) -> tuple[int, int, int]:
    # An EOCD signature inside the comment is data, not another end record.
    start = max(0, len(data) - 65535 - 22)
    pos = data.rfind(b"PK\x05\x06", start)
    while pos >= start:
        if pos + 22 <= len(data):
            fields = struct.unpack_from("<4s4H2IH", data, pos)
            if pos + 22 + fields[7] == len(data):
                break
        pos = data.rfind(b"PK\x05\x06", start, pos)
    else:
        _reject(ArchiveErrorCode.NOT_A_ZIP, "missing or truncated ZIP end record")
    _, disk, directory_disk, disk_count, count, size, offset, _ = fields
    if disk or directory_disk or disk_count != count:
        _reject(ArchiveErrorCode.NOT_A_ZIP, "multi-disk archives are unsupported")
    if count == 65535 or size == 0xffffffff or offset == 0xffffffff:
        _reject(ArchiveErrorCode.UNSUPPORTED_COMPRESSION, "ZIP64 archives are unsupported")
    if count > limits.entry_count:
        _reject(ArchiveErrorCode.TOO_MANY_ENTRIES, "archive exceeds entry count limit")
    if offset + size != pos:
        _reject(ArchiveErrorCode.NOT_A_ZIP, "central directory extent disagrees with end record")
    return offset, pos, count


def _check_extra(extra: bytes) -> None:
    offset = 0
    while offset < len(extra):
        if offset + 4 > len(extra):
            _reject(ArchiveErrorCode.NOT_A_ZIP, "truncated ZIP extra field")
        kind, length = struct.unpack_from("<HH", extra, offset)
        offset += 4
        if offset + length > len(extra):
            _reject(ArchiveErrorCode.NOT_A_ZIP, "truncated ZIP extra field")
        if kind == 1:
            _reject(ArchiveErrorCode.UNSUPPORTED_COMPRESSION, "ZIP64 archives are unsupported")
        offset += length


def _central_names(data: bytes, start: int, end: int, count: int) -> list[bytes]:
    """Check the exact central-record extent before delegating metadata parsing."""
    names = []
    pos = start
    for _ in range(count):
        if pos + 46 > end or data[pos:pos + 4] != b"PK\x01\x02":
            _reject(ArchiveErrorCode.NOT_A_ZIP, "central directory record missing")
        name_length, extra_length, comment_length, disk = struct.unpack_from("<4H", data, pos + 28)
        next_pos = pos + 46 + name_length + extra_length + comment_length
        if next_pos > end or disk:
            _reject(ArchiveErrorCode.NOT_A_ZIP, "central directory record exceeds its extent")
        name_end = pos + 46 + name_length
        names.append(data[pos + 46:name_end])
        _check_extra(data[name_end:name_end + extra_length])
        pos = next_pos
    if pos != end:
        _reject(ArchiveErrorCode.NOT_A_ZIP, "central directory count disagrees with its extent")
    return names


def _check_info(info: zipfile.ZipInfo) -> tuple[str, bool]:
    name = _normalise_entry_name(info.orig_filename)
    if info.extract_version > 63 or info.create_version > 63 or info.create_system > 14:
        _reject(ArchiveErrorCode.UNSUPPORTED_COMPRESSION, "entry declares unsupported ZIP version")
    if info.flag_bits & 1:
        _reject(ArchiveErrorCode.ENCRYPTED_ENTRY, "encrypted entries are unsupported")
    # Bits 1/2 are DEFLATE options, 3 selects a descriptor, 11 selects UTF-8.
    if info.flag_bits & ~0x080e:
        _reject(ArchiveErrorCode.UNSUPPORTED_COMPRESSION, "unsupported ZIP entry flags")
    if info.compress_type not in _ALLOWED_COMPRESS_TYPES:
        _reject(ArchiveErrorCode.UNSUPPORTED_COMPRESSION, "unsupported compression method")
    if info.compress_type == zipfile.ZIP_DEFLATED and info.extract_version < 20:
        _reject(ArchiveErrorCode.UNSUPPORTED_COMPRESSION, "DEFLATE requires ZIP version 2.0")
    if info.compress_type == zipfile.ZIP_STORED and info.flag_bits & 6:
        _reject(ArchiveErrorCode.UNSUPPORTED_COMPRESSION, "DEFLATE flags on a stored entry")
    if info.file_size == 0xffffffff or info.compress_size == 0xffffffff:
        _reject(ArchiveErrorCode.UNSUPPORTED_COMPRESSION, "ZIP64 archives are unsupported")
    mode = info.external_attr >> 16
    is_dir, _ = _kind_from_mode(name, mode)
    if _is_symlink_or_special(name, mode):
        raise ArchiveInspectionError(
            code=ArchiveErrorCode.SYMLINK_OR_SPECIAL_ENTRY,
            detail="entry is not a regular file or directory",
            entry_name=name,
        )
    return name, is_dir


def _local_span(data: bytes, info: zipfile.ZipInfo, raw_name: bytes, boundary: int) -> tuple[int, int]:
    start = info.header_offset
    if start < 0 or start + 30 > boundary or data[start:start + 4] != b"PK\x03\x04":
        _reject(ArchiveErrorCode.NOT_A_ZIP, "local file header missing or overlapping")
    version, flags, method, _, _, crc, compressed, size, name_length, extra_length = struct.unpack_from(
        "<5H3I2H", data, start + 4
    )
    name_end = start + 30 + name_length
    payload_start = name_end + extra_length
    if payload_start > boundary:
        _reject(ArchiveErrorCode.LENGTH_MISMATCH, "local header exceeds its entry extent")
    if data[start + 30:name_end] != raw_name:
        _reject(ArchiveErrorCode.INVALID_PATH, "local and central entry names disagree")
    if version != info.extract_version or flags != info.flag_bits or method != info.compress_type:
        _reject(ArchiveErrorCode.UNSUPPORTED_COMPRESSION, "local and central entry format disagree")
    _check_extra(data[name_end:payload_start])
    if flags & 8:
        # Streaming writers may leave each local metadata field zero.
        if crc not in (0, info.CRC):
            _reject(ArchiveErrorCode.CRC_MISMATCH, "local and central CRC disagree")
        if compressed not in (0, info.compress_size) or size not in (0, info.file_size):
            _reject(ArchiveErrorCode.LENGTH_MISMATCH, "local and central entry sizes disagree")
    else:
        if compressed != info.compress_size or size != info.file_size:
            _reject(ArchiveErrorCode.LENGTH_MISMATCH, "local and central entry sizes disagree")
        if crc != info.CRC:
            _reject(ArchiveErrorCode.CRC_MISMATCH, "local and central CRC disagree")
    payload_end = payload_start + info.compress_size
    if payload_end > boundary:
        _reject(ArchiveErrorCode.LENGTH_MISMATCH, "compressed entry overlaps the next ZIP record")
    if flags & 8:
        descriptor = data[payload_end:boundary]
        # Extent distinguishes a 12-byte unsigned descriptor from a 16-byte
        # signed one, including the legal case where CRC equals the signature.
        if len(descriptor) == 16 and descriptor[:4] == _DATA_DESCRIPTOR_SIG:
            descriptor = descriptor[4:]
        if len(descriptor) != 12:
            _reject(ArchiveErrorCode.LENGTH_MISMATCH, "invalid data descriptor extent")
        descriptor_crc, descriptor_compressed, descriptor_size = struct.unpack("<3I", descriptor)
        if descriptor_compressed != info.compress_size or descriptor_size != info.file_size:
            _reject(ArchiveErrorCode.LENGTH_MISMATCH, "descriptor and central entry sizes disagree")
        if descriptor_crc != info.CRC:
            _reject(ArchiveErrorCode.CRC_MISMATCH, "descriptor and central CRC disagree")
    elif payload_end != boundary:
        _reject(ArchiveErrorCode.LENGTH_MISMATCH, "unaccounted bytes after compressed entry")
    return payload_start, payload_end


def _verify_content(data: bytes, start: int, end: int, info: zipfile.ZipInfo,
                    limits: ArchiveLimits, total: int) -> int:
    actual = 0
    crc = 0

    def output_limit() -> int:
        # One sentinel byte allows immediate rejection without allocating the
        # rest of a bomb. A lower caller budget lowers the decoder output cap.
        return min(_STREAM_CHUNK_BYTES, limits.entry_uncompressed_bytes - actual + 1,
                   limits.total_uncompressed_bytes - total - actual + 1)

    def consume(chunk: bytes) -> None:
        nonlocal actual, crc
        actual += len(chunk)
        if actual > limits.entry_uncompressed_bytes:
            _reject(ArchiveErrorCode.ENTRY_TOO_LARGE, "entry exceeds decoded byte limit")
        if total + actual > limits.total_uncompressed_bytes:
            _reject(ArchiveErrorCode.TOTAL_UNCOMPRESSED_TOO_LARGE, "archive exceeds decoded byte limit")
        crc = zlib.crc32(chunk, crc)

    cursor = start
    if info.compress_type == zipfile.ZIP_STORED:
        while cursor < end:
            chunk_end = min(end, cursor + output_limit())
            consume(data[cursor:chunk_end])
            cursor = chunk_end
    else:
        decoder = zlib.decompressobj(-15)
        pending = b""
        while True:
            if not pending and cursor < end:
                chunk_end = min(end, cursor + _STREAM_CHUNK_BYTES)
                pending = data[cursor:chunk_end]
                cursor = chunk_end
            chunk = decoder.decompress(pending, output_limit())
            consume(chunk)
            pending = decoder.unconsumed_tail
            if decoder.eof:
                if decoder.unused_data or pending or cursor != end:
                    _reject(ArchiveErrorCode.LENGTH_MISMATCH, "bytes remain after DEFLATE stream EOF")
                break
            if not pending and cursor == end and not chunk:
                _reject(ArchiveErrorCode.LENGTH_MISMATCH, "DEFLATE stream is truncated")
            # Empty input can drain already-buffered output. flush() is not
            # used: its length argument does not bound the returned output.
    if actual != info.file_size:
        _reject(ArchiveErrorCode.LENGTH_MISMATCH, "decoded length disagrees with central directory")
    if crc != info.CRC:
        _reject(ArchiveErrorCode.CRC_MISMATCH, "decoded CRC disagrees with central directory")
    return actual


def _inspect_archive_bytes(data: bytes, limits: ArchiveLimits) -> ArchiveInspectionResult:
    directory_start, directory_end, count = _directory_bounds(data, limits)
    raw_names = _central_names(data, directory_start, directory_end, count)
    # ZipFile owns central metadata decoding, including UTF-8/CP437 names.
    # Strip only the validated comment to avoid signatures within comments
    # confusing zipfile's EOCD search; archive entry bytes remain unchanged.
    parse_data = data[:directory_end + 20] + b"\x00\x00"
    with zipfile.ZipFile(io.BytesIO(parse_data)) as archive:
        infos = archive.infolist()
    if len(infos) != count:
        _reject(ArchiveErrorCode.NOT_A_ZIP, "central directory count mismatch")
    physical = sorted(infos, key=lambda info: info.header_offset)
    boundaries = {}
    for index, info in enumerate(physical):
        if index == 0 and info.header_offset != 0:
            _reject(ArchiveErrorCode.NOT_A_ZIP, "ZIP preambles are unsupported")
        boundary = physical[index + 1].header_offset if index + 1 < count else directory_start
        if info.header_offset >= boundary:
            _reject(ArchiveErrorCode.NOT_A_ZIP, "duplicate or overlapping local entry offsets")
        boundaries[info.header_offset] = boundary
    if not infos and directory_start != 0:
        _reject(ArchiveErrorCode.NOT_A_ZIP, "unexpected content before empty directory")
    entries = []
    accepted = []
    total = 0
    for info, raw_name in zip(infos, raw_names):
        name, is_dir = _check_info(info)
        _check_collision(name, "dir" if is_dir else "file", accepted)
        if info.file_size > limits.entry_uncompressed_bytes:
            _reject(ArchiveErrorCode.ENTRY_TOO_LARGE, "entry declares too many bytes")
        if total + info.file_size > limits.total_uncompressed_bytes:
            _reject(ArchiveErrorCode.TOTAL_UNCOMPRESSED_TOO_LARGE, "archive declares too many bytes")
        if info.file_size > max(info.compress_size, 1) * limits.compression_ratio:
            _reject(ArchiveErrorCode.COMPRESSION_RATIO_EXCEEDED, "entry exceeds compression ratio limit")
        start, end = _local_span(data, info, raw_name, boundaries[info.header_offset])
        actual = _verify_content(data, start, end, info, limits, total)
        if is_dir and actual:
            _reject(ArchiveErrorCode.LENGTH_MISMATCH, "directory entry has content")
        total += actual
        accepted.append((name, "dir" if is_dir else "file"))
        entries.append(ArchiveEntry(name, actual, info.compress_size, is_dir))
    return ArchiveInspectionResult(tuple(entries), len(entries), total)


def inspect_archive(archive_bytes, *, limits: ArchiveLimits | None = None) -> ArchiveInspectionResult:
    """Inspect bytes in memory without extracting or executing any entry."""
    limits = limits if limits is not None else ArchiveLimits()
    if not isinstance(archive_bytes, (bytes, bytearray, memoryview)):
        _reject(ArchiveErrorCode.NOT_A_ZIP, "archive_bytes must be bytes-like")
    size = archive_bytes.nbytes if isinstance(archive_bytes, memoryview) else len(archive_bytes)
    if size > limits.archive_bytes:
        _reject(ArchiveErrorCode.ARCHIVE_TOO_LARGE, "archive exceeds byte limit")
    data = bytes(archive_bytes)
    try:
        return _inspect_archive_bytes(data, limits)
    except ArchiveInspectionError:
        raise
    except UnicodeError:
        _reject(ArchiveErrorCode.INVALID_PATH, "invalid ZIP filename encoding")
    except NotImplementedError:
        _reject(ArchiveErrorCode.UNSUPPORTED_COMPRESSION, "unsupported ZIP format")
    except zlib.error:
        _reject(ArchiveErrorCode.LENGTH_MISMATCH, "invalid DEFLATE stream")
    except (zipfile.BadZipFile, struct.error, ValueError, OverflowError):
        _reject(ArchiveErrorCode.NOT_A_ZIP, "invalid ZIP structure")
