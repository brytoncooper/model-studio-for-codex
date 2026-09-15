from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from model_deck.adapters.credentials.file_enrollment import FileEnrollmentCredentialStore
from model_deck.adapters.platform.macos.instance_lock import FileInstanceLock
from model_deck.adapters.platform.macos.extension_lease import ExtensionEngineLease
from model_deck.adapters.platform.macos.isolated_roots import validate_isolated_roots
from model_deck.adapters.platform.macos.paths import IsolatedApplicationPaths
from model_deck.adapters.storage.json_catalog_cache import JsonFixtureCatalogCacheRepository
from model_deck.adapters.storage.sqlite_connection_repository import SQLiteConnectionRepository
from model_deck.adapters.storage.sqlite_extension_lifecycle import SQLiteExtensionLifecycleRepository
from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.adapters.storage.sqlite_versioned_plugin_data import SQLiteVersionedPluginDataStore
from model_deck.adapters.storage.sqlite_model_repository import SQLiteModelRepository
from model_deck.adapters.storage.empty_model_repository import EmptyModelRepository
from model_deck.adapters.storage.sqlite_host_settings import SQLitePreviewStore, SQLiteSaveReceiptStore
from model_deck.adapters.transport.rendezvous import build_rendezvous_payload, publish_rendezvous_file
from model_deck.adapters.transport.unix_server import UnixSocketEngineServer
from model_deck.adapters.transport.framing import encode_frame
from model_deck.engine.builtins import (
    CAPABILITY_SUPPORTED,
    COLLABORATOR_PROVIDER_EXECUTION,
    COLLABORATOR_PROVIDER_ROUTES,
    BuiltinAvailability,
    BuiltinHandlerAdapter,
    build_capability_map,
    builtin_feature_descriptors,
    builtin_handler_registry,
    select_available_builtins,
)
from model_deck.engine.dispatch import EngineDispatch
from model_deck.engine.kernel_composition import KernelComposition
from model_deck.kernel import (
    KERNEL_API_MAJOR,
    KERNEL_API_MINOR,
    CompositionError,
    FeatureDescriptor,
    compose,
)
from model_deck.plugins.external_host import ExternalExtensionHost, HostDependencies
from model_deck.engine.connections.use_cases import ListConnectionsUseCase, SaveConnectionUseCase
from model_deck.engine.model_library.ports import ModelRepository
from model_deck.engine.model_library.use_cases import (
    ListModelsUseCase,
    RegisterModelUseCase,
    RemoveModelUseCase,
    RenameModelUseCase,
)
from model_deck.engine.server import EngineServer
from model_deck.engine.host_settings.ports import CallerContext, READ_GRANT, WRITE_GRANT, SettingsDocumentPort
from model_deck.engine.host_settings.service import HostSettingsService
from model_deck.engine.hosts import HostIntegrationPort
from model_deck.adapters.events.live_replay import LiveRunEventReplay
from model_deck.adapters.routing.registered import ProviderRouteDefinition, RegisteredRouteResolver
from model_deck.adapters.storage.sqlite_session_run_repository import SQLiteSessionRunRepository
from model_deck.adapters.storage.sqlite_evidence import SqliteEvidenceCacheRepository
from model_deck.adapters.storage.sqlite_usage import SqliteUsageRepository
from model_deck.engine.evidence.feature import (
    CAPABILITY_EVIDENCE_CACHE,
    CAPABILITY_EVIDENCE_REFRESH,
    COLLABORATOR_QUERY_BENCHMARKS,
    COLLABORATOR_QUERY_PRICES,
    COLLABORATOR_REFRESH_JOBS,
    evidence_feature_descriptors,
    evidence_handler_adapters,
)
from model_deck.engine.evidence.refresh_jobs import EvidenceRefreshJobService
from model_deck.engine.evidence.use_cases import (
    QueryBenchmarksUseCase,
    QueryPricesUseCase,
    RefreshEvidenceUseCase,
)
from model_deck.engine.jobs.first_party import (
    CreateFirstPartyJobUseCase,
    FirstPartyJobDirectory,
    first_party_owner,
    recover_first_party_jobs,
)
from model_deck.engine.jobs.runner import InProcessJobRunner
from model_deck.engine.usage.feature import (
    CAPABILITY_USAGE_SUMMARY,
    COLLABORATOR_SUMMARIZE_USAGE,
    usage_summary_feature_descriptors,
    usage_summary_handler_adapters,
)
from model_deck.engine.usage.use_cases import (
    QueryUsageUseCase,
    RecordUsageUseCase,
    SummarizeUsageUseCase,
)
from model_deck.engine.usage.reconciliation import ReconciledUsageQueryUseCase
from model_deck.engine.routing.ports import CapabilityFeature, CapabilityTriState, ExecutionMode
from model_deck.engine.runs.use_cases import (
    CancelRunUseCase,
    GetRunUseCase,
    RunApplicationCoordinator,
    StartRunUseCase,
    SubmitToolResultUseCase,
)
from model_deck.engine.runs.ports import ProviderExecutionPort
from model_deck.engine.sessions.ports import SessionContinuationResetPort
from model_deck.engine.sessions.use_cases import (
    CreateSessionUseCase,
    GetSessionUseCase,
    SelectSessionModelUseCase,
)
from model_deck.engine.projections.coordinator import ProjectionCoordinator
from model_deck_contracts.negotiation import ApiVersion
from model_deck_contracts.paths import repo_root as contracts_repo_root

