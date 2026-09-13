"""Bounded plugin archive authoring helpers for B23.

This package owns two narrow responsibilities:

- :func:`validate_project_archive` reuses :mod:`model_deck.plugins.archive_inspection`
  and :mod:`model_deck.plugins.manifest_inspection` to confirm a packed plugin
  archive is safe to load and its embedded ``manifest.json`` matches the
  bundled ``contracts/plugin.v1`` schema. It performs no extraction, no
  plugin execution, and no engine import.
- :func:`pack_project_archive` reads a bounded project tree once and produces a
  deterministic ZIP archive that satisfies the same contracts before atomic
  no-overwrite publication.

Both helpers are usable directly from Python; the CLI wraps them with
fixed exit codes and stdout/stderr discipline.
"""
from __future__ import annotations

from .errors import AuthoringError, AuthoringErrorCode
from .packing import PackLimits, PackResult, pack_project_archive
from .validation import (
    DEFAULT_CALLER_PLUGIN_API_MAJOR,
    DEFAULT_CALLER_PLUGIN_API_MINOR,
    ValidationReport,
    entrypoint_present,
    validate_project_archive,
)

__all__ = [
    "AuthoringError",
    "AuthoringErrorCode",
    "DEFAULT_CALLER_PLUGIN_API_MAJOR",
    "DEFAULT_CALLER_PLUGIN_API_MINOR",
    "PackLimits",
    "PackResult",
    "ValidationReport",
    "entrypoint_present",
    "pack_project_archive",
    "validate_project_archive",
]
