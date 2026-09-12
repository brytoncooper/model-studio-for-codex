"""Acceptance tests for the B20 archive inspection slice.

These tests build synthetic ZIP archives in memory, optionally mutate
the resulting byte string to drive specific defects, and feed the bytes
through :func:`inspect_archive`. They never touch the filesystem
beyond import-time, never spawn processes, and never open network
sockets.
"""
from __future__ import annotations

import io
import stat
import struct
import unittest
import zipfile
import zlib
from typing import Callable

from model_deck.plugins.archive_inspection import (
    DEFAULT_ARCHIVE_BYTES,
    DEFAULT_COMPRESSION_RATIO,
    DEFAULT_ENTRY_COUNT,
    DEFAULT_ENTRY_UNCOMPRESSED_BYTES,
    DEFAULT_TOTAL_UNCOMPRESSED_BYTES,
    ArchiveEntry,
    ArchiveErrorCode,
    ArchiveInspectionError,
    ArchiveInspectionResult,
    ArchiveLimits,
    inspect_archive,
)


# ---------------------------------------------------------------------------
# Synthetic ZIP builders
# ---------------------------------------------------------------------------


def _build_zip(
    setup: Callable[[zipfile.ZipFile], None],
    *,
    compress_level: int = zipfile.ZIP_STORED,
) -> bytes:
    """Build an in-memory ZIP by running ``setup`` against a writable archive.

    The default compression level is STORED so tests can build predictable
    metadata; tests that exercise DEFLATED set ``compress_level`` to
    ``ZIP_DEFLATED`` explicitly.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compress_level) as zf:
        setup(zf)
    return buf.getvalue()


def _writestr(
    zf: zipfile.ZipFile,
    name: str,
    data: bytes,
    *,
    compress_type: int | None = None,
    external_attr: int | None = None,
) -> None:
    """``ZipFile.writestr`` wrapper that lets the test force attributes."""
    info = zipfile.ZipInfo(filename=name)
    if compress_type is not None:
        info.compress_type = compress_type
    if external_attr is not None:
        info.external_attr = external_attr
    zf.writestr(info, data)


def _find_eocd_offset(buf: bytes) -> int:
    """Return the byte offset of the End of Central Directory record."""
    sig = b"PK\x05\x06"
    return buf.rfind(sig)


def _central_dir_offset(buf: bytes) -> int:
    """Return the offset of the central directory from the EOCD record."""
    eocd = _find_eocd_offset(buf)
    assert eocd >= 0, "missing EOCD"
    # EOCD layout: sig(4) + disk fields(6) + total_entries(4) +
    # cd_size(4) + cd_offset(4) + comment_length(2).
    cd_offset = struct.unpack_from("<I", buf, eocd + 16)[0]
    return cd_offset


def _patch_central_dir(buf: bytearray, matcher: Callable[[bytes], bool], offset_delta: int, new_byte: int) -> bool:
    """Walk central-directory entries, calling ``matcher(filename_bytes)``.

    When the matcher returns True, the entry at ``offset + offset_delta``
    is overwritten with ``new_byte``. Returns True on first match.
    """
    cd = _central_dir_offset(buf)
    pos = cd
    sig = b"PK\x01\x02"
    while pos + 46 <= len(buf) and buf[pos:pos + 4] == sig:
        # Central directory file header: filename length at offset 28,
        # extra length at offset 30, comment length at offset 32.
        name_len = struct.unpack_from("<H", buf, pos + 28)[0]
        extra_len = struct.unpack_from("<H", buf, pos + 30)[0]
        comment_len = struct.unpack_from("<H", buf, pos + 32)[0]
        name_bytes = bytes(buf[pos + 46:pos + 46 + name_len])
        if matcher(name_bytes):
            buf[pos + offset_delta] = new_byte
            return True
        pos += 46 + name_len + extra_len + comment_len
    return False


def _flag_bits_offset() -> int:
    return 8


def _compress_type_offset() -> int:
    return 10


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class InspectArchiveHappyPathTests(unittest.TestCase):
    def test_stored_single_file(self) -> None:
        payload = b"hello world"
        buf = _build_zip(lambda zf: _writestr(zf, "hello.txt", payload))
        result = inspect_archive(buf)
        self.assertTrue(result.ok)
        self.assertEqual(result.total_entries, 1)
        self.assertEqual(result.total_uncompressed_size, len(payload))
        self.assertEqual(len(result.entries), 1)
        entry = result.entries[0]
        self.assertIsInstance(entry, ArchiveEntry)
        self.assertEqual(entry.name, "hello.txt")
        self.assertEqual(entry.size, len(payload))
        self.assertEqual(entry.compressed_size, len(payload))
        self.assertFalse(entry.is_dir)

    def test_deflated_with_padding(self) -> None:
        payload = b"a" * 8192
        buf = _build_zip(
            lambda zf: _writestr(
                zf, "big.txt", payload,
                compress_type=zipfile.ZIP_DEFLATED,
            ),
            compress_level=zipfile.ZIP_DEFLATED,
        )
        result = inspect_archive(buf)
        self.assertTrue(result.ok)
        self.assertEqual(result.total_entries, 1)
        entry = result.entries[0]
        self.assertEqual(entry.name, "big.txt")
        self.assertEqual(entry.size, len(payload))
        self.assertGreater(entry.compressed_size, 0)

    def test_nested_directories_and_files(self) -> None:
        def setup(zf: zipfile.ZipFile) -> None:
            _writestr(zf, "plugin/", b"")
            _writestr(zf, "plugin/manifest.json", b'{"id":"demo"}')
            _writestr(zf, "plugin/lib/data.bin", b"\x00\x01\x02")

        result = inspect_archive(_build_zip(setup))
        self.assertTrue(result.ok)
        names = [e.name for e in result.entries]
        self.assertEqual(
            names,
            ["plugin/", "plugin/manifest.json", "plugin/lib/data.bin"],
        )
        self.assertTrue(result.entries[0].is_dir)
        self.assertFalse(result.entries[1].is_dir)
        self.assertEqual(result.total_uncompressed_size, len(b'{"id":"demo"}') + 3)

    def test_empty_archive_is_ok(self) -> None:
        buf = _build_zip(lambda _zf: None)
        result = inspect_archive(buf)
        self.assertTrue(result.ok)
        self.assertEqual(result.total_entries, 0)
        self.assertEqual(result.total_uncompressed_size, 0)
        self.assertEqual(result.entries, ())

    def test_result_is_immutable(self) -> None:
        buf = _build_zip(lambda zf: _writestr(zf, "x.txt", b"x"))
        result = inspect_archive(buf)
        with self.assertRaises(Exception):
            result.entries = ()  # type: ignore[misc]
        with self.assertRaises(Exception):
            result.total_entries = 99  # type: ignore[misc]


class InspectArchivePathDefectTests(unittest.TestCase):
    def _expect_path_error(self, name: str, code: str) -> None:
        buf = _build_zip(lambda zf: _writestr(zf, name, b"x"))
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(ctx.exception.entry_name, name)

    def test_absolute_path_rejected(self) -> None:
        self._expect_path_error("/etc/passwd", ArchiveErrorCode.INVALID_PATH)

    def test_backslash_rejected(self) -> None:
        self._expect_path_error("a\\b.txt", ArchiveErrorCode.INVALID_PATH)

    def test_drive_prefix_rejected(self) -> None:
        self._expect_path_error("C:windows.txt", ArchiveErrorCode.INVALID_PATH)

    def test_dot_segment_rejected(self) -> None:
        self._expect_path_error("./a.txt", ArchiveErrorCode.INVALID_PATH)

    def test_dotdot_segment_rejected(self) -> None:
        self._expect_path_error("a/../b.txt", ArchiveErrorCode.INVALID_PATH)

    def test_empty_segment_rejected(self) -> None:
        self._expect_path_error("a//b.txt", ArchiveErrorCode.INVALID_PATH)

    def test_empty_name_rejected_via_helper(self) -> None:
        # Direct structural check; zipfile normalises away an empty name at
        # write time, so this path is exercised via the helper rather than
        # via a synthesised archive.
        from model_deck.plugins.archive_inspection.inspect import (
            _normalise_entry_name,
        )

        with self.assertRaises(ArchiveInspectionError) as ctx:
            _normalise_entry_name("")
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.INVALID_PATH)
        self.assertEqual(ctx.exception.entry_name, "")


class InspectArchiveCollisionTests(unittest.TestCase):
    def test_exact_duplicate_rejected(self) -> None:
        def setup(zf: zipfile.ZipFile) -> None:
            _writestr(zf, "a.txt", b"one")
            _writestr(zf, "a.txt", b"two")

        buf = _build_zip(setup)
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf)
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.DUPLICATE_PATH)
        self.assertEqual(ctx.exception.entry_name, "a.txt")

    def test_casefold_collision_rejected(self) -> None:
        def setup(zf: zipfile.ZipFile) -> None:
            _writestr(zf, "Foo.txt", b"one")
            _writestr(zf, "foo.txt", b"two")

        buf = _build_zip(setup)
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf)
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.DUPLICATE_PATH)

    def test_file_vs_directory_collision_rejected(self) -> None:
        def setup(zf: zipfile.ZipFile) -> None:
            _writestr(zf, "shared", b"data")
            _writestr(zf, "shared/", b"")

        buf = _build_zip(setup)
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf)
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.FILE_PARENT_COLLISION)

    def test_directory_descending_from_file_rejected(self) -> None:
        def setup(zf: zipfile.ZipFile) -> None:
            # A directory "foo/bar/" descending from file "foo" is a
            # file-parent collision; the second entry is a directory
            # (trailing slash) so the rule actually fires.
            _writestr(zf, "foo", b"data")
            _writestr(zf, "foo/bar/", b"")

        buf = _build_zip(setup)
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf)
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.FILE_PARENT_COLLISION)

    def test_file_prefix_of_directory_rejected(self) -> None:
        def setup(zf: zipfile.ZipFile) -> None:
            _writestr(zf, "dir/", b"")
            _writestr(zf, "dir", b"data")  # not valid in normal tools,
            # but the inspector still rejects the collision.

        buf = _build_zip(setup)
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf)
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.FILE_PARENT_COLLISION)


class InspectArchiveSymlinkAndEncryptionTests(unittest.TestCase):
    def test_symlink_rejected(self) -> None:
        symlink_mode = (stat.S_IFLNK | 0o777) << 16
        buf = _build_zip(
            lambda zf: _writestr(
                zf,
                "link.txt",
                b"target",
                external_attr=symlink_mode,
            )
        )
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf)
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.SYMLINK_OR_SPECIAL_ENTRY)
        self.assertEqual(ctx.exception.entry_name, "link.txt")

    def test_character_device_rejected(self) -> None:
        # Use a fifo; both fifo and char/block devices must be rejected.
        fifo_mode = (stat.S_IFIFO | 0o666) << 16
        buf = _build_zip(
            lambda zf: _writestr(zf, "fifo", b"", external_attr=fifo_mode)
        )
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf)
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.SYMLINK_OR_SPECIAL_ENTRY)

    def test_encrypted_entry_rejected(self) -> None:
        buf = bytearray(
            _build_zip(lambda zf: _writestr(zf, "secret.txt", b"hi"))
        )
        self.assertTrue(
            _patch_central_dir(
                buf,
                lambda n: n == b"secret.txt",
                offset_delta=_flag_bits_offset(),
                new_byte=0x01,
            )
        )
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(bytes(buf))
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.ENCRYPTED_ENTRY)

    def test_bzip2_compression_rejected(self) -> None:
        buf = bytearray(
            _build_zip(
                lambda zf: _writestr(
                    zf,
                    "data.txt",
                    b"x",
                    compress_type=12,  # BZIP2
                )
            )
        )
        # ``zipfile.ZipFile.writestr`` will likely reject the unsupported
        # method, so build a STORED archive and patch the central-dir
        # compress_type to BZIP2.
        buf = bytearray(
            _build_zip(lambda zf: _writestr(zf, "data.txt", b"x"))
        )
        self.assertTrue(
            _patch_central_dir(
                buf,
                lambda n: n == b"data.txt",
                offset_delta=_compress_type_offset(),
                new_byte=12,
            )
        )
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(bytes(buf))
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.UNSUPPORTED_COMPRESSION
        )

    def test_lzma_compression_rejected(self) -> None:
        buf = bytearray(
            _build_zip(lambda zf: _writestr(zf, "data.txt", b"x"))
        )
        self.assertTrue(
            _patch_central_dir(
                buf,
                lambda n: n == b"data.txt",
                offset_delta=_compress_type_offset(),
                new_byte=14,  # LZMA
            )
        )
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(bytes(buf))
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.UNSUPPORTED_COMPRESSION
        )


class InspectArchiveLimitTests(unittest.TestCase):
    def test_archive_too_large(self) -> None:
        limits = ArchiveLimits(archive_bytes=8)
        buf = _build_zip(lambda zf: _writestr(zf, "a.txt", b"abcdef"))
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf, limits=limits)
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.ARCHIVE_TOO_LARGE)

    def test_entry_too_large(self) -> None:
        limits = ArchiveLimits(entry_uncompressed_bytes=4)
        buf = _build_zip(lambda zf: _writestr(zf, "a.txt", b"abcdef"))
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf, limits=limits)
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.ENTRY_TOO_LARGE)

    def test_compression_ratio_exceeded(self) -> None:
        # 1024 bytes of zeros compressed to a few bytes; ratio=2 forces
        # the rejection. _writestr must request DEFLATED explicitly
        # because passing a ZipInfo overrides the ZipFile default.
        payload = b"\x00" * 1024
        limits = ArchiveLimits(compression_ratio=2)
        buf = _build_zip(
            lambda zf: _writestr(
                zf, "zeros.bin", payload,
                compress_type=zipfile.ZIP_DEFLATED,
            ),
            compress_level=zipfile.ZIP_DEFLATED,
        )
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf, limits=limits)
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.COMPRESSION_RATIO_EXCEEDED
        )

    def test_total_uncompressed_too_large(self) -> None:
        limits = ArchiveLimits(total_uncompressed_bytes=4)
        def setup(zf: zipfile.ZipFile) -> None:
            _writestr(zf, "a.txt", b"hello")
            _writestr(zf, "b.txt", b"world")

        buf = _build_zip(setup)
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf, limits=limits)
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.TOTAL_UNCOMPRESSED_TOO_LARGE
        )

    def test_too_many_entries(self) -> None:
        limits = ArchiveLimits(entry_count=1)
        def setup(zf: zipfile.ZipFile) -> None:
            _writestr(zf, "a.txt", b"a")
            _writestr(zf, "b.txt", b"b")

        buf = _build_zip(setup)
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf, limits=limits)
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.TOO_MANY_ENTRIES)

    def test_cumulative_uncompressed_exceeded_during_stream(self) -> None:
        # First entry fits within the cumulative cap; second entry pushes
        # us over. The metadata gate allows the first entry to pass, the
        # second's metadata gate does the same (because each entry is
        # under the per-entry cap), but cumulative_uncompressed + size
        # exceeds the total. We pick the cumulative cap to be just under
        # the sum so the metadata gate triggers first; here we make the
        # cap exactly equal to the first entry's size so the second
        # entry's metadata addition trips the cap.
        limits = ArchiveLimits(
            total_uncompressed_bytes=5,
            entry_uncompressed_bytes=5,
        )

        def setup(zf: zipfile.ZipFile) -> None:
            _writestr(zf, "a.txt", b"hello")
            _writestr(zf, "b.txt", b"world")

        buf = _build_zip(setup)
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf, limits=limits)
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.TOTAL_UNCOMPRESSED_TOO_LARGE
        )


class InspectArchiveContentIntegrityTests(unittest.TestCase):
    def test_crc_mismatch_detected(self) -> None:
        # Use a moderately long STORED payload so flipping a mid-stream
        # byte produces a pure CRC mismatch rather than a corrupted
        # deflate stream. The inspector's reclassified code path
        # surfaces this as CRC_MISMATCH.
        payload = b"".join(bytes([i & 0xFF]) * 16 for i in range(64))
        buf = bytearray(
            _build_zip(lambda zf: _writestr(zf, "long.bin", payload))
        )
        local_sig = b"PK\x03\x04"
        idx = buf.index(local_sig)
        name_len = struct.unpack_from("<H", buf, idx + 26)[0]
        extra_len = struct.unpack_from("<H", buf, idx + 28)[0]
        data_offset = idx + 30 + name_len + extra_len + 100
        buf[data_offset] = (buf[data_offset] + 1) & 0xFF
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(bytes(buf))
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.CRC_MISMATCH)

    def test_length_mismatch_detected_by_truncation(self) -> None:
        buf = bytearray(
            _build_zip(lambda zf: _writestr(zf, "hello.txt", b"hello world"))
        )
        # Find the local file header for hello.txt and chop off the last
        # byte of the data section. The local header gives us the
        # compressed size; we shorten the file to leave the central
        # directory referring to bytes that no longer exist.
        local_sig = b"PK\x03\x04"
        idx = buf.index(local_sig)
        comp_size = struct.unpack_from("<I", buf, idx + 18)[0]
        name_len = struct.unpack_from("<H", buf, idx + 26)[0]
        extra_len = struct.unpack_from("<H", buf, idx + 28)[0]
        data_start = idx + 30 + name_len + extra_len
        data_end = data_start + comp_size
        # Truncate one byte so the file is shorter than the local/central
        # directory says.
        truncated = bytes(buf[:data_end - 1])
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(truncated)
        # Either NOT_A_ZIP (truncated EOCD) or LENGTH_MISMATCH (truncated
        # entry) is acceptable; in both cases the inspector caught the
        # defect.
        self.assertIn(
            ctx.exception.code,
            {ArchiveErrorCode.NOT_A_ZIP, ArchiveErrorCode.LENGTH_MISMATCH},
        )


class InspectArchiveInputHandlingTests(unittest.TestCase):
    def test_non_bytes_rejected(self) -> None:
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive("not bytes")  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.NOT_A_ZIP)

    def test_not_a_zip_rejected(self) -> None:
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(b"\x00\x01\x02 not a zip at all")
        self.assertEqual(ctx.exception.code, ArchiveErrorCode.NOT_A_ZIP)


class ArchiveLimitsValidationTests(unittest.TestCase):
    def test_defaults_match_module_constants(self) -> None:
        defaults = ArchiveLimits()
        self.assertEqual(defaults.archive_bytes, DEFAULT_ARCHIVE_BYTES)
        self.assertEqual(
            defaults.total_uncompressed_bytes, DEFAULT_TOTAL_UNCOMPRESSED_BYTES
        )
        self.assertEqual(
            defaults.entry_uncompressed_bytes, DEFAULT_ENTRY_UNCOMPRESSED_BYTES
        )
        self.assertEqual(defaults.entry_count, DEFAULT_ENTRY_COUNT)
        self.assertEqual(defaults.compression_ratio, DEFAULT_COMPRESSION_RATIO)

    def test_bool_rejected_for_every_field(self) -> None:
        for field_name in (
            "archive_bytes",
            "total_uncompressed_bytes",
            "entry_uncompressed_bytes",
            "entry_count",
            "compression_ratio",
        ):
            with self.assertRaises(ValueError):
                ArchiveLimits(**{field_name: True})  # type: ignore[arg-type]

    def test_zero_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ArchiveLimits(archive_bytes=0)
        with self.assertRaises(ValueError):
            ArchiveLimits(compression_ratio=0)

    def test_negative_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ArchiveLimits(entry_uncompressed_bytes=-1)

    def test_non_int_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ArchiveLimits(entry_count=1.5)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ArchiveLimits(entry_count="10")  # type: ignore[arg-type]


class InspectArchiveDetachedTests(unittest.TestCase):
    def test_result_detached_from_input(self) -> None:
        payload = b"hello world"
        buf = bytearray(
            _build_zip(lambda zf: _writestr(zf, "hello.txt", payload))
        )
        result = inspect_archive(bytes(buf))
        # Mutating the input after inspection does not affect the result.
        for i in range(len(buf)):
            buf[i] = 0
        self.assertEqual(result.entries[0].size, len(payload))





class InspectArchiveHostileRegressionTests(unittest.TestCase):
    """Hostile regression tests for the seven defects identified by
    b09_integration_review.

    Each test builds a synthetic ZIP (or mutates one byte-level) that
    triggers the named defect, and asserts the inspector raises the
    expected stable error code rather than silently accepting the
    archive. The tests never touch the filesystem, never spawn
    processes, and never open network sockets.
    """

    # --- Defect 1: hidden deflate payload accepted via declared
    #                file_size=0 / CRC=0 in the central directory.

    def test_hidden_deflate_payload_rejected(self) -> None:
        # Build a normal DEFLATED entry, then patch both local and
        # central directories to claim file_size=0 and CRC=0. The
        # actual compressed bytes are left in place, so a raw decoder
        # that ignores metadata would happily inflate them to real
        # content. The inspector must catch this with LENGTH_MISMATCH
        # because actual_size > declared file_size=0.
        payload = b"hidden payload that should be detected"
        buf = bytearray(
            _build_zip(
                lambda zf: _writestr(
                    zf,
                    "hidden.bin",
                    payload,
                    compress_type=zipfile.ZIP_DEFLATED,
                ),
                compress_level=zipfile.ZIP_DEFLATED,
            )
        )
        local_sig = b"PK"
        local_idx = buf.index(local_sig)
        # Local header layout: crc at +14 (4 bytes), file_size at +22.
        struct.pack_into("<I", buf, local_idx + 14, 0)  # local crc = 0
        struct.pack_into("<I", buf, local_idx + 22, 0)  # local file_size = 0
        # Central directory: crc at +16, file_size at +24.
        cd = _central_dir_offset(buf)
        struct.pack_into("<I", buf, cd + 16, 0)  # central crc = 0
        struct.pack_into("<I", buf, cd + 24, 0)  # central file_size = 0
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(bytes(buf))
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.LENGTH_MISMATCH
        )

    # --- Defect 2: prefix collision ``a`` (file) vs ``a/b`` (file).

    def test_prefix_collision_file_then_descendant_file_rejected(self) -> None:
        # File ``a`` followed by file ``a/b`` is a bidirectional
        # prefix collision because the second file descends from the
        # first file, which is illegal.
        buf = _build_zip(
            lambda zf: (
                _writestr(zf, "a", b"x"),
                _writestr(zf, "a/b", b"y"),
            )
        )
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf)
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.FILE_PARENT_COLLISION
        )

    def test_prefix_collision_descendant_file_then_file_rejected(self) -> None:
        # The reverse order: file ``a/b`` is accepted first, then file
        # ``a`` arrives. The collision rule must fire in both
        # directions; the previous code only checked one direction.
        buf = _build_zip(
            lambda zf: (
                _writestr(zf, "a/b", b"y"),
                _writestr(zf, "a", b"x"),
            )
        )
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(buf)
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.FILE_PARENT_COLLISION
        )

    # --- Defect 3: symlink directory marker accepted.

    def test_symlink_directory_marker_rejected(self) -> None:
        # Build a normal directory entry, then patch the central
        # directory's external_attr to mark the entry as a symlink
        # (S_IFLNK | 0o755). The previous code accepted this as a
        # directory because the trailing slash is set.
        buf = bytearray(
            _build_zip(lambda zf: _writestr(zf, "plugin/", b""))
        )
        cd = _central_dir_offset(buf)
        symlink_mode = (stat.S_IFLNK | 0o755) << 16
        struct.pack_into("<I", buf, cd + 38, symlink_mode)
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(bytes(buf))
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.SYMLINK_OR_SPECIAL_ENTRY
        )

    # --- Defect 4: directory payload ignored.

    def test_directory_with_metadata_size_but_no_data_rejected(self) -> None:
        # Build a directory entry with no content. Patch both local
        # and central to claim file_size=5. The previous code skipped
        # verification for directory entries entirely, so the
        # mismatch went unnoticed. The inspector must catch it with
        # LENGTH_MISMATCH because actual_size=0 != declared 5.
        buf = bytearray(
            _build_zip(lambda zf: _writestr(zf, "plugin/", b""))
        )
        local_sig = b"PK"
        local_idx = buf.index(local_sig)
        struct.pack_into("<I", buf, local_idx + 22, 5)  # local file_size = 5
        cd = _central_dir_offset(buf)
        struct.pack_into("<I", buf, cd + 24, 5)  # central file_size = 5
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(bytes(buf))
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.LENGTH_MISMATCH
        )

    # --- Defect 5: raw NUL filename truncated.

    def test_raw_nul_filename_rejected(self) -> None:
        # Python's zipfile silently truncates filenames at the first
        # NUL byte, so we build a normal ZIP and then patch the raw
        # central-directory filename bytes to include a NUL. The
        # inspector must read the full orig_filename (not the
        # NUL-truncated ``filename`` accessor) and raise
        # INVALID_PATH.
        buf = bytearray(
            _build_zip(lambda zf: _writestr(zf, "ok.txt", b"x"))
        )
        cd = _central_dir_offset(buf)
        # Replace the 6 raw filename bytes with 6 bytes that include
        # a NUL: "ok" + NUL + ".tx" (same length, no need to patch
        # filename_length).
        name_offset = cd + 46
        buf[name_offset:name_offset + 6] = b"ok\x00.tx"
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(bytes(buf))
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.INVALID_PATH
        )

    # --- Defect 6: malformed UTF-8 filename accepted as UTF-8.

    def test_malformed_utf8_filename_rejected(self) -> None:
        # Build a normal entry. Then patch the central directory to
        # set the UTF-8 flag (0x0800) and replace the raw filename
        # bytes with bytes that are not valid UTF-8 (0xFF). The
        # inspector must catch the decode failure and raise
        # INVALID_PATH rather than crashing or returning a
        # UnicodeDecodeError.
        buf = bytearray(
            _build_zip(lambda zf: _writestr(zf, "ok.txt", b"x"))
        )
        cd = _central_dir_offset(buf)
        # Force the UTF-8 flag bit in the central directory.
        flag_bits = struct.unpack_from("<H", buf, cd + 8)[0]
        flag_bits |= 0x0800
        struct.pack_into("<H", buf, cd + 8, flag_bits)
        # Replace the 6 raw filename bytes with 0xFF (invalid UTF-8).
        name_offset = cd + 46
        for i in range(6):
            buf[name_offset + i] = 0xFF
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(bytes(buf))
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.INVALID_PATH
        )

    # --- Defect 7: unsupported extract_version escape.

    def test_unsupported_extract_version_rejected(self) -> None:
        # Patch the central directory's extract_version to 100, which
        # is past the ZIP spec current 6.3 (encoded as 63). The
        # inspector must reject this with UNSUPPORTED_COMPRESSION
        # rather than accepting a tool or version it cannot
        # interpret.
        buf = bytearray(
            _build_zip(lambda zf: _writestr(zf, "ok.txt", b"x"))
        )
        cd = _central_dir_offset(buf)
        struct.pack_into("<H", buf, cd + 6, 100)  # extract_version = 100
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(bytes(buf))
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.UNSUPPORTED_COMPRESSION
        )

    def test_unsupported_create_system_rejected(self) -> None:
        # Patch the high byte of ``version_made_by`` to a create
        # system value > 14 (the highest defined ZIP system). This
        # covers the third clause of the version-range gate.
        buf = bytearray(
            _build_zip(lambda zf: _writestr(zf, "ok.txt", b"x"))
        )
        cd = _central_dir_offset(buf)
        version_made_by = struct.unpack_from("<H", buf, cd + 4)[0]
        version_made_by = (15 << 8) | (version_made_by & 0xFF)
        struct.pack_into("<H", buf, cd + 4, version_made_by)
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(bytes(buf))
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.UNSUPPORTED_COMPRESSION
        )

    # --- Deflate-stream safety: STORED with declared compressed span
    # that disagrees with the actual bytes that follow.

    def test_stored_compressed_span_length_mismatch_rejected(self) -> None:
        # Build a STORED entry, then patch the central/local
        # compressed_size to a value smaller than the actual number
        # of bytes that follow. The compressed span will end inside
        # the data, leaving bytes between data_end and the next PK
        # signature. The size/CRC cross-check must fire.
        buf = bytearray(
            _build_zip(lambda zf: _writestr(zf, "ok.txt", b"abcdef"))
        )
        local_sig = b"PK"
        local_idx = buf.index(local_sig)
        # Local: compressed_size at +18, file_size at +22.
        struct.pack_into("<I", buf, local_idx + 18, 3)  # claim 3 bytes
        # Keep file_size at 6 so the actual decode (STORED branch
        # reads compressed_chunk of length 3 and reports actual_size=3
        # vs actual_file_size=6) fires LENGTH_MISMATCH.
        cd = _central_dir_offset(buf)
        struct.pack_into("<I", buf, cd + 20, 3)  # central compressed_size = 3
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(bytes(buf))
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.LENGTH_MISMATCH
        )

    # --- Empty DEFLATED directory must still validate empty output.

    def test_empty_deflated_directory_accepted(self) -> None:
        # Build an empty directory with DEFLATED compression. The
        # compressed span is the DEFLATE representation of empty
        # input (a few bytes), and the inspector must decode it to
        # zero bytes and accept the directory.
        def setup(zf: zipfile.ZipFile) -> None:
            _writestr(zf, "dir/", b"", compress_type=zipfile.ZIP_DEFLATED)

        buf = _build_zip(setup, compress_level=zipfile.ZIP_DEFLATED)
        result = inspect_archive(buf)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.entries), 1)
        self.assertTrue(result.entries[0].is_dir)

    # --- Data descriptor entries (flag_bits & 0x08) with zero local
    # sizes/CRC are supported, not banned.

    def test_data_descriptor_entry_accepted(self) -> None:
        # Build a single DEFLATED entry via the normal zipfile path
        # to learn the real metadata, then splice in a data
        # descriptor record after the compressed bytes and patch the
        # local header to advertise flag_bits & 0x08 with zero
        # sizes/CRC. The central directory is shifted and its
        # flag_bits updated to match. zipfile does not write a
        # descriptor record by default.
        payload = b"descriptor payload"
        base = bytearray(
            _build_zip(
                lambda zf: _writestr(
                    zf,
                    "descriptor.bin",
                    payload,
                    compress_type=zipfile.ZIP_DEFLATED,
                ),
                compress_level=zipfile.ZIP_DEFLATED,
            )
        )
        local_idx = base.index(b"PK\x03\x04")
        name_len = struct.unpack_from("<H", base, local_idx + 26)[0]
        extra_len = struct.unpack_from("<H", base, local_idx + 28)[0]
        comp_size = struct.unpack_from("<I", base, local_idx + 18)[0]
        file_size = struct.unpack_from("<I", base, local_idx + 22)[0]
        crc = struct.unpack_from("<I", base, local_idx + 14)[0]
        data_start = local_idx + 30 + name_len + extra_len
        data_end = data_start + comp_size
        out = bytearray(base)
        # Patch local header: flag_bits |= 0x08, zero sizes and CRC.
        local_flag_bits = struct.unpack_from("<H", out, local_idx + 6)[0]
        local_flag_bits |= 0x08
        struct.pack_into("<H", out, local_idx + 6, local_flag_bits)
        struct.pack_into("<I", out, local_idx + 14, 0)  # local crc
        struct.pack_into("<I", out, local_idx + 18, 0)  # local compressed_size
        struct.pack_into("<I", out, local_idx + 22, 0)  # local file_size
        # Splice in PK\x07\x08 + descriptor record after the data.
        descriptor = b"PK\x07\x08" + struct.pack(
            "<III", crc, comp_size, file_size
        )
        insert_pos = data_end
        out = out[:insert_pos] + bytearray(descriptor) + out[insert_pos:]
        shift = len(descriptor)
        # Patch EOCD central-directory offset.
        eocd_idx = bytes(out).rfind(b"PK\x05\x06")
        cd_off = struct.unpack_from("<I", out, eocd_idx + 16)[0]
        struct.pack_into("<I", out, eocd_idx + 16, cd_off + shift)
        cd_new = cd_off + shift
        # Patch central flag_bits |= 0x08.
        cd_flag = struct.unpack_from("<H", out, cd_new + 8)[0]
        cd_flag |= 0x08
        struct.pack_into("<H", out, cd_new + 8, cd_flag)
        result = inspect_archive(bytes(out))
        self.assertTrue(result.ok)
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0].name, "descriptor.bin")
        self.assertEqual(result.entries[0].size, len(payload))

    def test_data_descriptor_comp_size_mismatch_rejected(self) -> None:
        # As above but the descriptor's compressed-size field is
        # forged to disagree with the actual span length. The
        # inspector must catch this with LENGTH_MISMATCH.
        payload = b"descriptor payload"
        base = bytearray(
            _build_zip(
                lambda zf: _writestr(
                    zf,
                    "descriptor.bin",
                    payload,
                    compress_type=zipfile.ZIP_DEFLATED,
                ),
                compress_level=zipfile.ZIP_DEFLATED,
            )
        )
        local_idx = base.index(b"PK\x03\x04")
        name_len = struct.unpack_from("<H", base, local_idx + 26)[0]
        extra_len = struct.unpack_from("<H", base, local_idx + 28)[0]
        comp_size = struct.unpack_from("<I", base, local_idx + 18)[0]
        file_size = struct.unpack_from("<I", base, local_idx + 22)[0]
        crc = struct.unpack_from("<I", base, local_idx + 14)[0]
        data_start = local_idx + 30 + name_len + extra_len
        data_end = data_start + comp_size
        out = bytearray(base)
        local_flag_bits = struct.unpack_from("<H", out, local_idx + 6)[0]
        local_flag_bits |= 0x08
        struct.pack_into("<H", out, local_idx + 6, local_flag_bits)
        struct.pack_into("<I", out, local_idx + 14, 0)
        struct.pack_into("<I", out, local_idx + 18, 0)
        struct.pack_into("<I", out, local_idx + 22, 0)
        # Splice in a descriptor whose compressed-size is forged:
        # claim 99 but the actual span is comp_size bytes.
        descriptor = b"PK\x07\x08" + struct.pack(
            "<III", crc, 99, file_size
        )
        insert_pos = data_end
        out = out[:insert_pos] + bytearray(descriptor) + out[insert_pos:]
        shift = len(descriptor)
        eocd_idx = bytes(out).rfind(b"PK\x05\x06")
        cd_off = struct.unpack_from("<I", out, eocd_idx + 16)[0]
        struct.pack_into("<I", out, eocd_idx + 16, cd_off + shift)
        cd_new = cd_off + shift
        cd_flag = struct.unpack_from("<H", out, cd_new + 8)[0]
        cd_flag |= 0x08
        struct.pack_into("<H", out, cd_new + 8, cd_flag)
        with self.assertRaises(ArchiveInspectionError) as ctx:
            inspect_archive(bytes(out))
        self.assertEqual(
            ctx.exception.code, ArchiveErrorCode.LENGTH_MISMATCH
        )


class InspectArchivePureStdlibTests(unittest.TestCase):
    def test_no_model_deck_imports_outside_archive_inspection(self) -> None:
        """Static check that the slice depends only on the stdlib."""
        import inspect as inspect_mod
        from model_deck.plugins import archive_inspection

        module = archive_inspection.inspect
        source = inspect_mod.getsource(module)
        bad = []
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if "from __future__" in stripped:
                continue
            if stripped.startswith("from .") or stripped.startswith("from model_deck"):
                bad.append(line)
            elif stripped.startswith("import "):
                # Allow stdlib imports.
                rest = stripped[len("import "):].split(" as ")[0].split(",")[0].strip()
                top = rest.split(".")[0]
                if top not in {
                    "struct",
                    "dataclasses",
                    "io",
                    "stat",
                    "typing",
                    "zipfile",
                    "zlib",
                }:
                    bad.append(line)
        self.assertEqual(bad, [], f"unexpected import: {bad!r}")




class ArchiveExactSpanRegressionTests(unittest.TestCase):
    """Independent wire fixtures for metadata agreement and bounded decoding."""

    @staticmethod
    def archive(payload=b"payload", *, method=0, compressed=None,
                descriptor=False, signed=True, name=b"a.bin", flags=0):
        encoded = payload if compressed is None else compressed
        crc = zlib.crc32(payload)
        flags |= 8 if descriptor else 0
        local = struct.pack(
            "<IHHHHHIIIHH", 0x04034b50, 20, flags, method, 0, 0,
            0 if descriptor else crc, 0 if descriptor else len(encoded),
            0 if descriptor else len(payload), len(name), 0,
        ) + name + encoded
        if descriptor:
            local += (b"PK\x07\x08" if signed else b"") + struct.pack("<III", crc, len(encoded), len(payload))
        central = struct.pack(
            "<IHHHHHHIIIHHHHHII", 0x02014b50, 20, 20, flags, method,
            0, 0, crc, len(encoded), len(payload), len(name), 0, 0, 0, 0, 0, 0,
        ) + name
        return local + central + struct.pack("<IHHHHIIH", 0x06054b50, 0, 0, 1, 1, len(central), len(local), 0)

    def test_zero_central_size_cannot_hide_local_payload(self):
        data = bytearray(self.archive())
        central = _central_dir_offset(data)
        struct.pack_into("<I", data, central + 16, 0)
        struct.pack_into("<I", data, central + 24, 0)
        with self.assertRaises(ArchiveInspectionError):
            inspect_archive(data)

    def test_local_name_must_equal_central_name(self):
        for name in (b"../xx", b"b.bin", b"a\x00bin"):
            with self.subTest(name=name):
                data = bytearray(self.archive())
                data[30:35] = name
                with self.assertRaises(ArchiveInspectionError) as caught:
                    inspect_archive(data)
                self.assertEqual(caught.exception.code, ArchiveErrorCode.INVALID_PATH)

    def test_local_format_and_crc_must_match(self):
        for offset, fmt, value in ((4, "<H", 63), (6, "<H", 1), (8, "<H", 8), (14, "<I", 0)):
            with self.subTest(offset=offset):
                data = bytearray(self.archive())
                struct.pack_into(fmt, data, offset, value)
                with self.assertRaises(ArchiveInspectionError):
                    inspect_archive(data)

    def test_unfed_suffix_after_deflate_chunk_boundary_rejected(self):
        payload = b"x" * 65531
        encoded = b"\x01" + struct.pack("<HH", len(payload), 65535 - len(payload)) + payload
        self.assertEqual(len(encoded), 65536)
        legal = self.archive(payload, method=8, compressed=encoded)
        with zipfile.ZipFile(io.BytesIO(legal)) as archive:
            self.assertEqual(archive.read("a.bin"), payload)
        self.assertEqual(inspect_archive(legal).total_uncompressed_size, len(payload))
        with self.assertRaises(ArchiveInspectionError) as caught:
            inspect_archive(self.archive(payload, method=8, compressed=encoded + b"HIDDEN"))
        self.assertEqual(caught.exception.code, ArchiveErrorCode.LENGTH_MISMATCH)

    def test_signed_and_unsigned_descriptors_ignore_payload_signatures(self):
        payload = b"prefixPK\x07\x08" + b"x" * 20
        for method in (0, 8):
            encoder = zlib.compressobj(wbits=-15)
            encoded = payload if method == 0 else encoder.compress(payload) + encoder.flush()
            for signed in (False, True):
                with self.subTest(method=method, signed=signed):
                    data = self.archive(payload, method=method, compressed=encoded, descriptor=True, signed=signed)
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        self.assertEqual(archive.read("a.bin"), payload)
                    entry = inspect_archive(data).entries[0]
                    self.assertEqual((entry.size, entry.compressed_size), (len(payload), len(encoded)))

    def test_descriptor_metadata_must_match_central(self):
        for field_offset in (0, 4, 8):
            data = bytearray(self.archive(descriptor=True))
            descriptor = data.index(b"PK\x07\x08") + 4
            struct.pack_into("<I", data, descriptor + field_offset, 999)
            with self.assertRaises(ArchiveInspectionError):
                inspect_archive(data)

    def test_central_directory_extent_count_and_disk_fields_checked(self):
        for offset, fmt, value in ((12, "<I", 0), (10, "<H", 0), (4, "<H", 1), (16, "<I", 0)):
            with self.subTest(offset=offset):
                data = bytearray(self.archive())
                struct.pack_into(fmt, data, len(data) - 22 + offset, value)
                with self.assertRaises(ArchiveInspectionError):
                    inspect_archive(data)

    def test_gap_and_duplicate_local_offsets_rejected(self):
        data = bytearray(_build_zip(lambda archive: (archive.writestr("a", b"x"), archive.writestr("b", b"y"))))
        central = _central_dir_offset(data)
        struct.pack_into("<I", data, central + 47 + 42, 0)
        with self.assertRaises(ArchiveInspectionError):
            inspect_archive(data)
        data = bytearray(self.archive())
        central = _central_dir_offset(data)
        # Insert a gap between the payload and central directory, maintaining
        # the end-record directory offset so only exact local spans catch it.
        data[central:central] = b"PK\x03\x04JUNK"
        struct.pack_into("<I", data, len(data) - 6, central + 8)
        with self.assertRaises(ArchiveInspectionError):
            inspect_archive(data)

    def test_truncated_deflate_with_matching_forged_length_rejected(self):
        payload = b"hello" * 100
        encoder = zlib.compressobj(wbits=-15)
        encoded = encoder.compress(payload) + encoder.flush()
        with self.assertRaises(ArchiveInspectionError) as caught:
            inspect_archive(self.archive(payload, method=8, compressed=encoded[:-1]))
        self.assertEqual(caught.exception.code, ArchiveErrorCode.LENGTH_MISMATCH)

    def test_decoder_output_limited_before_expansion(self):
        from unittest.mock import patch
        payload = b"x" * (2 * 1024 * 1024)
        encoder = zlib.compressobj(wbits=-15)
        encoded = encoder.compress(payload) + encoder.flush()
        data = bytearray(self.archive(payload, method=8, compressed=encoded))
        # Both headers lie consistently; only decoded byte accounting catches
        # this. Instrument the real decoder to verify max_length, not timing.
        central = _central_dir_offset(data)
        struct.pack_into("<I", data, 22, 0)
        struct.pack_into("<I", data, central + 24, 0)
        factory = zlib.decompressobj
        limits_seen = []
        outputs_seen = []

        class Decoder:
            def __init__(self, *args):
                self.inner = factory(*args)

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def decompress(self, source, max_length=0):
                limits_seen.append(max_length)
                self_test.assertGreater(max_length, 0)
                self_test.assertLessEqual(max_length, 1025)
                output = self.inner.decompress(source, max_length)
                outputs_seen.append(len(output))
                return output

        self_test = self
        with patch("zlib.decompressobj", Decoder):
            with self.assertRaises(ArchiveInspectionError) as caught:
                inspect_archive(data, limits=ArchiveLimits(entry_uncompressed_bytes=1024))
        self.assertEqual(caught.exception.code, ArchiveErrorCode.ENTRY_TOO_LARGE)
        self.assertEqual(outputs_seen, [1025])
        self.assertTrue(limits_seen)

    def test_legal_cp437_utf8_names_and_comment_signature(self):
        for raw_name, flags, decoded in ((b"caf\x82.txt", 0, "café.txt"), ("雪.txt".encode(), 0x800, "雪.txt")):
            data = self.archive(name=raw_name, flags=flags)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                self.assertEqual(archive.read(decoded), b"payload")
            self.assertEqual(inspect_archive(data).entries[0].name, decoded)
        data = bytearray(self.archive())
        comment = b"comment with PK\x05\x06 in it"
        struct.pack_into("<H", data, len(data) - 2, len(comment))
        data.extend(comment)
        self.assertEqual(inspect_archive(data).total_entries, 1)

    def test_directory_crc_and_content_are_checked(self):
        data = bytearray(self.archive(b"", name=b"dir/"))
        central = _central_dir_offset(data)
        struct.pack_into("<I", data, 14, 1)
        struct.pack_into("<I", data, central + 16, 1)
        with self.assertRaises(ArchiveInspectionError) as caught:
            inspect_archive(data)
        self.assertEqual(caught.exception.code, ArchiveErrorCode.CRC_MISMATCH)
        with self.assertRaises(ArchiveInspectionError):
            inspect_archive(self.archive(b"hidden", name=b"dir/"))

    def test_standard_writer_streaming_zip_is_accepted(self):
        class Unseekable(io.BytesIO):
            def seekable(self):
                return False

            def seek(self, *args):
                raise io.UnsupportedOperation()

        for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            output = Unseekable()
            with zipfile.ZipFile(output, "w", compression=method) as archive:
                archive.writestr("folder/", b"")
                archive.writestr("folder/data", b"PK\x07\x08" * 17)
            data = output.getvalue()
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                self.assertEqual(archive.read("folder/data"), b"PK\x07\x08" * 17)
            result = inspect_archive(data)
            self.assertEqual(result.total_entries, 2)
            self.assertEqual(result.total_uncompressed_size, 68)


if __name__ == "__main__":
    unittest.main()
