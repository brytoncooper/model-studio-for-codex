"""Bounded default network transport for the evidence adapters.

`prices.py` and `benchmarks.py` never import `urllib` themselves and never fetch on their
own initiative — every fetch goes through a `Callable[[str], bytes]` the caller injects.
This module is only the default implementation of that callable, wired in explicitly by
whatever composes the adapters (bootstrap, or a test). Adapters layer code is allowed to
use `urllib` (development/architecture/forbidden.py only forbids it for kernel/engine).
"""
from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
USER_AGENT = "ModelDeck-Evidence/1"


class EvidenceTransportError(Exception):
    """The transport failed to produce bytes: network, timeout, HTTP status, or size cap."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 - urllib's signature
        raise EvidenceTransportError("Evidence source redirected; redirects are not followed.")


def build_urllib_transport(
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    user_agent: str = USER_AGENT,
) -> Callable[[str], bytes]:
    """A `Callable[[str], bytes]` transport: fixed timeout, size cap, no redirects followed."""
    opener = urllib.request.build_opener(_NoRedirectHandler)

    def transport(url: str) -> bytes:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": user_agent},
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(max_bytes + 1)
        except EvidenceTransportError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise EvidenceTransportError(f"Evidence source request failed: {error}") from error
        if len(raw) > max_bytes:
            raise EvidenceTransportError("Evidence source response exceeded the size cap.")
        return raw

    return transport


# A ready-to-use default: same bounds as `build_urllib_transport()`'s own defaults.
default_transport: Callable[[str], bytes] = build_urllib_transport()
