"""Pure Codex settings document operations and injected descriptor types."""
from .document import (
    DocumentContext, DocumentError, EntrySpec, FieldSpec, ProtectedDeniedError,
    SectionSpec, SourceTooLargeError, assert_save_allowed, preview_candidate,
    read_snapshot, sha256_hex, validate_candidate,
)

__all__ = [
    "DocumentContext", "DocumentError", "EntrySpec", "FieldSpec", "ProtectedDeniedError",
    "SectionSpec", "SourceTooLargeError", "assert_save_allowed", "preview_candidate",
    "read_snapshot", "sha256_hex", "validate_candidate",
]
