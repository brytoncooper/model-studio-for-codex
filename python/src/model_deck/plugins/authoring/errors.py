"""Stable error codes for the B23 authoring helpers.

Codes are part of the public contract and must not change shape without
coordinated caller updates. Detail messages may evolve.
"""
from __future__ import annotations

from dataclasses import dataclass


class AuthoringErrorCode:
    """Stable string codes returned on :class:`AuthoringError`."""

    INPUT_TOO_LARGE = "input_too_large"
    INPUT_NOT_READABLE = "input_not_readable"
    INPUT_NOT_A_DIRECTORY = "input_not_a_directory"
    INPUT_SYMLINK_OR_SPECIAL = "input_symlink_or_special"
    INPUT_OUT_OF_BUDGET = "input_out_of_budget"
    OUTPUT_NOT_ABSOLUTE = "output_not_absolute"
    OUTPUT_NESTED_IN_INPUT = "output_nested_in_input"
    OUTPUT_EXISTS = "output_exists"
    OUTPUT_UNWRITABLE = "output_unwritable"
    MISSING_MANIFEST = "missing_manifest"
    MANIFEST_READ_FAILED = "manifest_read_failed"
    ENTRYPOINT_FILE_MISSING = "entrypoint_file_missing"


@dataclass(frozen=True)
class AuthoringError(Exception):
    """Raised when an authoring helper cannot complete safely.

    Attributes:
        code: Stable error code from :class:`AuthoringErrorCode`.
        detail: Human-readable detail; safe for logs, not for user copy.
        field: Optional relative path that triggered the defect.
    """

    code: str
    detail: str
    field: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.field is None:
            return f"{self.code}: {self.detail}"
        return f"{self.code} ({self.field}): {self.detail}"
