"""Pure in-memory plugin lifecycle session state.

The session walks one supervised plugin activation through
``created -> hello_verified -> active -> draining -> inactive`` with a
terminal ``failed`` state. It prepares outbound ``plugin.v1`` lifecycle
payloads and accepts inbound results, validating both directions against
the frozen ``contracts/plugin.v1/lifecycle`` schemas through the bundled
``model_deck_contracts`` validator.

Wire-shape note: ``activate.params`` carries only ``activation_token``,
``allowed_broker_methods`` and optional ``config_revision``. It has no
identity fields, no generic context object and no resource-limits field,
so plugin identity is bound session-side from the verified hello result
and any richer context must travel out-of-band. This module does not
invent wire fields to fill those gaps.

The session performs no execution, network access, filesystem access,
clock reads or global-state access. The optional injected ``clock`` is
retained for future deadline use and is never called by this revision.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Callable

from model_deck_contracts.validator import (
    SchemaValidationError,
    validate_schema_ref,
)

from .errors import SessionError, SessionErrorCode

_HELLO_PARAMS_REF = "contracts/plugin.v1/lifecycle/hello.params.schema.json"
_HELLO_RESULT_REF = "contracts/plugin.v1/lifecycle/hello.result.schema.json"
_ACTIVATE_PARAMS_REF = "contracts/plugin.v1/lifecycle/activate.params.schema.json"
_ACTIVATE_RESULT_REF = "contracts/plugin.v1/lifecycle/activate.result.schema.json"
_CANCEL_PARAMS_REF = "contracts/plugin.v1/lifecycle/cancel.params.schema.json"
_DRAIN_PARAMS_REF = "contracts/plugin.v1/lifecycle/drain.params.schema.json"
_DRAIN_RESULT_REF = "contracts/plugin.v1/lifecycle/drain.result.schema.json"

_NONCE_MAX_LENGTH = 128


class SessionState:
    """Lifecycle state values. Ordering is fixed by the activation flow."""

    CREATED = "created"
    HELLO_VERIFIED = "hello_verified"
    ACTIVE = "active"
    DRAINING = "draining"
    INACTIVE = "inactive"
    FAILED = "failed"


_TERMINAL_STATES = frozenset({SessionState.INACTIVE, SessionState.FAILED})


def _checked_outbound(
    schema_ref: str, payload: dict[str, Any], *, sensitive: bool = False
) -> dict[str, Any]:
    try:
        validate_schema_ref(schema_ref, payload)
    except SchemaValidationError:
        raise SessionError(
            code=SessionErrorCode.SCHEMA_INVALID,
            detail=f"outbound payload failed {schema_ref}",
        ) from None
    return copy.deepcopy(payload)


class LifecycleSession:
    """Tracks one plugin activation from hello through deactivation.

    Args:
        expected_plugin_id: Reverse-domain id the hello result must report.
        expected_plugin_version: Exact version string the hello result must
            report.
        offered_api_major: Plugin API major offered in the hello request.
        offered_api_minor: Plugin API minor offered in the hello request.
        activation_token: Opaque activation-scoped token sent verbatim in
            the activation request. Treated as sensitive: never exposed via
            ``repr``, errors or detached outputs other than the activation
            request itself.
        allowed_broker_methods: Broker method names the activation grants.
        config_revision: Immutable configuration revision sent with the
            activation request when not ``None``.
        clock: Optional injected monotonic clock for future deadline use.
            Never called by this revision.
    """

    def __init__(
        self,
        *,
        expected_plugin_id: str,
        expected_plugin_version: str,
        offered_api_major: int,
        offered_api_minor: int,
        activation_token: str,
        allowed_broker_methods: tuple[str, ...] | list[str],
        config_revision: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(expected_plugin_id, str) or not expected_plugin_id:
            raise SessionError(
                code=SessionErrorCode.SCHEMA_INVALID,
                detail="expected_plugin_id must be a non-empty string",
            )
        if not isinstance(expected_plugin_version, str) or not expected_plugin_version:
            raise SessionError(
                code=SessionErrorCode.SCHEMA_INVALID,
                detail="expected_plugin_version must be a non-empty string",
            )
        if isinstance(activation_token, str) and activation_token == "":
            raise SessionError(
                code=SessionErrorCode.SCHEMA_INVALID,
                detail="activation_token must be a non-empty string",
            )
        if not isinstance(activation_token, str):
            raise SessionError(
                code=SessionErrorCode.SCHEMA_INVALID,
                detail="activation_token must be a string",
            )
        methods = tuple(allowed_broker_methods)
        if any(not isinstance(m, str) for m in methods):
            raise SessionError(
                code=SessionErrorCode.SCHEMA_INVALID,
                detail="allowed_broker_methods must all be strings",
            )
        if len(activation_token) > 512:
            raise SessionError(
                code=SessionErrorCode.SCHEMA_INVALID,
                detail="activation_token exceeds maximum length",
            )
        if len(methods) > 64:
            raise SessionError(
                code=SessionErrorCode.SCHEMA_INVALID,
                detail="allowed_broker_methods exceeds maximum count",
            )
        if any(len(m) > 128 for m in methods):
            raise SessionError(
                code=SessionErrorCode.SCHEMA_INVALID,
                detail="allowed_broker_methods entry exceeds maximum length",
            )
        if config_revision is not None and (
            isinstance(config_revision, bool)
            or not isinstance(config_revision, int)
            or config_revision < 0
        ):
            raise SessionError(
                code=SessionErrorCode.SCHEMA_INVALID,
                detail="config_revision must be a non-negative integer",
            )
        self._expected_plugin_id = expected_plugin_id
        self._expected_plugin_version = expected_plugin_version
        self._offered_api_major = offered_api_major
        self._offered_api_minor = offered_api_minor
        self._activation_token = activation_token
        self._allowed_broker_methods = methods
        self._config_revision = config_revision
        self._clock = clock
        self._state = SessionState.CREATED
        self._activation_prepared = False
        self._hello_nonce: str | None = None
        self._activation_id: str | None = None
        self._invocation_prefix = ""
        self._admission_counter = 0
        self._outstanding: dict[str, str] = {}
        self._failure: SessionError | None = None

    def __repr__(self) -> str:
        return (
            f"LifecycleSession(plugin_id={self._expected_plugin_id!r}, "
            f"state={self._state!r})"
        )

    @property
    def state(self) -> str:
        """Current lifecycle state value."""
        return self._state

    @property
    def activation_id(self) -> str | None:
        """Activation id reported by the worker, once active."""
        return self._activation_id

    @property
    def outstanding(self) -> tuple[str, ...]:
        """Invocation ids admitted but not yet resolved or cancelled."""
        return tuple(self._outstanding)

    @property
    def failure(self) -> SessionError | None:
        """Terminal failure detail once the session has failed."""
        return self._failure

    def _fail(self, code: str, detail: str) -> SessionError:
        self._state = SessionState.FAILED
        self._failure = SessionError(code=code, detail=detail, state=SessionState.FAILED)
        return self._failure

    def _require_state(self, *allowed: str, action: str) -> None:
        if self._state in _TERMINAL_STATES:
            raise SessionError(
                code=SessionErrorCode.TERMINAL,
                detail=f"{action} is not allowed in terminal state {self._state}",
                state=self._state,
            )
        if self._state not in allowed:
            raise SessionError(
                code=SessionErrorCode.OUT_OF_ORDER,
                detail=f"{action} requires state {allowed}, current is {self._state}",
                state=self._state,
            )

    def prepare_hello_request(self, nonce: str) -> dict[str, Any]:
        """Build the detached ``hello.params`` payload for this session."""
        self._require_state(SessionState.CREATED, action="prepare_hello_request")
        if self._hello_nonce is not None:
            raise SessionError(
                code=SessionErrorCode.REPLAY,
                detail="hello request was already prepared",
                state=self._state,
            )
        if not isinstance(nonce, str) or not nonce or len(nonce) > _NONCE_MAX_LENGTH:
            raise SessionError(
                code=SessionErrorCode.SCHEMA_INVALID,
                detail="nonce must be a non-empty string within schema length",
                state=self._state,
            )
        self._hello_nonce = nonce
        return _checked_outbound(
            _HELLO_PARAMS_REF,
            {
                "offered_api": {
                    "major": self._offered_api_major,
                    "minor": self._offered_api_minor,
                },
                "nonce": nonce,
            },
        )

    def accept_hello_result(self, result: Any) -> None:
        """Accept the worker ``hello.result`` after identity verification."""
        self._require_state(SessionState.CREATED, action="accept_hello_result")
        if self._hello_nonce is None:
            raise SessionError(
                code=SessionErrorCode.OUT_OF_ORDER,
                detail="hello request must be prepared before accepting a result",
                state=self._state,
            )
        if not isinstance(result, Mapping):
            raise self._fail(
                SessionErrorCode.SCHEMA_INVALID,
                "hello result must be a JSON object",
            )
        document = copy.deepcopy(dict(result))
        try:
            validate_schema_ref(_HELLO_RESULT_REF, document)
        except SchemaValidationError as exc:
            raise self._fail(
                SessionErrorCode.SCHEMA_INVALID,
                "hello result failed schema validation",
            ) from None
        if (
            document.get("plugin_id") != self._expected_plugin_id
            or document.get("plugin_version") != self._expected_plugin_version
        ):
            raise self._fail(
                SessionErrorCode.IDENTITY_MISMATCH,
                "hello result identity does not match the expected plugin",
            )
        self._state = SessionState.HELLO_VERIFIED

    def prepare_activation_request(self) -> dict[str, Any]:
        """Build the detached ``activate.params`` payload for this session."""
        self._require_state(SessionState.HELLO_VERIFIED, action="prepare_activation_request")
        payload: dict[str, Any] = {
            "activation_token": self._activation_token,
            "allowed_broker_methods": list(self._allowed_broker_methods),
        }
        if self._config_revision is not None:
            payload["config_revision"] = self._config_revision
        request = _checked_outbound(_ACTIVATE_PARAMS_REF, payload, sensitive=True)
        self._activation_prepared = True
        return request

    def accept_activation_result(self, result: Any) -> None:
        """Accept the worker ``activate.result`` and enter ``active``."""
        self._require_state(SessionState.HELLO_VERIFIED, action="accept_activation_result")
        if not self._activation_prepared:
            raise SessionError(
                code=SessionErrorCode.OUT_OF_ORDER,
                detail="activation request must be prepared before accepting a result",
                state=self._state,
            )
        if not isinstance(result, Mapping):
            raise self._fail(
                SessionErrorCode.SCHEMA_INVALID,
                "activation result must be a JSON object",
            )
        document = copy.deepcopy(dict(result))
        try:
            validate_schema_ref(_ACTIVATE_RESULT_REF, document)
        except SchemaValidationError as exc:
            raise self._fail(
                SessionErrorCode.SCHEMA_INVALID,
                "activation result failed schema validation",
            ) from None
        activation_id = document["activation_id"]
        if not isinstance(activation_id, str) or not activation_id:
            raise self._fail(
                SessionErrorCode.SCHEMA_INVALID,
                "activation result activation_id must be a non-empty string",
            )
        prefix = document.get("invocation_handle_prefix", "")
        if not isinstance(prefix, str):
            raise self._fail(
                SessionErrorCode.SCHEMA_INVALID,
                "activation result invocation_handle_prefix must be a string",
            )
        self._activation_id = activation_id
        self._invocation_prefix = prefix
        self._state = SessionState.ACTIVE

    def admit_call(self, operation_id: str) -> str:
        """Admit one call while ``active`` and return its invocation id.

        Drain stops new admissions. Outstanding calls are tracked so the
        caller can cancel or resolve each one explicitly; the session never
        executes cancellation itself.
        """
        self._require_state(SessionState.ACTIVE, action="admit_call")
        if not isinstance(operation_id, str) or not operation_id:
            raise SessionError(
                code=SessionErrorCode.SCHEMA_INVALID,
                detail="operation_id must be a non-empty string",
                state=self._state,
            )
        self._admission_counter += 1
        invocation_id = f"{self._invocation_prefix}{self._admission_counter:08d}"
        self._outstanding[invocation_id] = operation_id
        return invocation_id

    def check_call_allowed(self, operation_id: str) -> str:
        """Admit one call and return its invocation id (alias of admit)."""
        return self.admit_call(operation_id)

    def resolve_call(self, invocation_id: str) -> None:
        """Mark an outstanding call completed by its invocation id."""
        self._require_state(
            SessionState.ACTIVE, SessionState.DRAINING, action="resolve_call"
        )
        try:
            del self._outstanding[invocation_id]
        except KeyError:
            raise SessionError(
                code=SessionErrorCode.UNKNOWN_HANDLE,
                detail="unknown invocation id",
                state=self._state,
            ) from None

    def cancel_call(self, invocation_id: str) -> None:
        """Mark an outstanding call cancelled by explicit caller action.

        The session only records the caller action; it never executes the
        cancel against a transport or process.
        """
        self._require_state(
            SessionState.ACTIVE, SessionState.DRAINING, action="cancel_call"
        )
        try:
            del self._outstanding[invocation_id]
        except KeyError:
            raise SessionError(
                code=SessionErrorCode.UNKNOWN_HANDLE,
                detail="unknown invocation id",
                state=self._state,
            ) from None

    def prepare_cancel_request(self, job_id: str) -> dict[str, Any]:
        """Build the detached ``cancel.params`` payload for a worker job id.

        Job ids are issued by the worker (for example via
        ``invoke.result``); the session never mints them and performs no
        outstanding-set membership check here.
        """
        self._require_state(
            SessionState.ACTIVE,
            SessionState.DRAINING,
            action="prepare_cancel_request",
        )
        return _checked_outbound(_CANCEL_PARAMS_REF, {"job_id": job_id})

    def prepare_drain_request(self, deadline_ms: int) -> dict[str, Any]:
        """Enter ``draining`` and build the detached ``drain.params`` payload."""
        self._require_state(SessionState.ACTIVE, action="prepare_drain_request")
        payload = _checked_outbound(_DRAIN_PARAMS_REF, {"deadline_ms": deadline_ms})
        self._state = SessionState.DRAINING
        return payload

    def accept_drain_result(self, result: Any) -> None:
        """Accept the worker ``drain.result`` while remaining ``draining``."""
        self._require_state(SessionState.DRAINING, action="accept_drain_result")
        if not isinstance(result, Mapping):
            raise self._fail(
                SessionErrorCode.SCHEMA_INVALID,
                "drain result must be a JSON object",
            )
        document = copy.deepcopy(dict(result))
        try:
            validate_schema_ref(_DRAIN_RESULT_REF, document)
        except SchemaValidationError as exc:
            raise self._fail(
                SessionErrorCode.SCHEMA_INVALID,
                "drain result failed schema validation",
            ) from None

    def deactivate(self) -> None:
        """Terminate the session from ``active`` or ``draining``."""
        self._require_state(
            SessionState.ACTIVE,
            SessionState.DRAINING,
            action="deactivate",
        )
        self._outstanding.clear()
        self._state = SessionState.INACTIVE

    def fail(self, detail: str) -> SessionError:
        """Record a caller-observed failure and enter terminal ``failed``.

        The caller-supplied ``detail`` is never persisted: error state must
        not retain user-controlled text that could echo sensitive values.
        """
        if self._state in _TERMINAL_STATES:
            raise SessionError(
                code=SessionErrorCode.TERMINAL,
                detail="fail is not allowed in a terminal state",
                state=self._state,
            )
        if not isinstance(detail, str):
            raise SessionError(
                code=SessionErrorCode.SCHEMA_INVALID,
                detail="fail detail must be a string",
                state=self._state,
            )
        return self._fail(SessionErrorCode.TERMINAL, "caller-reported failure")