if TYPE_CHECKING:
    # Vendor integrations are imported inside the branch that needs them, so a
    # minimal composition starts without loading a single vendor module.
    from model_deck.integrations.hosts.codex.projection_composition.snapshots import (
        ConnectionMetadataResolver,
    )


_OPAQUE_REF_PATTERN = re.compile(r"^ref:[a-z][a-z0-9._-]{0,120}$")
_MAX_OPAQUE_REF = 128


def _projection_root_traverses_symlink(path: Path) -> bool:
    """Reject user-controlled aliases while permitting macOS system temp aliases."""
    for candidate in (path, *path.parents):
        if not candidate.is_symlink():
            continue
        expected_destination = {
            Path("/tmp"): "private/tmp",
            Path("/var"): "private/var",
        }.get(candidate)
        if expected_destination is None:
            return True
        try:
            if candidate.readlink().as_posix() != expected_destination:
                return True
        except OSError:
            return True
    return False


def _combine_startup_recovery(
    base_callback: Callable[[], None] | None,
    projection_coordinator: ProjectionCoordinator,
) -> Callable[[], None]:
    """Keep projection recovery independent without masking base failures."""
    def recover_all() -> None:
        try:
            if base_callback is not None:
                base_callback()
        finally:
            projection_coordinator.recover_then_reconcile(trigger="startup")

    return recover_all


def _combine_first_party_job_recovery(
    base_callback: Callable[[], None] | None,
    repository: Any,
    owner: Any,
) -> Callable[[], None]:
    """Interrupt leftover first-party jobs without masking base failures."""
    def recover_all() -> None:
        try:
            if base_callback is not None:
                base_callback()
        finally:
            recover_first_party_jobs(repository, owner)

    return recover_all


def _canonical_uuid_spelling(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed).casefold() == value.casefold()


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left in right.parents or right in left.parents


