# Plugin archive inspection

This B20 primitive inspects ZIP bytes in memory and returns an immutable
entry summary. It does not extract files, inspect manifests, access credentials,
execute code, or perform filesystem/network/process operations.

## Ownership and public contract

The package owns `inspect.py`, its public exports, and
`python/tests/plugins/test_archive_inspection.py`. Dependencies are Python's
standard library only. Manifest validation, installation, and extraction are
separate systems; passing inspection alone does not authorize installation.

- `inspect_archive(archive_bytes, *, limits=None)` accepts bytes, bytearray,
  or memoryview and returns `ArchiveInspectionResult`.
- `ArchiveInspectionResult.entries` is a tuple of frozen `ArchiveEntry` values
  in central-directory order. Each entry exposes `name`, decoded `size`, actual
  declared-and-verified `compressed_size`, and `is_dir`. An empty DEFLATE
  directory has zero decoded bytes but nonzero compressed bytes.
- `total_entries`, `total_uncompressed_size`, and `ok` describe accepted entries.
- `ArchiveLimits` validates strict positive integers, rejecting booleans.
  Defaults are 64 MiB archive, 64 MiB per entry, 256 MiB total decoded bytes,
  4096 entries, and a declared compression ratio of 1000.
- `ArchiveInspectionError.code` uses the existing `ArchiveErrorCode` strings:
  `archive_too_large`, `not_a_zip`, `unsupported_compression`, `encrypted_entry`,
  `symlink_or_special_entry`, `invalid_path`, `duplicate_path`,
  `file_parent_collision`, `entry_too_large`, `too_many_entries`,
  `total_uncompressed_too_large`, `compression_ratio_exceeded`, `crc_mismatch`,
  and `length_mismatch`.

## Validation invariants

`zipfile.ZipFile` parses central metadata; its extraction reader is never used.
The inspector separately validates the end record's count and exact central
extent, the complete sequence of central records, and each local header.
Local and central names, format flags, compression, sizes, and CRC must agree.
Streaming descriptors may replace zero local size/CRC fields, but must agree
with central metadata. Unsupported ZIP64 extra fields are rejected.

Physical entry spans must be contiguous, nonoverlapping, and end exactly at the
next local header or central directory. The compressed-data end comes from the
validated central size, never from searching compressed payloads for signatures.
Descriptors are parsed at that exact end: both signed 16-byte and unsigned
12-byte forms are supported, including payloads containing signature bytes.

STORED data is consumed in bounded slices. DEFLATE uses raw zlib with a positive
`max_length` on every decode call, capped at 64 KiB and remaining per-entry/total
budgets plus one rejection sentinel byte. No unbounded `flush()` is used.
The decoder must reach EOF, consume the entire compressed span (including bytes
not yet fed), and leave no extra stream data. Actual length and CRC must match
central metadata for files and directories. Directory output must be empty.
Compressed source and central metadata are held within the archive input budget;
the decoded entry is never accumulated as a complete output buffer.

Paths reject empty names, NUL, backslashes, absolute/drive prefixes, and empty,
`.` or `..` segments. Raw names decode as UTF-8 when flagged or CP437 otherwise.
Casefold-equivalent names collide. Files cannot be ancestors of any other entry,
regardless of entry order. Unix types must be regular files/directories or
unspecified; explicit directory type and trailing slash must agree.

## Supported scope and limitations

Only single-disk, non-ZIP64 STORED/DEFLATED ZIPs are supported. Encryption,
unsupported flags/versions, self-extracting preambles, inter-record padding,
central signatures, and trailing junk are rejected. ZIP comments and bounded
non-ZIP64 extra fields are accepted. This is intentionally narrower than every
archive accepted by Python's ZIP reader. Existing format-version policy caps
extract/create versions at 63 and creator systems at 14; extending this policy
requires new fixtures and review.

Inspection does not supply safe extraction. A future extractor must preserve
these path/span checks and separately enforce destination ownership, symlink,
and atomic-write policies. No cryptographic authenticity guarantee is made.

## Extension and verification

Keep public types/error codes stable. Add format support only with positive
fixtures checked against Python's ZIP reader and adversarial metadata/span
fixtures that fail closed. Budget changes must preserve bounded decoder output.

From the Architecture `python/` directory, with its dependencies available:

```sh
PYTHONPATH=src python -B -m unittest discover -s tests/plugins -p test_archive_inspection.py
```

The focused suite covers existing path/mode/budget failures, hidden payloads,
local/central disagreement, truncation, exact DEFLATE EOF boundaries, signed and
unsigned descriptors, output-cap instrumentation, UTF-8/CP437 names, and real
streaming archives generated by an unseekable standard-library writer.
