from __future__ import annotations

import re
import threading
import uuid
from copy import deepcopy
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from model_deck.engine.projections.coordinator import ProjectionCoordinator

from model_deck_contracts.negotiation import (
    ApiVersion,
    NegotiationFailure,
    evaluate_hello_negotiation,
    rendezvous_matches,
)
from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref
from model_deck.engine.connections.ports import (
    ConnectionIdempotencyConflictError,
    ConnectionRevisionConflictError,
)
from model_deck.engine.connections.use_cases import ListConnectionsUseCase, SaveConnectionUseCase
from model_deck.engine.model_library.ports import (
    CatalogUnavailableError,
    ModelIdempotencyConflictError,
    ModelRegistrationNotFoundError,
    ModelRevisionConflictError,
)
from model_deck.engine.model_library.use_cases import (
    ListModelsUseCase,
    RegisterModelUseCase,
    RemoveModelUseCase,
    RenameModelUseCase,
    UnsupportedCollectionError,
)
from model_deck.engine.routing.ports import (
    RegistrationNotFoundError,
    UnknownCapabilityError,
    UnsupportedCapabilityError,
)
from model_deck.engine.runs.ports import (
    ApplicationRunEvent,
    EventReplayOutcome,
    GetRunCommand,
    ReplaySubscriptionHandle,
    RunAdmissionRequestHashConflictError,
    RunEventReplayPort,
    RunRepository,
    RunAuthorizationMismatchError,
    RunNotFoundError,
    RunStateConflictError,
    ToolCallNotOutstandingError,
    ToolResultIdempotencyConflictError,
)
from model_deck.engine.runs.use_cases import (
    CancelRunUseCase,
    GetRunUseCase,
    StartRunUseCase,
    SubmitToolResultUseCase,
)
from model_deck.engine.sessions.ports import (
    SessionActiveRunConflictError,
    SessionNotFoundError,
    SessionRevisionConflictError,
)
from model_deck.engine.sessions.use_cases import (
    CreateSessionUseCase,
    GetSessionUseCase,
    SelectSessionModelUseCase,
)
from model_deck.engine.host_settings.ports import (
    CallerContext,
    SettingsConflictError,
    SettingsDeniedError,
    SettingsExhaustedError,
    SettingsInternalError,
    SettingsInvalidError,
    SettingsNotFoundError,
    SettingsUnsupportedError,
    SettingsVersionMismatchError,
)
from model_deck.engine.host_settings.service import HostSettingsService
from model_deck.engine.hosts.ports import HostIntegrationPort
from model_deck.engine.hosts.service import HostOperationsService
from model_deck.engine.builtins import BUILTIN_FEATURE_IDS, BuiltinDispatchBinding
from model_deck.engine.kernel_composition import (
    DispatchInvocationContext,
    KernelComposition,
    KernelDomainError,
    KernelInputError,
    KernelInvocationError,
    KernelPassthroughError,
)
from model_deck.kernel import CompositionError, GrantDeniedError
from model_deck.engine.extensions.ports import (
    ExtensionRecord,
    ExtensionStatus,
    ExtensionHostConflictError,
    ExtensionHostUnavailableError,
    ExtensionGateway,
    LifecycleReceipt,
    ReceiptOutcome,
)
from model_deck.engine.jobs.first_party import FirstPartyJobDirectory
from model_deck.engine.jobs.ports import (
    JobResumeConflictError, JobResumeUnsupportedError,
)
from model_deck.engine.jobs.use_cases import (
    JobsNotFoundError, JobsCallerMismatchError, JobsInvalidArgumentError,
    JobsPluginUnavailableError, JobsUnknownKeyError,
)
from model_deck.engine.usage.ports import (
    UsageConflictError, UsageEventMismatchError, UsageQueryValidationError, UsageResourceExhaustedError,
)
from model_deck.engine.usage.reconciliation import ReconciledUsageQueryUseCase, UsageReconciliationError
from model_deck.engine.usage.use_cases import USAGE_QUERY_PARAMS_REF, USAGE_QUERY_RESULT_REF

SERVER_API = ApiVersion(1, 0)
# Hello negotiation vocabulary. Unrelated to the kernel capability IDs that the
# built-in feature descriptors declare; the two never share a namespace.
SERVER_FEATURES = {"tools": "unsupported", "compaction": "unknown"}
_CLIENT_PRINCIPAL_NAME_PREFIX = "model-deck:client:"
_BUILTIN_FEATURE_IDS = frozenset(BUILTIN_FEATURE_IDS)

_BASE_IMPLEMENTED_METHODS = frozenset(
    {
        "engine.v1.hello",
        "engine.v1.health",
        "engine.v1.operations.list",
        "engine.v1.capabilities.get",
        "engine.v1.models.list",
    }
)

_B07_METHODS = frozenset(
    {
        "engine.v1.models.register",
        "engine.v1.models.rename",
        "engine.v1.models.remove",
        "engine.v1.connections.list",
        "engine.v1.connections.save",
    }
)

_B12_METHODS = frozenset(
    {
        "engine.v1.sessions.create",
        "engine.v1.sessions.get",
        "engine.v1.sessions.select_model",
        "engine.v1.runs.start",
        "engine.v1.runs.get",
        "engine.v1.runs.cancel",
        "engine.v1.runs.submit_tool_result",
    }
)

_EVENT_METHODS = frozenset(
    {
        "engine.v1.events.subscribe",
        "engine.v1.events.ack",
        "engine.v1.events.unsubscribe",
    }
)

_HOST_SETTINGS_METHODS = frozenset(
    {
        "engine.v1.hosts.settings.read",
        "engine.v1.hosts.settings.validate",
        "engine.v1.hosts.settings.preview",
        "engine.v1.hosts.settings.save",
    }
)

_EXTERNAL_EXTENSION_METHODS = frozenset(
    {
        "engine.v1.extensions.install",
        "engine.v1.extensions.enable",
        "engine.v1.extensions.disable",
        "engine.v1.extensions.get",
        "engine.v1.extensions.list",
        "engine.v1.extensions.inspect",
        "engine.v1.extensions.update",
        "engine.v1.extensions.remove",
        "engine.v1.operations.invoke",
        "engine.v1.ui.contributions.list",
        "engine.v1.ui.panel.get",
    }
)

_HOST_PROJECTION_METHODS = frozenset(
    {
        "engine.v1.hosts.projection_status",
    }
)
_HOST_OPERATIONS_METHODS = frozenset(
    {
        "engine.v1.hosts.list",
        "engine.v1.hosts.prepare",
    }
)
_JOB_METHODS = frozenset(
    {"engine.v1.jobs.get", "engine.v1.jobs.cancel", "engine.v1.jobs.resume"}
)

# Which method each job directory serves. A directory that does not implement
# one of these cannot answer for it; see _first_matching_job_directory.
_JOB_DIRECTORY_ATTRIBUTES: dict[str, str] = {
    "engine.v1.jobs.get": "job_get",
    "engine.v1.jobs.cancel": "job_cancel",
    "engine.v1.jobs.resume": "job_resume",
}

# The public sentence for each domain error code a composed feature may name by
# raising KernelDomainError. The feature chooses the classification; the text is
# the engine's, so no handler can interpolate a path, an identifier or a
# provider response into what the caller reads. Built-in operations do not use
# this table: their handlers are dispatch's own methods and carry their own
# messages through KernelPassthroughError.
_COMPOSED_DOMAIN_ERROR_MESSAGES: dict[str, str] = {
    "invalid_argument": "invalid operation argument",
    "unsupported_capability": "capability not supported",
    "capability_denied": "capability denied",
    "not_found": "not found",
    "conflict": "operation conflict",
    "version_mismatch": "version mismatch",
    "provider_unavailable": "provider unavailable",
    "plugin_unavailable": "plugin unavailable",
    "rate_limited": "rate limited",
    "deadline_exceeded": "deadline exceeded",
    "interrupted": "operation interrupted",
    "resume_unavailable": "operation cannot be resumed",
    "resource_exhausted": "answer too large to serve; narrow the request",
    "projection_pending": "projection pending",
    "internal": "operation failed",
}

_RUN_TOPIC_PATTERN = re.compile(
    r"^run:([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})$"
)