def _validate_capability_snapshot_ref(value: object) -> str | None:
    """Validate a capability snapshot reference against the existing application rules.

    The reference must be a non-empty string of at most 128 characters that is
    either a canonical UUID (no braces, no urn: prefix) or a ``ref:`` opaque
    reference matching the lowercase reverse-domain shape used by
    ``engine.connections.use_cases``. ``None`` is accepted when the route
    declares no snapshot reference.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("provider capability snapshot reference must be a string or None")
    if not value:
        raise ValueError("provider capability snapshot reference must be a non-empty string")
    if len(value) > _MAX_OPAQUE_REF:
        raise ValueError(f"provider capability snapshot reference must be at most {_MAX_OPAQUE_REF} characters")
    if _OPAQUE_REF_PATTERN.fullmatch(value):
        return value
    if _canonical_uuid_spelling(value):
        return value
    raise ValueError("provider capability snapshot reference must be a UUID or ref: opaque reference")


class EngineCompositionError(CompositionError):
    """Composing the engine kernel at startup failed; no engine was built.

    Messages name only validated feature, operation and capability identifiers,
    never a configuration value, path or request payload.
    """


@dataclass(frozen=True, slots=True)
class AdditionalEngineFeatures:
    """Kernel features composed alongside the built-ins at startup.

    ``features`` are descriptors the engine composes in addition to the built-in
    set; ``handler_adapters`` binds each of them by feature ID; ``capabilities``
    adds capability states for the capabilities those features declare (it may
    not restate a built-in capability); ``collaborators`` are the ports those
    adapters read, merged over the built-in collaborators.
    """

    features: tuple[FeatureDescriptor, ...] = ()
    handler_adapters: Mapping[str, BuiltinHandlerAdapter] = field(default_factory=dict)
    capabilities: Mapping[str, str] = field(default_factory=dict)
    collaborators: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.features, tuple) or any(
            not isinstance(descriptor, FeatureDescriptor) for descriptor in self.features
        ):
            raise EngineCompositionError("additional features must be a tuple of FeatureDescriptor")
        for name in ("handler_adapters", "capabilities", "collaborators"):
            if not isinstance(getattr(self, name), Mapping):
                raise EngineCompositionError(f"additional {name} must be a mapping")
        for feature_id, adapter in self.handler_adapters.items():
            if not isinstance(feature_id, str) or not callable(adapter):
                raise EngineCompositionError("additional handler adapters must be callables keyed by feature ID")


@dataclass(frozen=True, slots=True)
class EngineRuntime:
    server: EngineServer
    rendezvous_path: Path
    enrollment: FileEnrollmentCredentialStore
    application_database_path: Path | None = None
    kernel_capabilities: Mapping[str, str] = field(default_factory=dict)
    degraded_optional_capabilities: tuple[tuple[str, str], ...] = ()


def _snapshot_provider_routes(routes: Mapping[str, ProviderRouteDefinition]) -> dict[str, ProviderRouteDefinition]:
    if not isinstance(routes, Mapping):
        raise ValueError("provider route definitions must be a mapping")
    captured = {}
    for provider_id, definition in dict(routes).items():
        if not isinstance(provider_id, str) or not provider_id or not isinstance(definition, ProviderRouteDefinition):
            raise ValueError("provider route definitions require provider IDs and ProviderRouteDefinition values")
        if not isinstance(definition.execution_mode, ExecutionMode):
            raise ValueError("provider route execution mode must be an ExecutionMode")
        if not isinstance(definition.capability_features, (tuple, list)):
            raise ValueError("provider route capabilities must be an explicit sequence")
        features = []
        for feature in definition.capability_features:
            if (not isinstance(feature, CapabilityFeature) or not isinstance(feature.name, str)
                    or not feature.name or not isinstance(feature.state, CapabilityTriState)):
                raise ValueError("provider route capabilities require named tri-state features")
            features.append(CapabilityFeature(feature.name, feature.state))
        snapshot_ref = _validate_capability_snapshot_ref(definition.capability_snapshot_ref)
        captured[provider_id] = ProviderRouteDefinition(
            definition.execution_mode, tuple(features), snapshot_ref)
    return captured


def _features_behind_a_cycle(features: Sequence[FeatureDescriptor]) -> tuple[str, ...]:
    """Feature IDs that never resolve because they sit in, or behind, a cycle.

    Callers check for missing dependencies first, so anything left unresolved
    here is waiting on itself.
    """
    pending = {descriptor.feature_id: set(descriptor.dependencies) for descriptor in features}
    resolved: set[str] = set()
    progressed = True
    while progressed:
        progressed = False
        for feature_id, dependencies in tuple(pending.items()):
            if dependencies <= resolved:
                resolved.add(feature_id)
                del pending[feature_id]
                progressed = True
    return tuple(sorted(pending))


def _reject_unfit_engine_features(
    features: Sequence[FeatureDescriptor],
    capabilities: Mapping[str, str],
) -> None:
    """Fail startup with a named diagnostic before the kernel is composed.

    ``compose`` rejects the same faults, but phrases them for a library caller
    and reports whichever it happens to reach first. Startup deserves one
    exception type and a sentence an operator can act on.
    """
    known = {descriptor.feature_id for descriptor in features}
    if len(known) != len(features):
        raise EngineCompositionError("two engine features declare the same feature identifier")
    owners: dict[str, str] = {}
    for descriptor in features:
        if descriptor.api.major != KERNEL_API_MAJOR or descriptor.api.minimum_minor > KERNEL_API_MINOR:
            raise EngineCompositionError(
                f"engine feature requires an incompatible kernel API: {descriptor.feature_id}"
            )
        for operation in descriptor.operations:
            owner = owners.setdefault(operation.operation_id, descriptor.feature_id)
            if owner != descriptor.feature_id:
                raise EngineCompositionError(
                    f"engine features collide on operation {operation.operation_id}"
                )
        for capability in descriptor.required_capabilities:
            if capabilities.get(capability) != CAPABILITY_SUPPORTED:
                raise EngineCompositionError(
                    f"engine feature {descriptor.feature_id} requires missing capability {capability}"
                )
        if any(dependency not in known for dependency in descriptor.dependencies):
            raise EngineCompositionError(
                f"engine feature {descriptor.feature_id} depends on a feature that is not composed"
            )
    cyclic = _features_behind_a_cycle(features)
    if cyclic:
        raise EngineCompositionError(f"engine features declare a dependency cycle: {', '.join(cyclic)}")


def _engine_capability_map(
    availability: BuiltinAvailability,
    additional: AdditionalEngineFeatures,
) -> dict[str, str]:
    """The built-in capability map plus the additional features' own capabilities."""
    capabilities = build_capability_map(availability)
    for capability, state in additional.capabilities.items():
        if capability in capabilities:
            raise EngineCompositionError(
                f"additional feature restates a built-in capability: {capability}"
            )
        capabilities[capability] = state
    return capabilities


