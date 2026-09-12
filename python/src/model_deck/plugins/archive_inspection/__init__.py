"""Pure in-memory plugin archive inspection.

Public surface is intentionally small:

- :class:`ArchiveErrorCode` and :class:`ArchiveInspectionError` for stable
  error reporting.
- :class:`ArchiveEntry` and :class:`ArchiveInspectionResult` as detached,
  immutable value types.
- :class:`ArchiveLimits` for caller-overridable budgets, plus the
  ``DEFAULT_*`` constants that document the default budget.
- :func:`inspect_archive` as the only entry point.

The inspector relies only on the standard library (``io``, ``stat``,
``zipfile``, ``zlib``, :mod:`dataclasses`). It performs no extraction, no
filesystem access, no network call, no subprocess invocation, and no
manifest execution.
"""
from .inspect import (
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

__all__ = [
    "DEFAULT_ARCHIVE_BYTES",
    "DEFAULT_COMPRESSION_RATIO",
    "DEFAULT_ENTRY_COUNT",
    "DEFAULT_ENTRY_UNCOMPRESSED_BYTES",
    "DEFAULT_TOTAL_UNCOMPRESSED_BYTES",
    "ArchiveEntry",
    "ArchiveErrorCode",
    "ArchiveInspectionError",
    "ArchiveInspectionResult",
    "ArchiveLimits",
    "inspect_archive",
]