_OPERATION_CATALOG: tuple[dict[str, str], ...] = (
    {
        "operation_id": "engine.v1.jobs.get",
        "input_schema_id": "contracts/engine.v1/methods/jobs.get.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/jobs.get.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.jobs.cancel",
        "input_schema_id": "contracts/engine.v1/methods/jobs.cancel.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/jobs.cancel.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.jobs.resume",
        "input_schema_id": "contracts/engine.v1/methods/jobs.resume.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/jobs.resume.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.usage.query",
        "input_schema_id": USAGE_QUERY_PARAMS_REF,
        "output_schema_id": USAGE_QUERY_RESULT_REF,
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.hello",
        "input_schema_id": "contracts/engine.v1/methods/hello.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/hello.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.health",
        "input_schema_id": "contracts/engine.v1/methods/health.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/health.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.operations.list",
        "input_schema_id": "contracts/engine.v1/methods/operations.list.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/operations.list.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.capabilities.get",
        "input_schema_id": "contracts/engine.v1/methods/capabilities.get.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/capabilities.get.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.models.list",
        "input_schema_id": "contracts/engine.v1/methods/models.list.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/models.list.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.models.register",
        "input_schema_id": "contracts/engine.v1/methods/models.register.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/models.register.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.models.rename",
        "input_schema_id": "contracts/engine.v1/methods/models.rename.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/models.rename.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.models.remove",
        "input_schema_id": "contracts/engine.v1/methods/models.remove.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/models.remove.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.connections.list",
        "input_schema_id": "contracts/engine.v1/methods/connections.list.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/connections.list.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.connections.save",
        "input_schema_id": "contracts/engine.v1/methods/connections.save.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/connections.save.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.sessions.create",
        "input_schema_id": "contracts/engine.v1/methods/sessions.create.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/sessions.create.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.sessions.get",
        "input_schema_id": "contracts/engine.v1/methods/sessions.get.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/sessions.get.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.sessions.select_model",
        "input_schema_id": "contracts/engine.v1/methods/sessions.select_model.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/sessions.select_model.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.runs.start",
        "input_schema_id": "contracts/engine.v1/methods/runs.start.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/runs.start.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.runs.get",
        "input_schema_id": "contracts/engine.v1/methods/runs.get.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/runs.get.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.runs.cancel",
        "input_schema_id": "contracts/engine.v1/methods/runs.cancel.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/runs.cancel.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.runs.submit_tool_result",
        "input_schema_id": "contracts/engine.v1/methods/runs.submit_tool_result.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/runs.submit_tool_result.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.events.subscribe",
        "input_schema_id": "contracts/engine.v1/methods/events.subscribe.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/events.subscribe.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.events.ack",
        "input_schema_id": "contracts/engine.v1/methods/events.ack.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/events.ack.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.events.unsubscribe",
        "input_schema_id": "contracts/engine.v1/methods/events.unsubscribe.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/events.unsubscribe.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.hosts.settings.read",
        "input_schema_id": "contracts/engine.v1/methods/hosts.settings.read.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/hosts.settings.read.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.hosts.settings.validate",
        "input_schema_id": "contracts/engine.v1/methods/hosts.settings.validate.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/hosts.settings.validate.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.hosts.settings.preview",
        "input_schema_id": "contracts/engine.v1/methods/hosts.settings.preview.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/hosts.settings.preview.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.hosts.settings.save",
        "input_schema_id": "contracts/engine.v1/methods/hosts.settings.save.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/hosts.settings.save.result.schema.json",
        "effect": "write",
    },
    {
        "operation_id": "engine.v1.hosts.list",
        "input_schema_id": "contracts/engine.v1/methods/hosts.list.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/hosts.list.result.schema.json",
        "effect": "read",
    },
    {
        "operation_id": "engine.v1.hosts.prepare",
        "input_schema_id": "contracts/engine.v1/methods/hosts.prepare.params.schema.json",
        "output_schema_id": "contracts/engine.v1/methods/hosts.prepare.result.schema.json",
        "effect": "write",
    },
    *(
        {
            "operation_id": f"engine.v1.{name}",
            "input_schema_id": f"contracts/engine.v1/methods/{name}.params.schema.json",
            "output_schema_id": f"contracts/engine.v1/methods/{name}.result.schema.json",
            "effect": effect,
        }
        for name, effect in (
            ("extensions.install", "write"),
            ("extensions.enable", "write"),
            ("extensions.disable", "write"),
            ("extensions.get", "read"),
            ("extensions.list", "read"),
            ("extensions.inspect", "read"),
            ("extensions.update", "write"),
            ("extensions.remove", "write"),
            ("operations.invoke", "write"),
            ("ui.contributions.list", "read"),
            ("ui.panel.get", "read"),
        )
    ),
)


def principal_id_for_client_name(client_name: str, engine_instance_id: str) -> str:
    namespace = uuid.UUID(engine_instance_id)
    return str(uuid.uuid5(namespace, f"{_CLIENT_PRINCIPAL_NAME_PREFIX}{client_name}"))


@dataclass(slots=True)
class _ManagedSubscription:
    subscription_id: str
    run_id: str
    principal_id: str
    connection_id: int
    handle: ReplaySubscriptionHandle


def _parse_run_topic(topic: str) -> str | None:
    match = _RUN_TOPIC_PATTERN.match(topic)
    if match is None:
        return None
    return str(uuid.UUID(match.group(1)))


def _flatten_application_run_event(event: ApplicationRunEvent) -> dict[str, Any]:
    flattened: dict[str, Any] = {
        "kind": event.kind,
        "run_id": event.run_id,
        "session_id": event.session_id,
        "sequence": event.sequence,
        "event_schema_version": event.event_schema_version,
        "observed_at": event.observed_at,
    }
    payload = event.payload
    if event.kind == "tool.requested":
        # The application/store payload is flat; only the public event nests it.
        # Validate the whole payload so extra or mixed shapes cannot be dropped.
        validate_schema_ref("contracts/engine.v1/vocabulary.schema.json#/definitions/tool_call", payload)
        flattened["tool_call"] = deepcopy(payload)
        return flattened
    if payload is None:
        return flattened
    if not isinstance(payload, dict):
        raise ValueError("application run event payload must be an object")
    for key, value in payload.items():
        if key in flattened:
            raise ValueError(f"application run event payload collides with field {key}")
        flattened[key] = value
    return flattened


class EngineInstanceIdentity(Protocol):
    engine_instance_id: str
    instance_nonce: str


class EnrollmentVerifier(Protocol):
    def verify(self, engine_instance_id: str, instance_nonce: str, credential: str) -> bool: ...