def _engine_owned_features(
    *,
    query_prices: Any,
    query_benchmarks: Any,
    evidence_refresh_jobs: Any,
    summarize_usage: Any,
) -> AdditionalEngineFeatures:
    """The engine's own kernel-routed features, for what it actually wired.

    Each group appears only when its collaborators exist, so an engine without
    an evidence cache advertises no evidence operations rather than failing
    every call to one.
    """
    features: tuple[FeatureDescriptor, ...] = ()
    handler_adapters: dict[str, Any] = {}
    capabilities: dict[str, str] = {}
    collaborators: dict[str, Any] = {}
    reads_evidence = query_prices is not None and query_benchmarks is not None
    if reads_evidence:
        can_refresh = evidence_refresh_jobs is not None
        features += evidence_feature_descriptors(include_refresh=can_refresh)
        handler_adapters.update(evidence_handler_adapters(include_refresh=can_refresh))
        capabilities[CAPABILITY_EVIDENCE_CACHE] = CAPABILITY_SUPPORTED
        collaborators[COLLABORATOR_QUERY_PRICES] = query_prices
        collaborators[COLLABORATOR_QUERY_BENCHMARKS] = query_benchmarks
        if can_refresh:
            capabilities[CAPABILITY_EVIDENCE_REFRESH] = CAPABILITY_SUPPORTED
            collaborators[COLLABORATOR_REFRESH_JOBS] = evidence_refresh_jobs
    if summarize_usage is not None:
        features += usage_summary_feature_descriptors()
        handler_adapters.update(usage_summary_handler_adapters())
        capabilities[CAPABILITY_USAGE_SUMMARY] = CAPABILITY_SUPPORTED
        collaborators[COLLABORATOR_SUMMARIZE_USAGE] = summarize_usage
    return AdditionalEngineFeatures(
        features=features,
        handler_adapters=handler_adapters,
        capabilities=capabilities,
        collaborators=collaborators,
    )


def _merge_additional_features(
    engine_owned: AdditionalEngineFeatures,
    caller: AdditionalEngineFeatures,
) -> AdditionalEngineFeatures:
    """Compose the engine's own extra features alongside the caller's.

    The engine composes features of its own (the evidence operations, usage
    totals) through the same seam a caller uses. Neither side may silently win
    a name from the other, so every collision fails startup by name.
    """
    engine_ids = {descriptor.feature_id for descriptor in engine_owned.features}
    for descriptor in caller.features:
        if descriptor.feature_id in engine_ids:
            raise EngineCompositionError(
                f"additional feature restates an engine feature: {descriptor.feature_id}"
            )
    return AdditionalEngineFeatures(
        features=(*engine_owned.features, *caller.features),
        handler_adapters=_merged_mapping(
            engine_owned.handler_adapters, caller.handler_adapters, "handler adapter"
        ),
        capabilities=_merged_mapping(
            engine_owned.capabilities, caller.capabilities, "capability"
        ),
        collaborators=_merged_mapping(
            engine_owned.collaborators, caller.collaborators, "collaborator"
        ),
    )


def _merged_mapping(
    engine_owned: Mapping[str, Any],
    caller: Mapping[str, Any],
    kind: str,
) -> dict[str, Any]:
    merged = dict(engine_owned)
    for name, value in caller.items():
        if name in merged:
            raise EngineCompositionError(f"additional feature restates an engine {kind}: {name}")
        merged[name] = value
    return merged


def _compose_engine_kernel(
    *,
    availability: BuiltinAvailability,
    collaborators: Mapping[str, Any],
    additional: AdditionalEngineFeatures,
    operator_grants: Sequence[str],
) -> tuple[KernelComposition, dict[str, str]]:
    """Compose the built-in descriptors, and any additional ones, at startup.

    A feature whose required capability was not injected is left out, and so is
    anything that depends on it; the rest still composes and still serves. A
    feature whose *optional* capability is missing composes in a degraded state,
    which ``KernelComposition.degraded_optional_capabilities`` reports.
    """
    capabilities = _engine_capability_map(availability, additional)
    features = (
        *select_available_builtins(capabilities, builtin_feature_descriptors()),
        *additional.features,
    )
    _reject_unfit_engine_features(features, capabilities)
    registry = builtin_handler_registry()
    for feature_id, adapter in additional.handler_adapters.items():
        registry = registry.with_adapter(feature_id, adapter)
    # A wiring mistake raises BuiltinWiringError, which is already a precise,
    # payload-free CompositionError; only the kernel's own failures are reworded.
    handlers = registry.build_handlers(features, {**collaborators, **additional.collaborators})
    try:
        kernel = compose(features, capabilities, handlers)
        composition = KernelComposition(kernel, operator_grants=operator_grants)
    except CompositionError:
        raise EngineCompositionError("engine kernel composition failed") from None
    return composition, capabilities


def _compose_shutdown_callback(
    external_extension_host: object | None,
) -> Callable[[], None] | None:
    """Return shutdown for the engine-owned extension host, when present.

    Injected provider execution ports are borrowed from the caller and are
    intentionally absent from this callback.
    """
    if external_extension_host is None or not callable(
        getattr(external_extension_host, "close", None)
    ):
        return None
    return external_extension_host.close


