"""Stable error codes for plugin manifest inspection.

Codes are part of the inspection public contract and must not change shape
without coordinated caller updates. Detail messages may evolve.
"""
from __future__ import annotations

from dataclasses import dataclass


class InspectionErrorCode:
    """Stable string codes returned on ``InspectionError``.

    Each code is a stable identifier; callers branch on these strings.
    """

    SCHEMA_INVALID = "schema_invalid"
    ENTRYPOINT_PATH_INVALID = "entrypoint_path_invalid"
    DUPLICATE_CONTRIBUTION = "duplicate_contribution"
    INCOMPATIBLE_API_VERSION = "incompatible_api_version"


@dataclass(frozen=True)
class InspectionError(Exception):
    """Raised when a manifest fails inspection.

    Attributes:
        code: Stable error code from ``InspectionErrorCode``.
        detail: Human-readable detail; safe for logs, not for user copy.
        field: Optional JSON-path-style location, e.g. ``"entrypoint.path"``.
    """

    code: str
    detail: str
    field: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.field is None:
            return f"{self.code}: {self.detail}"
        return f"{self.code} ({self.field}): {self.detail}"
