from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Pattern, TypeVar

from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

from model_deck.engine.hosts.ports import (
    HostIntegrationError,
    HostIntegrationPort,
)


LIST_RESULT_SCHEMA = "contracts/engine.v1/methods/hosts.list.result.schema.json"
PREPARE_RESULT_SCHEMA = "contracts/engine.v1/methods/hosts.prepare.result.schema.json"


_REVERSE_DOMAIN_PATTERN: Pattern[str] = re.compile(
    r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9_-]*)+$"
)
_HostResultError = TypeVar("_HostResultError", bound=Exception)


class HostOperationsService:
    """Engine-side service for ``engine.v1.hosts.list`` and ``engine.v1.hosts.prepare``.

    Validates params against the frozen JSON schemas, delegates to the
    injected :class:`HostIntegrationPort` for actual host enumeration and
    pure launch preparation, and wraps results in schema-valid envelopes.

    Domain errors raised by the integration port are translated into the
    ``ListError`` / ``PrepareError`` exception classes below; both carry a
    ``.code`` attribute (``"version_mismatch"``, ``"conflict"``,
    ``"not_found"``, ``"unsupported_capability"``) that the dispatch
    layer copies into a JSON-RPC domain error response.
    """

    class ListError(Exception):
        """Translation of a host integration error for ``engine.v1.hosts.list``."""

        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    class PrepareError(Exception):
        """Translation of a host integration error for ``engine.v1.hosts.prepare``."""

        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    def __init__(self, integration: HostIntegrationPort) -> None:
        self._integration = integration

    def list_hosts(self) -> list[dict[str, str]]:
        """Return one descriptor per supported host.

        Translates any :class:`HostIntegrationError` raised by the
        integration into a ``ListError`` carrying the matching ``code``.
        Validates each returned descriptor has a well-formed ``host_id``
        (reverse-domain) and a non-empty ``api_profile``.
        """
        try:
            descriptors = self._integration.list_hosts()
        except HostIntegrationError as exc:
            raise self.ListError(code=exc.code, message=str(exc)) from exc
        if not isinstance(descriptors, list):
            raise self.ListError(
                code="internal",
                message="host integration returned a non-list result",
            )
        result: list[dict[str, str]] = []
        for entry in descriptors:
            if not isinstance(entry, dict):
                raise self.ListError(
                    code="unsupported_capability",
                    message=f"host integration returned a non-dict entry: {entry!r}",
                )
            host_id = entry.get("host_id")
            api_profile = entry.get("api_profile")
            if not isinstance(host_id, str) or not _REVERSE_DOMAIN_PATTERN.match(host_id):
                raise self.ListError(
                    code="unsupported_capability",
                    message=f"host integration returned malformed descriptor: {entry!r}",
                )
            if not isinstance(api_profile, str) or not api_profile:
                raise self.ListError(
                    code="unsupported_capability",
                    message=f"host integration returned malformed descriptor: {entry!r}",
                )
            result.append({"host_id": host_id, "api_profile": api_profile})
        return result

    def list_hosts_envelope(self) -> dict[str, Any]:
        """Return a schema-valid result envelope for ``engine.v1.hosts.list``."""
        return self._checked_result(
            LIST_RESULT_SCHEMA,
            {"hosts": self.list_hosts()},
            self.ListError,
        )

    def prepare(self, host_id: str) -> bool:
        """Validate ``host_id`` shape, then delegate to the integration port.

        Translates integration errors into ``PrepareError`` with the
        matching ``code``. A ``False`` return from the integration port
        is treated as ``not_found`` so adapters that prefer signalling
        via return value rather than exception still produce the
        contractually consistent error code.
        """
        if not isinstance(host_id, str) or not _REVERSE_DOMAIN_PATTERN.match(host_id):
            raise self.PrepareError(
                code="unsupported_capability",
                message=f"unsupported host_id: {host_id!r}",
            )
        try:
            prepared = self._integration.prepare(host_id)
        except HostIntegrationError as exc:
            raise self.PrepareError(code=exc.code, message=str(exc)) from exc
        if not prepared:
            raise self.PrepareError(
                code="not_found",
                message=f"unknown host_id: {host_id!r}",
            )
        return True

    def prepare_envelope(self, host_id: str) -> dict[str, Any]:
        """Return a schema-valid result envelope for ``engine.v1.hosts.prepare``."""
        return self._checked_result(
            PREPARE_RESULT_SCHEMA,
            {"prepared": self.prepare(host_id)},
            self.PrepareError,
        )

    def _checked_result(
        self,
        schema: str,
        body: dict[str, Any],
        error_type: Callable[..., _HostResultError],
    ) -> dict[str, Any]:
        try:
            validate_schema_ref(schema, body)
        except SchemaValidationError as exc:
            raise error_type(
                code="internal",
                message=f"host result envelope rejected: {exc}",
            ) from exc
        return body
