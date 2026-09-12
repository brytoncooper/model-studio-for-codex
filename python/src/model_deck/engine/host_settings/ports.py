from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

READ_GRANT = "hosts.settings.read"
WRITE_GRANT = "hosts.settings.write"


@dataclass(frozen=True, slots=True)
class CallerContext:
    """Server-assigned caller identity and grants for one settings call."""

    principal: str
    grants: frozenset[str]


@dataclass(frozen=True, slots=True)
class PreviewRecord:
    """Engine-owned binding for one issued preview token."""

    preview_id: str
    principal: str
    host_id: str
    document_id: str
    base_content_hash: str
    context_revision: str
    candidate_content_hash: str


@runtime_checkable
class SettingsDocumentPort(Protocol):
    """Host adapter: parsing, protection, and lossless writes live here."""

    def read(self, host_id: str) -> dict[str, Any]: ...

    def validate(
        self,
        host_id: str,
        document_id: str,
        expected_content_hash: str,
        context_revision: str,
        draft: dict[str, Any],
    ) -> dict[str, Any]: ...

    def preview(
        self,
        host_id: str,
        document_id: str,
        expected_content_hash: str,
        context_revision: str,
        draft: dict[str, Any],
    ) -> dict[str, Any]: ...

    def save(
        self,
        host_id: str,
        document_id: str,
        expected_content_hash: str,
        context_revision: str,
        candidate_content_hash: str,
        candidate_raw_toml: str,
    ) -> dict[str, Any]: ...


@runtime_checkable
class PreviewStorePort(Protocol):
    """Atomic single-use admission ledger for preview tokens."""

    def admit(self, record: PreviewRecord) -> None: ...

    def lookup(self, preview_id: str) -> PreviewRecord | None: ...

    def is_consumed(self, preview_id: str) -> bool: ...

    def consume(self, preview_id: str) -> PreviewRecord | None: ...


@dataclass(frozen=True, slots=True)
class SaveClaimBinding:
    """Ledger-persisted save identity: hashes and binding only, never raw TOML."""

    principal: str
    idempotency_key: str
    fingerprint: str
    host_id: str
    document_id: str
    base_content_hash: str
    context_revision: str
    candidate_content_hash: str
    preview_id: str


CLAIM_ADMITTED = "admitted"
CLAIM_REPLAY = "replay"
CLAIM_IN_PROGRESS = "in_progress"


@runtime_checkable
class SaveReceiptStorePort(Protocol):
    """Atomic idempotency receipt ledger for save replays.

    Real ledger must implement claim/settle atomically: concurrent
    identical claims admit exactly one writer; unsettled claims report
    in_progress and are never re-admitted; settled claims replay the
    exact stored result. Binding persists candidate hash for future
    adapter reconciliation; raw TOML is never stored here.
    Crash/write uncertainty: once the adapter save invocation begins,
    the admitted claim stays uncertain until settle() succeeds.
    Preview reservation: claim() is atomic across ALL keys as well as
    principal/key. Once a preview_id is admitted it is reserved to that
    claim: the same key replays (settled) or reports in_progress
    (unsettled); a different key (or principal) raising the same
    preview_id gets ReceiptConflictError. release() before any save
    invocation frees both the key and preview indexes; settle() retains
    the preview reservation permanently (same retention as the receipt)
    so a settled preview can never be re-saved. Restart proof is owned
    by the ledger, never by revalidation alone.
    """

    def lookup(
        self, principal: str, idempotency_key: str, fingerprint: str
    ) -> dict[str, Any] | None: ...

    def store(
        self,
        principal: str,
        idempotency_key: str,
        fingerprint: str,
        result: dict[str, Any],
    ) -> None: ...

    def claim(
        self, binding: SaveClaimBinding
    ) -> tuple[str, dict[str, Any] | None]: ...

    def settle(
        self,
        principal: str,
        idempotency_key: str,
        fingerprint: str,
        result: dict[str, Any],
    ) -> None: ...

    def release(
        self, principal: str, idempotency_key: str, fingerprint: str
    ) -> None: ...


@runtime_checkable
class TokenFactory(Protocol):
    """Injected opaque preview token source."""

    def __call__(self) -> str: ...


class PreviewExistsError(ValueError):
    pass


class ReceiptConflictError(ValueError):
    pass


class SettingsDeniedError(PermissionError):
    """Maps to capability_denied: not the local operator or missing grant."""


class SettingsNotFoundError(LookupError):
    """Maps to not_found: unknown host or document identity."""


class SettingsConflictError(ValueError):
    """Maps to conflict: stale base, changed context, or consumed preview."""

    def __init__(self, message: str, *, reason: str = "conflict") -> None:
        super().__init__(message)
        self.reason = reason


class SettingsVersionMismatchError(ValueError):
    """Maps to version_mismatch: document or schema identity no longer applies."""


class SettingsUnsupportedError(ValueError):
    """Maps to unsupported_capability: no adapter or toml_only restriction."""


class SettingsExhaustedError(ValueError):
    """Maps to resource_exhausted: source or frame exceeds a frozen bound."""


class SettingsInvalidError(ValueError):
    """Maps to invalid_argument: malformed envelope or failed revalidation."""


class SettingsInternalError(RuntimeError):
    """Maps to internal: safe write or backup failure without raw content."""
