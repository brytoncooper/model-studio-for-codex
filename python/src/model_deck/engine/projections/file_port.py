from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

PROJECTION_FILE_OPERATION_WRITE = "write"
PROJECTION_FILE_OPERATION_DELETE = "delete"

PROJECTION_FILE_OUTCOME_APPLIED = "applied"
PROJECTION_FILE_OUTCOME_NOOP = "noop"
PROJECTION_FILE_OUTCOME_CONFLICT = "conflict"

PROJECTION_FILE_REASON_MISSING = "missing"
PROJECTION_FILE_REASON_UNEXPECTED_EXISTING = "unexpected_existing"
PROJECTION_FILE_REASON_HASH_MISMATCH = "hash_mismatch"
PROJECTION_FILE_REASON_SYMLINK = "symlink"
PROJECTION_FILE_REASON_FOREIGN_OWNER = "foreign_owner"
PROJECTION_FILE_REASON_POSTWRITE_INTERFERENCE = "postwrite_interference"
PROJECTION_FILE_REASON_POSTDELETE_INTERFERENCE = "postdelete_interference"


@dataclass(frozen=True, slots=True)
class ProjectionFileReceipt:
    """Content-free result of one conditional projection-file operation."""

    operation: str
    path: Path
    outcome: str
    expected_sha256: str | None
    observed_sha256: str | None
    result_sha256: str | None
    reason: str | None


@runtime_checkable
class ConditionalProjectionFiles(Protocol):
    """Compare-before-mutate boundary for consumer-owned projection files."""

    def compare_and_write(
        self,
        path: Path,
        data: bytes,
        expected_sha256: str | None,
    ) -> ProjectionFileReceipt:
        """Conditionally replace one owned file with ``data``."""
        ...

    def compare_and_delete(
        self,
        path: Path,
        expected_sha256: str | None,
    ) -> ProjectionFileReceipt:
        """Conditionally delete one owned file."""
        ...