def build_engine_server(
    *,
    state_root: Path,
    artifact_root: Path,
    socket_root: Path,
    legacy_agents_dir: Path,
    default_connection_id: str,
    source_root: Path | None = None,
    catalog_cache_path: Path | None = None,
    enable_application_state: bool = False,
    enable_fixture_runs: bool = False,
    host_settings_document: SettingsDocumentPort | None = None,
    host_settings_caller: CallerContext | None = None,
    host_integration: HostIntegrationPort | None = None,
    # Legacy escape hatch; see the docstring. A caller-composed kernel is served
    # as it stands, so the engine composes none of its own features.
    kernel_composition: KernelComposition | None = None,
    additional_features: AdditionalEngineFeatures | None = None,
    kernel_operator_grants: Sequence[str] = (),
    model_repository: ModelRepository | None = None,
    provider_execution: ProviderExecutionPort | None = None,
    provider_route_definitions: Mapping[str, ProviderRouteDefinition] | None = None,
    enable_external_extensions: bool = False,
    extension_state_root: Path | None = None,
    extension_artifact_root: Path | None = None,
    evidence_transport: Callable[[str], bytes] | None = None,
    projection_root: Path | None = None,
    projection_resolver: ConnectionMetadataResolver | None = None,
    projection_token_helper_path: Path | None = None,
) -> EngineRuntime:
    """Wire the engine's ports, compose its kernel, and return a startable server.

    By default the engine composes its own kernel: the built-in descriptors for
    the ports it just wired, its own evidence and usage-summary features, and
    any ``additional_features`` the caller supplies.

    ``kernel_composition`` is the legacy escape hatch for a caller that composed
    a kernel itself. That kernel is served exactly as it stands, which means the
    engine composes *none* of its own features: no evidence operations, no usage
    summary, and no built-in descriptors, leaving dispatch's static tables to
    serve the rest. Because that path composes nothing, it cannot honour
    ``additional_features``, and supplying both is rejected rather than quietly
    dropping the caller's features.
    """
    if kernel_composition is not None and additional_features is not None:
        raise ValueError(
            "kernel_composition serves a caller-composed kernel as it stands and "
            "cannot compose additional_features"
        )
    if enable_fixture_runs and not enable_application_state:
        raise ValueError("enable_fixture_runs requires enable_application_state")
    if enable_external_extensions and not enable_application_state:
        raise ValueError("external extensions require application state")
    if evidence_transport is not None:
        if not enable_application_state:
            raise ValueError("an evidence transport requires enable_application_state")
        if not callable(evidence_transport):
            raise ValueError("evidence transport must be a callable returning bytes")
    extension_roots_supplied = extension_state_root is not None or extension_artifact_root is not None
    if enable_external_extensions != extension_roots_supplied:
        raise ValueError("external extension state and artifact roots are required together")
    if enable_external_extensions:
        if extension_state_root is None or extension_artifact_root is None:
            raise ValueError("external extension state and artifact roots are required together")
        validate_isolated_roots(
            extension_state_root,
            extension_artifact_root,
            socket_root,
            source_root=source_root or contracts_repo_root(),
        )
        if any(
            _paths_overlap(extension_root, application_root)
            for extension_root in (extension_state_root, extension_artifact_root)
            for application_root in (state_root, artifact_root)
        ):
            raise ValueError("external extension roots must be distinct from application roots")
    if model_repository is not None:
        if enable_application_state:
            raise ValueError("model_repository applies only without application state")
        if not isinstance(model_repository, ModelRepository):
            raise ValueError("model_repository must implement ModelRepository")
    if additional_features is not None and not isinstance(additional_features, AdditionalEngineFeatures):
        raise ValueError("additional_features must be an AdditionalEngineFeatures")
    if (provider_execution is None) != (provider_route_definitions is None):
        raise ValueError("provider execution and route definitions must be supplied together")
    injected_routes = None
    if provider_execution is not None:
        if not enable_application_state:
            raise ValueError("injected provider requires enable_application_state")
        if enable_fixture_runs:
            raise ValueError("injected provider cannot be combined with enable_fixture_runs")
        if not isinstance(provider_execution, ProviderExecutionPort) or not callable(provider_execution.start):
            raise ValueError("injected provider must implement ProviderExecutionPort")
        injected_routes = _snapshot_provider_routes(provider_route_definitions)
    projection_supplied = any(
        value is not None
        for value in (projection_root, projection_resolver, projection_token_helper_path)
    )
    if projection_supplied and any(
        value is None
        for value in (projection_root, projection_resolver, projection_token_helper_path)
    ):
        raise ValueError(
            "projection_root, projection_resolver, and projection_token_helper_path "
            "must be supplied together"
        )
    if projection_supplied and not enable_application_state:
        raise ValueError("projection wiring requires enable_application_state")
    if projection_supplied:
        if not projection_root.exists() or not projection_root.is_dir():
            raise ValueError("projection_root must be an existing directory")
        if _projection_root_traverses_symlink(projection_root):
            raise ValueError("projection_root must not traverse a symlink")
        projection_root = projection_root.resolve(strict=True)
        for existing_root in (state_root, artifact_root, socket_root):
            if _paths_overlap(projection_root, existing_root):
                raise ValueError("projection_root must be distinct from application roots")
    if (host_settings_document is None) != (host_settings_caller is None):
        raise ValueError("host settings document and caller must be supplied together")
    if host_settings_document is not None:
        if not isinstance(host_settings_document, SettingsDocumentPort):
            raise ValueError("host settings document must implement SettingsDocumentPort")
        if (type(host_settings_caller) is not CallerContext
                or type(host_settings_caller.principal) is not str
                or not host_settings_caller.principal.strip()
                or type(host_settings_caller.grants) is not frozenset
                or not {READ_GRANT, WRITE_GRANT}.issubset(host_settings_caller.grants)):
            raise ValueError("host settings require an explicit local operator with read and write grants")

    root = source_root or contracts_repo_root()
    validate_isolated_roots(state_root, artifact_root, socket_root, source_root=root)

    paths = IsolatedApplicationPaths(state_root)
    instance_lock = FileInstanceLock(state_root / "engine" / "instance.lock")
    enrollment = FileEnrollmentCredentialStore(state_root)

    catalog_reader = JsonFixtureCatalogCacheRepository(catalog_cache_path)
    application_database_path: Path | None = None
    register_model = None
    rename_model = None
    remove_model = None
    list_connections = None
    save_connection = None
    create_session = None
    get_session = None
    select_session_model = None
    start_run = None
    get_run = None
    cancel_run = None
    submit_tool_result = None
    run_repository = None
    event_replay = None
    usage_query = None
    summarize_usage = None
    host_settings = None
    external_extension_host = None
    projection_coordinator = None
    query_prices = None
    query_benchmarks = None
    evidence_refresh_jobs = None
    first_party_jobs = None
    first_party_job_repository = None
    first_party_job_owner = None
    if host_settings_document is not None and host_settings_caller is not None:
        settings_database = paths.state_root() / "engine" / "host-settings.sqlite3"
        host_settings = HostSettingsService(
            document=host_settings_document,
            previews=SQLitePreviewStore(settings_database),
            receipts=SQLiteSaveReceiptStore(settings_database),
            new_preview_id=lambda: str(uuid4()),
            local_operator_principal=host_settings_caller.principal,
        )

    if enable_application_state:
        application_database_path = paths.state_root() / "engine" / "state.sqlite3"
        application_database_path.parent.mkdir(parents=True, exist_ok=True)
        sqlite_models = SQLiteModelRepository(application_database_path)
        sqlite_connections = SQLiteConnectionRepository(application_database_path)
        if projection_root is not None and projection_resolver is not None:
            from model_deck.cli.codex_projection_composition import CodexProjectionCoordinator

            projection_coordinator = CodexProjectionCoordinator.build(
                projection_root=projection_root,
                database_path=application_database_path,
                model_repository=sqlite_models,
                connection_repository=sqlite_connections,
                resolver=projection_resolver,
                token_helper_path=projection_token_helper_path,
            )
        list_models = ListModelsUseCase(sqlite_models, catalog_reader=catalog_reader)
        register_model = RegisterModelUseCase(sqlite_models)
        rename_model = RenameModelUseCase(sqlite_models)
        remove_model = RemoveModelUseCase(sqlite_models)
        list_connections = ListConnectionsUseCase(sqlite_connections)
        save_connection = SaveConnectionUseCase(sqlite_connections)

        # Evidence lives with application state, not with runs: a deck that has
        # executed nothing still wants cached prices and benchmarks.
        evidence_cache = SqliteEvidenceCacheRepository(application_database_path)
        query_prices = QueryPricesUseCase(evidence_cache)
        query_benchmarks = QueryBenchmarksUseCase(evidence_cache)

        # First-party jobs share the durable job state the plugin path uses, in
        # the application database and under the engine's reserved owner.
        first_party_job_repository = SQLitePluginJobRepository(
            application_database_path,
            checkpoint_validator=lambda _schema, _value: None,
        )
        first_party_job_owner = first_party_owner(enrollment.engine_instance_id)
        first_party_jobs = FirstPartyJobDirectory(first_party_job_repository)
        from model_deck.adapters.evidence.source import HttpEvidenceSource
        from model_deck.adapters.evidence.transport import build_urllib_transport

        # The transport is injectable so a test never reaches the network, and
        # the default one only ever runs inside an explicit refresh job.
        evidence_refresh_jobs = EvidenceRefreshJobService(
            refresh=RefreshEvidenceUseCase(
                source=HttpEvidenceSource(
                    transport=evidence_transport
                    if evidence_transport is not None
                    else build_urllib_transport(),
                ),
                repository=evidence_cache,
            ),
            create_job=CreateFirstPartyJobUseCase(
                first_party_job_repository,
                owner=first_party_job_owner,
            ),
            runner=InProcessJobRunner(
                first_party_job_repository,
                owner=first_party_job_owner,
            ),
        )

        if enable_fixture_runs or provider_execution is not None:
            run_provider = provider_execution
            run_routes = injected_routes
            if enable_fixture_runs:
                from model_deck.adapters.providers.deterministic import (
                    DETERMINISTIC_PROVIDER_ID,
                    DeterministicProviderExecutionPort,
                    EmitContent,
                    EmitStarted,
                    EmitTerminalCompleted,
                )

                run_provider = DeterministicProviderExecutionPort(
                    auto_advance=True,
                    script=(EmitStarted(), EmitContent("fixture text"), EmitTerminalCompleted()),
                )
                run_routes = {
                    DETERMINISTIC_PROVIDER_ID: ProviderRouteDefinition(
                        execution_mode=ExecutionMode.CUSTOM,
                        capability_features=(CapabilityFeature("tools", CapabilityTriState.UNSUPPORTED),),
                        capability_snapshot_ref="ref:capability.fixture",
                    ),
                }
            session_run_repository = SQLiteSessionRunRepository(
                application_database_path,
                continuation_reset=(
                    run_provider
                    if isinstance(run_provider, SessionContinuationResetPort)
                    else None
                ),
            )
            run_repository = session_run_repository
            usage_repository = SqliteUsageRepository(application_database_path)
            usage_query = ReconciledUsageQueryUseCase(
                reader=session_run_repository,
                record_usage=RecordUsageUseCase(usage_repository),
                # Cached prices let a record be labelled "estimated" on read.
                # No price ever reaches settled money, and a record the cache
                # cannot price keeps exactly the shape the provider reported.
                query_usage=QueryUsageUseCase(usage_repository, prices=query_prices),
            )
            # Totals read the same reconciled records usage.query serves, so
            # the two operations are composed and withdrawn together.
            summarize_usage = SummarizeUsageUseCase(usage_query)
            route_resolver = RegisteredRouteResolver(
                model_repository=sqlite_models,
                connection_repository=sqlite_connections,
                provider_route_definitions=run_routes,
            )
            event_replay = LiveRunEventReplay()
            run_coordinator = RunApplicationCoordinator(
                session_run_repository,
                event_publisher=event_replay,
            )
            create_session = CreateSessionUseCase(session_run_repository, route_resolver)
            get_session = GetSessionUseCase(session_run_repository)
            select_session_model = SelectSessionModelUseCase(
                session_run_repository,
                route_resolver,
            )
            start_run = StartRunUseCase(
                session_run_repository,
                session_run_repository,
                route_resolver,
                run_provider,
                run_coordinator,
            )
            get_run = GetRunUseCase(session_run_repository)
            cancel_run = CancelRunUseCase(
                session_run_repository,
                run_coordinator,
                cancel_deadline="2099-01-01T00:00:00Z",
            )
            submit_tool_result = SubmitToolResultUseCase(
                session_run_repository,
                run_coordinator,
            )
    else:
        # No application database: the caller owns whatever registered models
        # exist. The default is vendor-free, so a minimal engine starts without
        # loading any host integration.
        repository = model_repository if model_repository is not None else EmptyModelRepository()
        list_models = ListModelsUseCase(repository, catalog_reader=catalog_reader)

    if enable_external_extensions:
        assert extension_state_root is not None and extension_artifact_root is not None
        external_extension_host = ExternalExtensionHost(
            extension_state_root,
            artifact_root=extension_artifact_root,
            dependencies=HostDependencies(
                lifecycle_repository=SQLiteExtensionLifecycleRepository,
                jobs_repository=lambda path: SQLitePluginJobRepository(
                    path,
                    checkpoint_validator=lambda _schema, _value: None,
                ),
                data_store=SQLiteVersionedPluginDataStore,
                instance_lock=FileInstanceLock,
                extension_lease=ExtensionEngineLease,
            ),
        )

    external_extension_jobs = external_extension_host is not None and all(
        callable(getattr(external_extension_host, name, None)) for name in ("job_get", "job_cancel")
    )
    # One record of what actually reached this engine. Every entry mirrors the
    # collaborator test EngineDispatch makes for the same operations.
    availability = BuiltinAvailability(
        models_library=all(
            use_case is not None for use_case in (register_model, rename_model, remove_model)
        ),
        connections=all(use_case is not None for use_case in (list_connections, save_connection)),
        sessions_runs=all(
            use_case is not None
            for use_case in (
                create_session,
                get_session,
                select_session_model,
                start_run,
                get_run,
                cancel_run,
                submit_tool_result,
            )
        ),
        events=run_repository is not None and event_replay is not None,
        host_settings=host_settings is not None,
        host_projection=projection_coordinator is not None,
        host_operations=host_integration is not None,
        external_extensions=external_extension_host is not None,
        jobs=external_extension_jobs or first_party_jobs is not None,
        usage=usage_query is not None,
        provider_execution=provider_execution is not None,
        provider_routes=bool(injected_routes),
    )
    collaborators: dict[str, Any] = {
        "list_models": list_models,
        "register_model": register_model,
        "rename_model": rename_model,
        "remove_model": remove_model,
        "list_connections": list_connections,
        "save_connection": save_connection,
        "create_session": create_session,
        "get_session": get_session,
        "select_session_model": select_session_model,
        "start_run": start_run,
        "get_run": get_run,
        "cancel_run": cancel_run,
        "submit_tool_result": submit_tool_result,
        "run_repository": run_repository,
        "event_replay": event_replay,
        "host_settings": host_settings,
        "host_settings_caller": host_settings_caller,
        "host_integration": host_integration,
        "external_extension_host": external_extension_host,
        "projection_coordinator": projection_coordinator,
        "usage_query": usage_query,
        COLLABORATOR_PROVIDER_EXECUTION: provider_execution,
        COLLABORATOR_PROVIDER_ROUTES: injected_routes,
    }
    composition = kernel_composition
    if composition is not None:
        # The legacy escape hatch: the caller's kernel is served as it stands,
        # so the engine composes no features of its own and reports only the
        # capabilities of the ports it wired, never one nothing routes.
        kernel_capabilities = build_capability_map(availability)
    else:
        additional = _merge_additional_features(
            _engine_owned_features(
                query_prices=query_prices,
                query_benchmarks=query_benchmarks,
                evidence_refresh_jobs=evidence_refresh_jobs,
                summarize_usage=summarize_usage,
            ),
            additional_features or AdditionalEngineFeatures(),
        )
        composition, kernel_capabilities = _compose_engine_kernel(
            availability=availability,
            collaborators=collaborators,
            additional=additional,
            operator_grants=kernel_operator_grants,
        )

    dispatch = EngineDispatch(
        list_models,
        enrollment,
        enrollment,
        register_model=register_model,
        rename_model=rename_model,
        remove_model=remove_model,
        list_connections=list_connections,
        save_connection=save_connection,
        create_session=create_session,
        get_session=get_session,
        select_session_model=select_session_model,
        start_run=start_run,
        get_run=get_run,
        cancel_run=cancel_run,
        submit_tool_result=submit_tool_result,
        run_repository=run_repository,
        event_replay=event_replay,
        host_settings=host_settings,
        host_settings_caller=host_settings_caller,
        host_integration=host_integration,
        kernel_composition=composition,
        response_preflight=encode_frame,
        usage_query=usage_query,
        external_extension_host=external_extension_host,
        first_party_jobs=first_party_jobs,
        projection_coordinator=projection_coordinator,
    )

    socket_path = socket_root / "engine.sock"
    rendezvous_path = paths.state_root() / "engine" / "rendezvous.json"

    def handler(frame: dict[str, Any], connection_id: int, stop) -> dict[str, Any] | None:
        return dispatch.handle(frame, connection_id)

    socket_server = UnixSocketEngineServer(
        socket_path,
        handler,
        on_disconnect=dispatch.disconnect,
        notification_provider=dispatch.drain_notifications,
    )

    def rendezvous_payload_builder() -> dict[str, Any]:
        current = enrollment.load_or_create()
        return build_rendezvous_payload(
            socket_path=socket_path,
            engine_instance_id=current.engine_instance_id,
            instance_nonce=current.instance_nonce,
            api_profile=ApiVersion(1, 0),
        )

    def rendezvous_publish(payload: dict[str, Any]) -> None:
        publish_rendezvous_file(rendezvous_path, payload)

    startup_callback = None
    if run_repository is not None:
        def recover_runs() -> None:
            observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            run_repository.recover_after_restart(observed_at)

        startup_callback = recover_runs
    if first_party_job_repository is not None and first_party_job_owner is not None:
        # The in-process runner died with the previous process, so anything
        # still active is interrupted and is never replayed; a caller that
        # still wants the work asks for a new refresh.
        startup_callback = _combine_first_party_job_recovery(
            startup_callback,
            first_party_job_repository,
            first_party_job_owner,
        )
    if projection_coordinator is not None:
        startup_callback = _combine_startup_recovery(
            startup_callback,
            projection_coordinator,
        )

    server = EngineServer(
        instance_lock=instance_lock,
        socket_server=socket_server,
        rendezvous_payload_builder=rendezvous_payload_builder,
        rendezvous_publish=rendezvous_publish,
        startup_callback=startup_callback,
        shutdown_callback=_compose_shutdown_callback(external_extension_host),
    )
    return EngineRuntime(
        server=server,
        rendezvous_path=rendezvous_path,
        enrollment=enrollment,
        application_database_path=application_database_path,
        kernel_capabilities=MappingProxyType(dict(kernel_capabilities)),
        degraded_optional_capabilities=composition.degraded_optional_capabilities(),
    )