class EngineDispatch:
    def __init__(
        self,
        list_models: ListModelsUseCase,
        identity: EngineInstanceIdentity,
        enrollment: EnrollmentVerifier,
        *,
        register_model: RegisterModelUseCase | None = None,
        rename_model: RenameModelUseCase | None = None,
        remove_model: RemoveModelUseCase | None = None,
        list_connections: ListConnectionsUseCase | None = None,
        save_connection: SaveConnectionUseCase | None = None,
        create_session: CreateSessionUseCase | None = None,
        get_session: GetSessionUseCase | None = None,
        select_session_model: SelectSessionModelUseCase | None = None,
        start_run: StartRunUseCase | None = None,
        get_run: GetRunUseCase | None = None,
        cancel_run: CancelRunUseCase | None = None,
        submit_tool_result: SubmitToolResultUseCase | None = None,
        run_repository: RunRepository | None = None,
        event_replay: RunEventReplayPort | None = None,
        host_settings: HostSettingsService | None = None,
        host_settings_caller: CallerContext | None = None,
        host_integration: HostIntegrationPort | None = None,
        kernel_composition: KernelComposition | None = None,
        response_preflight: Callable[[dict[str, Any]], Any] | None = None,
        usage_query: ReconciledUsageQueryUseCase | None = None,
        external_extension_host: ExtensionGateway | None = None,
        first_party_jobs: FirstPartyJobDirectory | None = None,
        projection_coordinator: "ProjectionCoordinator | None" = None,
        builtin_binding: BuiltinDispatchBinding | None = None,
    ) -> None:
        self._list_models = list_models
        self._identity = identity
        self._enrollment = enrollment
        self._register_model = register_model
        self._rename_model = rename_model
        self._remove_model = remove_model
        self._list_connections = list_connections
        self._save_connection = save_connection
        self._create_session = create_session
        self._get_session = get_session
        self._select_session_model = select_session_model
        self._start_run = start_run
        self._get_run = get_run
        self._cancel_run = cancel_run
        self._submit_tool_result = submit_tool_result
        self._run_repository = run_repository
        self._event_replay = event_replay
        self._host_settings = host_settings
        self._host_settings_caller = host_settings_caller
        self._host_integration = host_integration
        self._kernel_composition = kernel_composition
        self._response_preflight = response_preflight
        self._usage_query = usage_query
        self._external_extension_host = external_extension_host
        self._first_party_jobs = first_party_jobs
        self._projection_coordinator = projection_coordinator
        self._implemented_methods = self._build_implemented_methods()
        self._kernel_methods: frozenset[str] = frozenset()
        self._kernel_routed_methods: frozenset[str] = frozenset()
        if kernel_composition is not None:
            if not isinstance(kernel_composition, KernelComposition):
                raise CompositionError("kernel composition must be a KernelComposition")
            self._kernel_methods = frozenset(row.operation_id for row in kernel_composition.operations())
            owners = kernel_composition.operation_owners()
            builtin_served = frozenset(
                operation_id
                for operation_id, feature_id in owners.items()
                if feature_id in _BUILTIN_FEATURE_IDS
            )
            foreign_methods = self._kernel_methods - builtin_served
            # A built-in descriptor may name a reserved engine operation, because
            # its handler is the engine method that serves it. Anything else must
            # stay out of the reserved engine namespace.
            if foreign_methods & {entry["operation_id"] for entry in _OPERATION_CATALOG}:
                raise CompositionError("kernel operation collides with a reserved engine operation")
            unimplemented = builtin_served - self._implemented_methods
            if unimplemented:
                raise CompositionError("composed built-in operation has no engine implementation")
            # Every composed operation now routes through the kernel, built-in
            # and foreign alike; a built-in the kernel did not compose is served
            # from the same binding without it.
            self._kernel_routed_methods = self._kernel_methods
            self._implemented_methods |= foreign_methods
            try:
                validate_schema_ref("contracts/engine.v1/methods/operations.list.result.schema.json",
                                    {"operations": self._operation_catalog()})
            except Exception:
                raise CompositionError("combined operation discovery exceeds the public contract") from None
        self._authenticated_sessions: set[int] = set()
        self._connection_principals: dict[int, str] = {}
        self._subscriptions: dict[str, _ManagedSubscription] = {}
        self._subscription_last_acked: dict[str, int] = {}
        self._notification_queues: dict[int, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        # Last, because the binding reads the methods above. A caller that
        # composed the kernel passes the binding whose handlers that kernel
        # already holds; anyone else gets one that only this dispatch reaches.
        self._builtin_binding = builtin_binding or BuiltinDispatchBinding()
        self._builtin_binding.bind(self)

    def _build_implemented_methods(self) -> frozenset[str]:
        methods = set(_BASE_IMPLEMENTED_METHODS)
        if self._usage_query is not None:
            methods.add("engine.v1.usage.query")
        if self._register_model is not None:
            methods.add("engine.v1.models.register")
        if self._rename_model is not None:
            methods.add("engine.v1.models.rename")
        if self._remove_model is not None:
            methods.add("engine.v1.models.remove")
        if self._list_connections is not None:
            methods.add("engine.v1.connections.list")
        if self._save_connection is not None:
            methods.add("engine.v1.connections.save")
        if self._create_session is not None:
            methods.add("engine.v1.sessions.create")
        if self._get_session is not None:
            methods.add("engine.v1.sessions.get")
        if self._select_session_model is not None:
            methods.add("engine.v1.sessions.select_model")
        if self._start_run is not None:
            methods.add("engine.v1.runs.start")
        if self._get_run is not None:
            methods.add("engine.v1.runs.get")
        if self._cancel_run is not None:
            methods.add("engine.v1.runs.cancel")
        if self._submit_tool_result is not None:
            methods.add("engine.v1.runs.submit_tool_result")
        if self._run_repository is not None and self._event_replay is not None:
            methods.update(_EVENT_METHODS)
        if self._host_settings is not None:
            methods.update(_HOST_SETTINGS_METHODS)
        if self._external_extension_host is not None:
            methods.update(_EXTERNAL_EXTENSION_METHODS)
            has_job_reader = callable(
                getattr(self._external_extension_host, "job_get", None)
            )
            has_job_canceller = callable(
                getattr(self._external_extension_host, "job_cancel", None)
            )
            if has_job_reader and has_job_canceller:
                methods.update(_JOB_METHODS)
        if self._first_party_jobs is not None:
            # The engine's own job repository answers jobs.get / jobs.cancel
            # whether or not an external extension host was composed.
            methods.update(_JOB_METHODS)
        if self._projection_coordinator is not None:
            methods.update(_HOST_PROJECTION_METHODS)
        if self._host_integration is not None:
            methods.update(_HOST_OPERATIONS_METHODS)
        return frozenset(methods)

    def drain_notifications(self, connection_id: int) -> tuple[dict[str, Any], ...]:
        event_replay = self._event_replay
        if event_replay is not None:
            with self._lock:
                subscriptions = tuple(
                    managed
                    for managed in self._subscriptions.values()
                    if managed.connection_id == connection_id
                )
            for managed in subscriptions:
                try:
                    page = event_replay.read_available(managed.handle)
                except KeyError:
                    with self._lock:
                        current = self._subscriptions.get(managed.subscription_id)
                        if current is managed:
                            self._subscriptions.pop(managed.subscription_id, None)
                            self._subscription_last_acked.pop(
                                managed.subscription_id,
                                None,
                            )
                    continue
                if page.outcome != EventReplayOutcome.DELIVERED:
                    continue
                try:
                    notifications = [
                        self._build_event_notification(managed.subscription_id, event)
                        for event in page.events
                    ]
                except (SchemaValidationError, ValueError):
                    self._drop_managed_subscription(
                        managed.handle,
                        connection_id=connection_id,
                    )
                    continue
                if notifications:
                    with self._lock:
                        current = self._subscriptions.get(managed.subscription_id)
                        if current is managed:
                            queue = self._notification_queues.setdefault(
                                connection_id,
                                [],
                            )
                            queue.extend(notifications)
        with self._lock:
            queued = self._notification_queues.pop(connection_id, None)
        if not queued:
            return ()
        return tuple(queued)

    def disconnect(self, connection_id: int) -> None:
        with self._lock:
            subscriptions = tuple(
                managed
                for managed in self._subscriptions.values()
                if managed.connection_id == connection_id
            )
            for managed in subscriptions:
                self._subscriptions.pop(managed.subscription_id, None)
                self._subscription_last_acked.pop(managed.subscription_id, None)
            self._authenticated_sessions.discard(connection_id)
            self._connection_principals.pop(connection_id, None)
            self._notification_queues.pop(connection_id, None)
        if self._event_replay is not None:
            for managed in subscriptions:
                self._event_replay.unsubscribe(managed.handle)

    def handle(self, frame: dict[str, Any], connection_id: int) -> dict[str, Any] | None:
        if frame.get("jsonrpc") != "2.0":
            return self._error(frame.get("id"), -32600, "invalid request")
        method = frame.get("method")
        if not isinstance(method, str):
            return self._error(frame.get("id"), -32600, "invalid request")
        if method not in self._implemented_methods:
            if (
                method in _B07_METHODS
                or method in _B12_METHODS
                or method in _EVENT_METHODS
                or method in _HOST_SETTINGS_METHODS
            ):
                return self._domain_error(
                    frame.get("id"),
                    "unsupported_capability",
                    f"method not implemented: {method}",
                )
            return self._domain_error(
                frame.get("id"),
                "unsupported_capability",
                f"method not implemented: {method}",
            )
        if "params" not in frame:
            params: dict[str, Any] = {}
        else:
            raw_params = frame["params"]
            if raw_params is None:
                return self._error(frame.get("id"), -32602, "invalid params")
            if isinstance(raw_params, dict):
                params = raw_params
            else:
                return self._error(frame.get("id"), -32602, "invalid params")
        if method == "engine.v1.hello":
            return self._hello(frame.get("id"), params, connection_id)
        if not self._is_authenticated(connection_id):
            return self._domain_error(frame.get("id"), "capability_denied", "authentication required")
        if method in self._kernel_routed_methods:
            return self._invoke_kernel(frame.get("id"), method, params, connection_id)
        builtin = self._builtin_binding.dispatch_handler(method)
        if builtin is not None:
            # A built-in this engine's kernel did not compose: the escape hatch
            # serves the caller's kernel, and a dispatch built without one has no
            # kernel at all. Same registered handler, one fewer hop.
            return builtin(params, self._invocation_context(frame.get("id"), connection_id))
        return self._domain_error(frame.get("id"), "internal", "unhandled method")

    def _hello(self, request_id: Any, params: Mapping[str, Any], connection_id: int) -> dict[str, Any]:
        try:
            validate_schema_ref("contracts/engine.v1/methods/hello.params.schema.json", dict(params))
        except SchemaValidationError as exc:
            return self._error(request_id, -32602, str(exc))
        client_name = str(params["client_name"])
        offered = ApiVersion.parse(params["offered_api"])
        required = params.get("required_capabilities") or []
        if not isinstance(required, list):
            required = []
        negotiation = evaluate_hello_negotiation(offered, SERVER_API, required, SERVER_FEATURES)
        if not negotiation.ok:
            code = (
                "version_mismatch"
                if negotiation.failure == NegotiationFailure.INCOMPATIBLE_MAJOR
                else negotiation.failure.value
            )
            return self._domain_error(request_id, code, "hello negotiation failed")
        auth = params.get("authentication")
        if auth is None:
            result = {
                "authenticated": False,
                "api_profile": {"major": SERVER_API.major, "minor": SERVER_API.minor},
                "engine_instance_id": self._identity.engine_instance_id,
                "instance_nonce": self._identity.instance_nonce,
                "capabilities": {"features": SERVER_FEATURES},
            }
        else:
            if not isinstance(auth, dict):
                return self._error(request_id, -32602, "invalid authentication")
            if not self._enrollment.verify(
                str(auth.get("engine_instance_id")),
                str(auth.get("instance_nonce")),
                str(auth.get("credential")),
            ):
                return self._domain_error(request_id, "capability_denied", "invalid enrollment credential")
            if not rendezvous_matches(
                self._identity.engine_instance_id,
                self._identity.instance_nonce,
                str(auth.get("engine_instance_id")),
                str(auth.get("instance_nonce")),
            ):
                return self._domain_error(request_id, "capability_denied", "rendezvous mismatch")
            principal_id = principal_id_for_client_name(
                client_name,
                self._identity.engine_instance_id,
            )
            with self._lock:
                self._authenticated_sessions.add(connection_id)
                self._connection_principals[connection_id] = principal_id
            result = {
                "authenticated": True,
                "api_profile": {"major": SERVER_API.major, "minor": SERVER_API.minor},
                "engine_instance_id": self._identity.engine_instance_id,
                "instance_nonce": self._identity.instance_nonce,
                "capabilities": {"features": SERVER_FEATURES},
            }
        try:
            validate_schema_ref("contracts/engine.v1/methods/hello.result.schema.json", result)
        except SchemaValidationError as exc:
            return self._error(request_id, -32603, str(exc))
        return self._success(request_id, result)

    def _invocation_context(self, request_id: Any, connection_id: int) -> DispatchInvocationContext:
        with self._lock:
            principal_id = self._connection_principals.get(connection_id)
        return DispatchInvocationContext(
            connection_id=connection_id,
            principal_id=principal_id,
            request_id=request_id,
        )

    def _invoke_kernel(
        self, request_id: Any, method: str, params: dict[str, Any], connection_id: int
    ) -> dict[str, Any]:
        # A built-in operation's handler is one of this class's own methods, so
        # it answers with a whole response and keeps its own public error codes.
        # A foreign feature's handler stays on the generic path, where a failure
        # is redacted to one internal error.
        dispatch_bound = method in self._kernel_composition.dispatch_bound_operations()
        try:
            if dispatch_bound:
                result = self._kernel_composition.invoke_dispatch_bound(
                    method, params, self._invocation_context(request_id, connection_id)
                )
            else:
                result = self._kernel_composition.invoke(method, params)
        except KernelPassthroughError as passthrough:
            return passthrough.envelope
        except KernelInputError:
            return self._error(request_id, -32602, "invalid operation params")
        except GrantDeniedError:
            return self._domain_error(request_id, "capability_denied", "operation grant denied")
        except KernelDomainError as domain_error:
            # Only the code travels. The sentence is this engine's, chosen by
            # code, so a composed feature can classify its failure publicly
            # without publishing any text of its own.
            return self._domain_error(
                request_id,
                domain_error.code,
                _COMPOSED_DOMAIN_ERROR_MESSAGES.get(domain_error.code, "operation failed"),
            )
        except KernelInvocationError:
            return self._domain_error(request_id, "internal", "operation failed")
        response = self._success(request_id, result)
        if self._response_preflight is not None and not dispatch_bound:
            # A bound handler already applied whatever preflight its own method
            # applies, with that method's error code; running it again here would
            # answer one oversized response with a different code than before.
            try:
                self._response_preflight(response)
            except Exception:
                return self._domain_error(request_id, "internal", "operation failed")
        return response

    def _health(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._success(request_id, {"status": "ok"})

    def _capabilities_get(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._success(request_id, {"features": SERVER_FEATURES})

    def _operation_catalog(self) -> list[dict[str, Any]]:
        # A composed feature describes its own operations, so the static table is
        # only the residue: operations no composed descriptor already covers.
        composed = self._kernel_composition.catalog() if self._kernel_composition is not None else []
        described = {entry["operation_id"] for entry in composed}
        operations = [
            dict(entry)
            for entry in _OPERATION_CATALOG
            if entry["operation_id"] in self._implemented_methods
            and entry["operation_id"] not in described
        ]
        operations.extend(composed)
        if self._external_extension_host is not None:
            operations.extend(
                {
                    "operation_id": descriptor["id"],
                    "input_schema_id": descriptor["input_schema"],
                    "output_schema_id": descriptor["output_schema"],
                    "effect": descriptor["effect"],
                }
                for descriptor in self._external_extension_host.operation_catalog()
            )
        return operations

    def _operations_list(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            validate_schema_ref(
                "contracts/engine.v1/methods/operations.list.params.schema.json",
                dict(params),
            )
        except SchemaValidationError:
            return self._error(request_id, -32602, "invalid params")
        try:
            plugin_id = params.get("plugin_id")
            if plugin_id is None:
                operations = self._operation_catalog()
            elif self._external_extension_host is None:
                operations = []
            else:
                operations = [
                    {
                        "operation_id": descriptor["id"],
                        "input_schema_id": descriptor["input_schema"],
                        "output_schema_id": descriptor["output_schema"],
                        "effect": descriptor["effect"],
                    }
                    for descriptor in self._external_extension_host.operation_catalog()
                    if descriptor["extension_id"] == plugin_id
                ]
        except Exception:
            return self._domain_error(request_id, "internal", "operation catalog unavailable")
        result = {"operations": operations}
        try:
            validate_schema_ref(
                "contracts/engine.v1/methods/operations.list.result.schema.json",
                result,
            )
        except SchemaValidationError as exc:
            return self._error(request_id, -32603, str(exc))
        return self._success(request_id, result)

    def _query_usage(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            validate_schema_ref(USAGE_QUERY_PARAMS_REF, dict(params))
        except SchemaValidationError:
            return self._error(request_id, -32602, "invalid usage query params")
        if self._usage_query is None:
            return self._domain_error(request_id, "unsupported_capability", "usage query not configured")
        try:
            result = self._usage_query.query(since=params.get("since"), until=params.get("until")).to_wire()
            validate_schema_ref(USAGE_QUERY_RESULT_REF, result)
        except UsageQueryValidationError:
            return self._domain_error(request_id, "invalid_argument", "invalid usage query bounds")
        except UsageConflictError:
            return self._domain_error(request_id, "conflict", "usage observation conflict")
        except UsageResourceExhaustedError:
            return self._domain_error(request_id, "resource_exhausted", "usage query limit exceeded")
        except (UsageEventMismatchError, UsageReconciliationError):
            return self._domain_error(request_id, "internal", "committed usage unavailable")
        except Exception:
            return self._domain_error(request_id, "internal", "usage query failed")
        response = self._success(request_id, result)
        if self._response_preflight is not None:
            try:
                self._response_preflight(response)
            except Exception:
                return self._domain_error(request_id, "resource_exhausted", "usage response exceeds transport limit")
        return response

    def _models_list(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            validate_schema_ref("contracts/engine.v1/methods/models.list.params.schema.json", dict(params))
        except SchemaValidationError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            result = self._list_models.execute(params)
        except UnsupportedCollectionError as exc:
            return self._domain_error(request_id, "unsupported_capability", str(exc))
        except CatalogUnavailableError as exc:
            return self._domain_error(request_id, "unsupported_capability", str(exc))
        except ValueError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            validate_schema_ref("contracts/engine.v1/methods/models.list.result.schema.json", result)
        except SchemaValidationError as exc:
            return self._error(request_id, -32603, str(exc))
        return self._success(request_id, result)

    def _models_register(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_model_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/models.register.params.schema.json",
            "contracts/engine.v1/methods/models.register.result.schema.json",
            self._register_model,
            trigger="models.register",
        )

    def _models_rename(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_model_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/models.rename.params.schema.json",
            "contracts/engine.v1/methods/models.rename.result.schema.json",
            self._rename_model,
            trigger="models.rename",
        )

    def _models_remove(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_model_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/models.remove.params.schema.json",
            "contracts/engine.v1/methods/models.remove.result.schema.json",
            self._remove_model,
            trigger="models.remove",
        )

    def _connections_list(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_connection_operation(
            request_id,
            params,
            "contracts/engine.v1/methods/connections.list.params.schema.json",
            "contracts/engine.v1/methods/connections.list.result.schema.json",
            self._list_connections,
            trigger=None,
        )

    def _connections_save(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_connection_operation(
            request_id,
            params,
            "contracts/engine.v1/methods/connections.save.params.schema.json",
            "contracts/engine.v1/methods/connections.save.result.schema.json",
            self._save_connection,
            trigger="connections.save",
        )

    def _sessions_create(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_session_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/sessions.create.params.schema.json",
            "contracts/engine.v1/methods/sessions.create.result.schema.json",
            self._create_session,
        )

    def _sessions_get(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_session_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/sessions.get.params.schema.json",
            "contracts/engine.v1/methods/sessions.get.result.schema.json",
            self._get_session,
        )

    def _sessions_select_model(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_session_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/sessions.select_model.params.schema.json",
            "contracts/engine.v1/methods/sessions.select_model.result.schema.json",
            self._select_session_model,
        )

    def _runs_start(self, request_id: Any, params: Mapping[str, Any], connection_id: int) -> dict[str, Any]:
        principal = self._principal_for_connection(request_id, connection_id)
        if isinstance(principal, dict):
            return principal
        return self._run_run_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/runs.start.params.schema.json",
            "contracts/engine.v1/methods/runs.start.result.schema.json",
            self._start_run,
            principal_id=principal,
        )

    def _runs_get(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_run_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/runs.get.params.schema.json",
            "contracts/engine.v1/methods/runs.get.result.schema.json",
            self._get_run,
        )

    def _runs_cancel(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_run_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/runs.cancel.params.schema.json",
            "contracts/engine.v1/methods/runs.cancel.result.schema.json",
            self._cancel_run,
        )

    def _runs_submit_tool_result(
        self, request_id: Any, params: Mapping[str, Any], connection_id: int
    ) -> dict[str, Any]:
        principal = self._principal_for_connection(request_id, connection_id)
        if isinstance(principal, dict):
            return principal
        return self._run_run_mutation(
            request_id,
            params,
            "contracts/engine.v1/methods/runs.submit_tool_result.params.schema.json",
            "contracts/engine.v1/methods/runs.submit_tool_result.result.schema.json",
            self._submit_tool_result,
            principal_id=principal,
        )

    def _principal_for_connection(self, request_id: Any, connection_id: int) -> str | dict[str, Any]:
        with self._lock:
            principal_id = self._connection_principals.get(connection_id)
        if principal_id is None:
            return self._domain_error(request_id, "capability_denied", "authenticated principal unavailable")
        return principal_id

    def _run_model_mutation(
        self,
        request_id: Any,
        params: Mapping[str, Any],
        params_schema: str,
        result_schema: str,
        use_case: RegisterModelUseCase | RenameModelUseCase | RemoveModelUseCase | None,
        *,
        trigger: str | None,
    ) -> dict[str, Any]:
        if use_case is None:
            return self._domain_error(request_id, "unsupported_capability", "method not configured")
        try:
            validate_schema_ref(params_schema, dict(params))
        except SchemaValidationError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            result = use_case.execute(params)
        except ModelRevisionConflictError as exc:
            return self._domain_error(request_id, "conflict", str(exc))
        except ModelIdempotencyConflictError as exc:
            return self._domain_error(request_id, "conflict", str(exc))
        except ModelRegistrationNotFoundError as exc:
            return self._domain_error(request_id, "not_found", str(exc))
        except ValueError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            validate_schema_ref(result_schema, result)
        except SchemaValidationError as exc:
            return self._error(request_id, -32603, str(exc))
        if trigger is not None:
            self._reconcile_projection(trigger=trigger)
        return self._success(request_id, result)

    def _run_connection_operation(
        self,
        request_id: Any,
        params: Mapping[str, Any],
        params_schema: str,
        result_schema: str,
        use_case: ListConnectionsUseCase | SaveConnectionUseCase | None,
        *,
        trigger: str,
    ) -> dict[str, Any]:
        if use_case is None:
            return self._domain_error(request_id, "unsupported_capability", "method not configured")
        try:
            validate_schema_ref(params_schema, dict(params))
        except SchemaValidationError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            result = use_case.execute(params)
        except ConnectionRevisionConflictError as exc:
            return self._domain_error(request_id, "conflict", str(exc))
        except ConnectionIdempotencyConflictError as exc:
            return self._domain_error(request_id, "conflict", str(exc))
        except ValueError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            validate_schema_ref(result_schema, result)
        except SchemaValidationError as exc:
            return self._error(request_id, -32603, str(exc))
        self._reconcile_projection(trigger=trigger)
        return self._success(request_id, result)

    def _run_session_mutation(
        self,
        request_id: Any,
        params: Mapping[str, Any],
        params_schema: str,
        result_schema: str,
        use_case: CreateSessionUseCase | GetSessionUseCase | SelectSessionModelUseCase | None,
    ) -> dict[str, Any]:
        if use_case is None:
            return self._domain_error(request_id, "unsupported_capability", "method not configured")
        try:
            validate_schema_ref(params_schema, dict(params))
        except SchemaValidationError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            result = use_case.execute(params)
        except RegistrationNotFoundError as exc:
            return self._domain_error(request_id, "not_found", str(exc))
        except SessionNotFoundError as exc:
            return self._domain_error(request_id, "not_found", str(exc))
        except SessionRevisionConflictError as exc:
            return self._domain_error(request_id, "conflict", str(exc))
        except SessionActiveRunConflictError as exc:
            return self._domain_error(request_id, "conflict", str(exc))
        except LookupError as exc:
            return self._domain_error(request_id, "not_found", str(exc))
        except ValueError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            validate_schema_ref(result_schema, result)
        except SchemaValidationError as exc:
            return self._error(request_id, -32603, str(exc))
        return self._success(request_id, result)

    def _run_run_mutation(
        self,
        request_id: Any,
        params: Mapping[str, Any],
        params_schema: str,
        result_schema: str,
        use_case: StartRunUseCase | GetRunUseCase | CancelRunUseCase | SubmitToolResultUseCase | None,
        *,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        if use_case is None:
            return self._domain_error(request_id, "unsupported_capability", "method not configured")
        try:
            validate_schema_ref(params_schema, dict(params))
        except SchemaValidationError as exc:
            return self._error(request_id, -32602, str(exc))
        try:
            if isinstance(use_case, StartRunUseCase):
                assert principal_id is not None
                result = use_case.execute(
                    params,
                    principal_id=principal_id,
                    authorized_host_context_ref=None,
                )
            elif isinstance(use_case, SubmitToolResultUseCase):
                assert principal_id is not None
                result = use_case.execute(
                    params,
                    principal_id=principal_id,
                    authorized_host_context_ref=None,
                )
            else:
                result = use_case.execute(params)
        except RegistrationNotFoundError as exc:
            return self._domain_error(request_id, "not_found", str(exc))
        except (
            RunNotFoundError,
            SessionNotFoundError,
            LookupError,
        ) as exc:
            return self._domain_error(request_id, "not_found", str(exc))
        except (
            RunAdmissionRequestHashConflictError,
            RunStateConflictError,
            SessionRevisionConflictError,
            ToolCallNotOutstandingError,
            ToolResultIdempotencyConflictError,
        ) as exc:
            return self._domain_error(request_id, "conflict", str(exc))
        except RunAuthorizationMismatchError as exc:
            return self._domain_error(request_id, "capability_denied", str(exc))
        except (
            UnsupportedCapabilityError,
            UnknownCapabilityError,
        ) as exc:
            return self._domain_error(request_id, "unsupported_capability", str(exc))
        except ValueError as exc:
            message = str(exc)
            if "host context" in message:
                return self._domain_error(request_id, "capability_denied", message)
            return self._error(request_id, -32602, message)
        try:
            validate_schema_ref(result_schema, result)
        except SchemaValidationError as exc:
            return self._error(request_id, -32603, str(exc))
        return self._success(request_id, result)


    def _hosts_settings_caller(
        self, request_id: Any, connection_id: int
    ) -> CallerContext | dict[str, Any]:
        principal = self._principal_for_connection(request_id, connection_id)
        if isinstance(principal, dict):
            return principal
        if self._host_settings_caller is None:
            return self._domain_error(
                request_id, "capability_denied", "host settings operator context not configured"
            )
        return self._host_settings_caller

    def _hosts_settings(
        self, request_id: Any, params: Mapping[str, Any], connection_id: int, operation: str
    ) -> dict[str, Any]:
        if self._host_settings is None:
            return self._domain_error(request_id, "unsupported_capability", "method not configured")
        caller = self._hosts_settings_caller(request_id, connection_id)
        if isinstance(caller, dict):
            return caller
        service_method = {
            "read": self._host_settings.read,
            "validate": self._host_settings.validate,
            "preview": self._host_settings.preview,
            "save": self._host_settings.save,
        }[operation]
        try:
            result = service_method(caller, dict(params))
        except SettingsDeniedError as exc:
            return self._domain_error(request_id, "capability_denied", "host settings access denied")
        except SettingsNotFoundError as exc:
            return self._domain_error(request_id, "not_found", "host settings document not found")
        except SettingsConflictError as exc:
            reason = getattr(exc, "reason", None)
            if reason == "preview_consumed":
                message = "settings save conflict: preview_consumed"
            else:
                message = "settings save conflict"
            return self._domain_error(request_id, "conflict", message)
        except SettingsVersionMismatchError as exc:
            return self._domain_error(request_id, "version_mismatch", "host settings version changed")
        except SettingsUnsupportedError as exc:
            return self._domain_error(request_id, "unsupported_capability", "host settings capability unavailable")
        except SettingsExhaustedError as exc:
            return self._domain_error(request_id, "resource_exhausted", "host settings limit exceeded")
        except SettingsInvalidError as exc:
            return self._domain_error(request_id, "invalid_argument", "host settings request invalid")
        except SettingsInternalError as exc:
            return self._domain_error(request_id, "internal", "host settings operation failed")
        except Exception:
            return self._domain_error(request_id, "internal", "settings result unavailable")
        if not isinstance(result, dict):
            return self._domain_error(request_id, "internal", "settings result unavailable")
        return self._success(request_id, result)

    @staticmethod
    def _public_extension_record(record: ExtensionRecord) -> dict[str, Any]:
        result: dict[str, Any] = {
            "extension_id": record.extension_id,
            "status": record.status.value,
            "revision": record.revision,
        }
        version = record.selected.executable.version
        if version:
            result["version"] = version
        return result

    def _job_operation(
        self,
        request_id: Any,
        method: str,
        params: Mapping[str, Any],
        connection_id: int,
    ) -> dict[str, Any]:
        # The engine's own jobs and the extension host's jobs live in separate
        # repositories, so a job id is looked up in each in turn. Only "not
        # found" moves on; every other outcome is that directory's answer.
        directories = tuple(
            directory
            for directory in (self._first_party_jobs, self._external_extension_host)
            if directory is not None
        )
        if not directories:
            return self._domain_error(request_id, "unsupported_capability", "method not configured")
        principal = self._principal_for_connection(request_id, connection_id)
        if isinstance(principal, dict):
            return principal
        schema_name = method.removeprefix("engine.v1.")
        try:
            validate_schema_ref(f"contracts/engine.v1/methods/{schema_name}.params.schema.json", dict(params))
            result = self._first_matching_job_directory(
                directories, method, params, principal
            )
            validate_schema_ref(
                f"contracts/engine.v1/methods/{schema_name}.result.schema.json",
                result,
            )
            return self._success(request_id, result)
        except JobsNotFoundError:
            return self._domain_error(request_id, "not_found", "job not found")
        except JobsCallerMismatchError:
            return self._domain_error(request_id, "capability_denied", "job ownership required")
        except (JobsInvalidArgumentError, JobsUnknownKeyError):
            return self._domain_error(request_id, "invalid_argument", "invalid job request")
        except JobsPluginUnavailableError:
            return self._domain_error(request_id, "plugin_unavailable", "job plugin unavailable")
        except JobResumeUnsupportedError:
            return self._domain_error(request_id, "resume_unavailable", "job cannot be resumed")
        except JobResumeConflictError:
            return self._domain_error(request_id, "conflict", "job resume conflict")
        except SchemaValidationError:
            return self._error(request_id, -32602, "invalid params")
        except ValueError:
            return self._domain_error(request_id, "conflict", "job operation conflict")

    @staticmethod
    def _first_matching_job_directory(
        directories: tuple[Any, ...],
        method: str,
        params: Mapping[str, Any],
        principal: str,
    ) -> dict[str, Any]:
        """Ask each job directory in turn; only "not found" tries the next one.

        Every directory receives the authenticated caller. The extension host
        authorizes each job against the principal that originated it; the
        first-party directory owns engine jobs and accepts any authenticated
        caller, which is why the same principal is safe to pass to both.

        A directory that does not implement this method is skipped rather than
        allowed to end the search: a later directory may still own the job, and
        a gateway may legitimately serve ``jobs.get`` without serving
        ``jobs.resume``. Only when no directory could serve at all does the
        method itself decide the answer — ``resume_unavailable`` for a resume
        nothing can perform, and "not found" for a lookup or a cancellation,
        neither of which has anything to do with resuming.
        """
        attribute = _JOB_DIRECTORY_ATTRIBUTES[method]
        served_by_any = False
        for directory in directories:
            operation = getattr(directory, attribute, None)
            if operation is None:
                continue
            served_by_any = True
            try:
                return operation(dict(params), principal=principal)
            except JobsNotFoundError:
                continue
        if not served_by_any and method == "engine.v1.jobs.resume":
            raise JobResumeUnsupportedError("no job directory can resume")
        raise JobsNotFoundError("job not found")

    def _external_extension(
        self,
        request_id: Any,
        method: str,
        params: Mapping[str, Any],
        connection_id: int,
    ) -> dict[str, Any]:
        host = self._external_extension_host
        if host is None:
            return self._domain_error(request_id, "unsupported_capability", "method not configured")
        principal = self._principal_for_connection(request_id, connection_id)
        if isinstance(principal, dict):
            return principal
        method_name = method.removeprefix("engine.v1.")
        params_schema = f"contracts/engine.v1/methods/{method_name}.params.schema.json"
        result_schema = f"contracts/engine.v1/methods/{method_name}.result.schema.json"
        try:
            validate_schema_ref(params_schema, dict(params))
        except SchemaValidationError:
            return self._error(request_id, -32602, "invalid params")
        try:
            if method_name == "extensions.install":
                receipt = host.install(
                    params["archive_path"],
                    principal=principal,
                    idempotency_key=params["idempotency_key"],
                    expected_revision=params["expected_revision"],
                )
                if (
                    not isinstance(receipt, LifecycleReceipt)
                    or receipt.record is None
                    or receipt.outcome is not ReceiptOutcome.APPLIED
                ):
                    raise ExtensionHostConflictError("lifecycle operation did not settle")
                result = {
                    "extension_id": receipt.record.extension_id,
                    "version": receipt.record.selected.executable.version,
                }
            elif method_name in ("extensions.enable", "extensions.disable"):
                lifecycle_method = host.enable if method_name.endswith("enable") else host.disable
                receipt = lifecycle_method(
                    params["extension_id"],
                    principal=principal,
                    idempotency_key=params["idempotency_key"],
                    expected_revision=params["expected_revision"],
                )
                if (
                    not isinstance(receipt, LifecycleReceipt)
                    or receipt.record is None
                    or receipt.outcome is not ReceiptOutcome.APPLIED
                ):
                    raise ExtensionHostConflictError("lifecycle operation did not settle")
                result = {"enabled": receipt.record.status is ExtensionStatus.ENABLED}
            elif method_name == "extensions.get":
                record = host.get_extension(params["extension_id"])
                if record.status is ExtensionStatus.REMOVED:
                    raise KeyError(params["extension_id"])
                result = self._public_extension_record(record)
            elif method_name == "extensions.list":
                result = {
                    "extensions": [
                        {"extension_id": record.extension_id, "status": record.status.value}
                        for record in host.list_extensions()
                        if record.status is not ExtensionStatus.REMOVED
                    ]
                }
            elif method_name == "extensions.inspect":
                payload = host.inspect(params["archive_path"])
                if not isinstance(payload, dict):
                    raise ExtensionHostConflictError("inspect payload was not an object")
                result = {
                    key: payload[key]
                    for key in ("manifest", "provenance")
                    if key in payload
                }
            elif method_name == "extensions.update":
                receipt = host.update(
                    params["extension_id"],
                    params["archive_path"],
                    principal=principal,
                    idempotency_key=params["idempotency_key"],
                    expected_revision=params["expected_revision"],
                )
                if (
                    not isinstance(receipt, LifecycleReceipt)
                    or receipt.record is None
                    or receipt.outcome is not ReceiptOutcome.APPLIED
                ):
                    raise ExtensionHostConflictError("lifecycle operation did not settle")
                result = {
                    "extension_id": receipt.record.extension_id,
                    "version": receipt.record.selected.executable.version,
                }
            elif method_name == "extensions.remove":
                receipt = host.remove(
                    params["extension_id"],
                    principal=principal,
                    idempotency_key=params["idempotency_key"],
                    expected_revision=params["expected_revision"],
                )
                if (
                    not isinstance(receipt, LifecycleReceipt)
                    or receipt.record is None
                    or receipt.outcome is not ReceiptOutcome.APPLIED
                    or receipt.record.status is not ExtensionStatus.REMOVED
                ):
                    raise ExtensionHostConflictError("lifecycle operation did not settle")
                result = {"removed": True}
            elif method_name == "operations.invoke":
                envelope = host.invoke_result(
                    params["operation"],
                    params["input"],
                    principal=principal,
                    idempotency_key=params["idempotency_key"],
                )
                result = envelope
            elif method_name == "ui.contributions.list":
                extension_id = params.get("extension_id")
                panels = host.ui_contributions()
                result = {
                    "panels": [
                        {"panel_id": panel["id"], **({"title": panel["title"]} if "title" in panel else {})}
                        for panel in panels
                        if extension_id is None or panel["extension_id"] == extension_id
                    ]
                }
            elif method_name == "ui.panel.get":
                result = {"panel": host.panel_get(params["panel_id"])}
            else:
                return self._domain_error(request_id, "unsupported_capability", "method not configured")
        except ExtensionHostUnavailableError:
            return self._domain_error(request_id, "plugin_unavailable", "extension operation unavailable")
        except KeyError:
            return self._domain_error(request_id, "not_found", "extension resource not found")
        except ExtensionHostConflictError:
            return self._domain_error(request_id, "conflict", "extension operation conflict")
        except (OSError, ValueError):
            return self._domain_error(request_id, "invalid_argument", "extension request invalid")
        except Exception:
            return self._domain_error(request_id, "internal", "extension operation failed")
        try:
            validate_schema_ref(result_schema, result)
        except SchemaValidationError:
            return self._domain_error(request_id, "internal", "extension result unavailable")
        response = self._success(request_id, result)
        if self._response_preflight is not None:
            try:
                self._response_preflight(response)
            except Exception:
                return self._domain_error(request_id, "internal", "extension result unavailable")
        return response

    def _events_subscribe(
        self, request_id: Any, params: Mapping[str, Any], connection_id: int
    ) -> dict[str, Any]:
        if self._run_repository is None or self._event_replay is None:
            return self._domain_error(request_id, "unsupported_capability", "method not configured")
        principal = self._principal_for_connection(request_id, connection_id)
        if isinstance(principal, dict):
            return principal
        try:
            validate_schema_ref(
                "contracts/engine.v1/methods/events.subscribe.params.schema.json",
                dict(params),
            )
        except SchemaValidationError as exc:
            return self._error(request_id, -32602, str(exc))
        topic = params["topics"][0]
        run_id = _parse_run_topic(topic)
        if run_id is None:
            return self._error(request_id, -32602, "invalid topic")
        try:
            run = self._run_repository.get(GetRunCommand(run_id=run_id))
        except RunNotFoundError:
            return self._domain_error(request_id, "not_found", "run not found")
        if run.principal_id != principal:
            return self._domain_error(request_id, "capability_denied", "run ownership required")
        initial_credit = params["initial_credit"]
        try:
            handle, page = self._event_replay.subscribe(
                run_id,
                after_sequence=None,
                grant_credit=initial_credit,
            )
        except ValueError as exc:
            return self._error(request_id, -32602, str(exc))
        outcome_error = self._replay_page_outcome_error(request_id, page.outcome)
        if outcome_error is not None:
            self._event_replay.unsubscribe(handle)
            return outcome_error
        result = {"subscription_id": handle.subscription_id}
        try:
            validate_schema_ref(
                "contracts/engine.v1/methods/events.subscribe.result.schema.json",
                result,
            )
        except SchemaValidationError as exc:
            self._drop_managed_subscription(handle)
            return self._error(request_id, -32603, str(exc))
        try:
            notifications = [
                self._build_event_notification(handle.subscription_id, event)
                for event in page.events
            ]
        except (SchemaValidationError, ValueError):
            self._drop_managed_subscription(handle)
            return self._domain_error(request_id, "internal", "invalid notification event")
        managed = _ManagedSubscription(
            subscription_id=handle.subscription_id,
            run_id=run_id,
            principal_id=principal,
            connection_id=connection_id,
            handle=handle,
        )
        with self._lock:
            self._subscriptions[handle.subscription_id] = managed
            if notifications:
                queue = self._notification_queues.setdefault(connection_id, [])
                queue.extend(notifications)
        return self._success(request_id, result)

    def _events_ack(
        self, request_id: Any, params: Mapping[str, Any], connection_id: int
    ) -> dict[str, Any]:
        if self._run_repository is None or self._event_replay is None:
            return self._domain_error(request_id, "unsupported_capability", "method not configured")
        principal = self._principal_for_connection(request_id, connection_id)
        if isinstance(principal, dict):
            return principal
        try:
            validate_schema_ref(
                "contracts/engine.v1/methods/events.ack.params.schema.json",
                dict(params),
            )
        except SchemaValidationError as exc:
            return self._error(request_id, -32602, str(exc))
        subscription_id = str(params["subscription_id"])
        sequence = params["sequence"]
        managed = self._require_subscription_owner(
            request_id,
            subscription_id,
            principal,
            connection_id,
        )
        if isinstance(managed, dict):
            return managed
        with self._lock:
            last_acked = self._subscription_last_acked.get(managed.subscription_id, 0)
            newly_advanced = max(0, sequence - last_acked)
            return_credit = newly_advanced if newly_advanced > 0 else 1
            try:
                ack_result = self._event_replay.ack(
                    managed.handle,
                    sequence,
                    return_credit,
                )
            except KeyError:
                return self._domain_error(request_id, "not_found", "subscription not found")
            except ValueError as exc:
                return self._error(request_id, -32602, str(exc))
            outcome_error = self._replay_ack_outcome_error(request_id, ack_result.outcome)
            if outcome_error is not None:
                return outcome_error
            if newly_advanced > 0:
                self._subscription_last_acked[managed.subscription_id] = sequence
        try:
            page = self._event_replay.read_available(managed.handle)
        except KeyError:
            return self._domain_error(request_id, "not_found", "subscription not found")
        page_error = self._replay_page_outcome_error(request_id, page.outcome)
        if page_error is not None:
            return page_error
        try:
            self._queue_replay_page(connection_id, managed.subscription_id, page)
        except ValueError:
            return self._domain_error(request_id, "internal", "invalid notification event")
        result = {"credit": page.credit_remaining}
        try:
            validate_schema_ref(
                "contracts/engine.v1/methods/events.ack.result.schema.json",
                result,
            )
        except SchemaValidationError as exc:
            return self._error(request_id, -32603, str(exc))
        return self._success(request_id, result)

    def _events_unsubscribe(
        self, request_id: Any, params: Mapping[str, Any], connection_id: int
    ) -> dict[str, Any]:
        if self._run_repository is None or self._event_replay is None:
            return self._domain_error(request_id, "unsupported_capability", "method not configured")
        principal = self._principal_for_connection(request_id, connection_id)
        if isinstance(principal, dict):
            return principal
        try:
            validate_schema_ref(
                "contracts/engine.v1/methods/events.unsubscribe.params.schema.json",
                dict(params),
            )
        except SchemaValidationError as exc:
            return self._error(request_id, -32602, str(exc))
        subscription_id = str(params["subscription_id"])
        managed = self._require_subscription_owner(
            request_id,
            subscription_id,
            principal,
            connection_id,
        )
        if isinstance(managed, dict):
            return managed
        self._drop_managed_subscription(
            managed.handle,
            connection_id=connection_id,
        )
        result = {"unsubscribed": True}
        try:
            validate_schema_ref(
                "contracts/engine.v1/methods/events.unsubscribe.result.schema.json",
                result,
            )
        except SchemaValidationError as exc:
            return self._error(request_id, -32603, str(exc))
        return self._success(request_id, result)


    def _remove_queued_subscription_notifications(
        self, connection_id: int, subscription_id: str
    ) -> None:
        with self._lock:
            queue = self._notification_queues.get(connection_id)
            if queue is None:
                return
            filtered = [
                notification
                for notification in queue
                if notification.get("params", {}).get("subscription_id") != subscription_id
            ]
            if filtered:
                self._notification_queues[connection_id] = filtered
            else:
                self._notification_queues.pop(connection_id, None)

    def _drop_managed_subscription(
        self,
        handle: ReplaySubscriptionHandle,
        *,
        connection_id: int | None = None,
    ) -> None:
        subscription_id = handle.subscription_id
        self._event_replay.unsubscribe(handle)
        with self._lock:
            self._subscriptions.pop(subscription_id, None)
            self._subscription_last_acked.pop(subscription_id, None)
        if connection_id is not None:
            self._remove_queued_subscription_notifications(connection_id, subscription_id)

    def _require_subscription_owner(
        self,
        request_id: Any,
        subscription_id: str,
        principal_id: str,
        connection_id: int,
    ) -> _ManagedSubscription | dict[str, Any]:
        with self._lock:
            managed = self._subscriptions.get(subscription_id)
        if managed is None:
            return self._domain_error(request_id, "not_found", "subscription not found")
        if (
            managed.principal_id != principal_id
            or managed.connection_id != connection_id
        ):
            return self._domain_error(request_id, "capability_denied", "subscription ownership required")
        return managed

    def _replay_page_outcome_error(
        self, request_id: Any, outcome: EventReplayOutcome
    ) -> dict[str, Any] | None:
        if outcome == EventReplayOutcome.RESUME_UNAVAILABLE:
            return self._domain_error(request_id, "resume_unavailable", "resume unavailable")
        if outcome == EventReplayOutcome.SLOW_READER:
            return self._domain_error(request_id, "resource_exhausted", "subscriber queue exhausted")
        return None

    def _replay_ack_outcome_error(
        self, request_id: Any, outcome: EventReplayOutcome
    ) -> dict[str, Any] | None:
        if outcome == EventReplayOutcome.RESUME_UNAVAILABLE:
            return self._domain_error(request_id, "resume_unavailable", "resume unavailable")
        if outcome == EventReplayOutcome.SLOW_READER:
            return self._domain_error(request_id, "resource_exhausted", "subscriber queue exhausted")
        return None

    def _queue_replay_page(
        self,
        connection_id: int,
        subscription_id: str,
        page: Any,
    ) -> None:
        if not page.events:
            return
        notifications: list[dict[str, Any]] = []
        for event in page.events:
            notifications.append(self._build_event_notification(subscription_id, event))
        with self._lock:
            queue = self._notification_queues.setdefault(connection_id, [])
            queue.extend(notifications)

    def _build_event_notification(
        self, subscription_id: str, event: ApplicationRunEvent
    ) -> dict[str, Any]:
        flattened = _flatten_application_run_event(event)
        notification = {
            "jsonrpc": "2.0",
            "method": "engine.v1.event",
            "params": {
                "subscription_id": subscription_id,
                "event": flattened,
            },
        }
        validate_schema_ref(
            "contracts/engine.v1/notifications/event.schema.json",
            notification,
        )
        return notification

    def _is_authenticated(self, connection_id: int) -> bool:
        with self._lock:
            return connection_id in self._authenticated_sessions

    def _reconcile_projection(self, *, trigger: str) -> None:
        coordinator = self._projection_coordinator
        if coordinator is None:
            return
        try:
            coordinator.reconcile_after(trigger=trigger)
        except Exception:
            # Reconcile is best-effort by design; never block a successful mutation.
            return

    def _hosts_projection_status(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        coordinator = self._projection_coordinator
        if coordinator is None:
            return self._domain_error(request_id, "unsupported_capability", "projection status not configured")
        try:
            validate_schema_ref(
                "contracts/engine.v1/methods/hosts.projection_status.params.schema.json",
                dict(params),
            )
        except SchemaValidationError as exc:
            return self._error(request_id, -32602, str(exc))
        host_id = params.get("host_id")
        if not isinstance(host_id, str) or host_id != coordinator.host_id:
            return self._domain_error(request_id, "unsupported_capability", "unknown host_id")
        try:
            report = coordinator.status()
        except Exception:
            return self._domain_error(
                request_id,
                "internal",
                "projection status unavailable",
            )
        result = {"status": report.status}
        try:
            validate_schema_ref(
                "contracts/engine.v1/methods/hosts.projection_status.result.schema.json",
                result,
            )
        except SchemaValidationError as exc:
            return self._error(request_id, -32603, str(exc))
        return self._success(request_id, result)

    def _hosts_list(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        integration = self._host_integration
        if integration is None:
            return self._domain_error(
                request_id, "unsupported_capability", "host integration not configured"
            )
        try:
            validate_schema_ref(
                "contracts/engine.v1/methods/hosts.list.params.schema.json",
                dict(params),
            )
        except SchemaValidationError as exc:
            return self._domain_error(request_id, "invalid_argument", str(exc))
        service = HostOperationsService(integration)
        try:
            result = service.list_hosts_envelope()
        except HostOperationsService.ListError as exc:
            return self._domain_error(request_id, exc.code, str(exc))
        return self._success(request_id, result)

    def _hosts_prepare(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        integration = self._host_integration
        if integration is None:
            return self._domain_error(
                request_id, "unsupported_capability", "host integration not configured"
            )
        try:
            validate_schema_ref(
                "contracts/engine.v1/methods/hosts.prepare.params.schema.json",
                dict(params),
            )
        except SchemaValidationError as exc:
            return self._domain_error(request_id, "invalid_argument", str(exc))
        host_id = params.get("host_id")
        if not isinstance(host_id, str):
            return self._domain_error(
                request_id, "invalid_argument", "host_id must be a string"
            )
        service = HostOperationsService(integration)
        try:
            result = service.prepare_envelope(host_id)
        except HostOperationsService.PrepareError as exc:
            return self._domain_error(request_id, exc.code, str(exc))
        return self._success(request_id, result)

    def _success(self, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _domain_error(self, request_id: Any, code: str, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32000,
                "message": message,
                "data": {"code": code, "message": message, "retryable": False},
            },
        }
