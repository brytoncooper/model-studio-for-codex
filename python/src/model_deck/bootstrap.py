from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
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
from model_deck.adapters.storage.sqlite_host_settings import SQLitePreviewStore, SQLiteSaveReceiptStore
from model_deck.adapters.transport.rendezvous import build_rendezvous_payload, publish_rendezvous_file
from model_deck.adapters.transport.unix_server import UnixSocketEngineServer
from model_deck.adapters.transport.framing import encode_frame
from model_deck.engine.dispatch import EngineDispatch
from model_deck.engine.kernel_composition import KernelComposition
from model_deck.plugins.external_host import ExternalExtensionHost, HostDependencies
from model_deck.engine.connections.use_cases import ListConnectionsUseCase, SaveConnectionUseCase
from model_deck.engine.model_library.use_cases import (
    ListModelsUseCase,
    RegisterModelUseCase,
    RemoveModelUseCase,
    RenameModelUseCase,
)
from model_deck.engine.server import EngineServer
from model_deck.engine.host_settings.ports import CallerContext, READ_GRANT, WRITE_GRANT, SettingsDocumentPort
from model_deck.engine.host_settings.service import HostSettingsService
from model_deck.adapters.events.live_replay import LiveRunEventReplay
from model_deck.adapters.providers.deterministic import (
    DETERMINISTIC_PROVIDER_ID,
    DeterministicProviderExecutionPort,
    EmitContent,
    EmitStarted,
    EmitTerminalCompleted,
)
from model_deck.adapters.routing.registered import ProviderRouteDefinition, RegisteredRouteResolver
from model_deck.adapters.storage.sqlite_session_run_repository import SQLiteSessionRunRepository
from model_deck.adapters.storage.sqlite_usage import SqliteUsageRepository
from model_deck.engine.usage.use_cases import QueryUsageUseCase, RecordUsageUseCase
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
from model_deck.engine.sessions.use_cases import (
    CreateSessionUseCase,
    GetSessionUseCase,
    SelectSessionModelUseCase,
)
from model_deck.integrations.hosts.codex.legacy_models import LegacyCodexModelRepository
from model_deck_contracts.negotiation import ApiVersion
from model_deck_contracts.paths import repo_root as contracts_repo_root


_OPAQUE_REF_PATTERN = re.compile(r"^ref:[a-z][a-z0-9._-]{0,120}$")
_MAX_OPAQUE_REF = 128


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


@dataclass(frozen=True, slots=True)
class EngineRuntime:
    server: EngineServer
    rendezvous_path: Path
    enrollment: FileEnrollmentCredentialStore
    application_database_path: Path | None = None


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


def _compose_shutdown_callback(
    external_extension_host: object | None,
    provider_execution: object | None,
) -> Callable[[], None] | None:
    """Compose a single shutdown callback for the engine server.

    Closes the external extension host (when present) and the injected
    provider execution in that order. Either may be ``None`` or may lack
    a ``close`` callable; missing steps are skipped silently. The first
    error raised by either step is preserved and re-raised after both
    steps have been attempted so callers see the same failure mode as the
    existing single-source shutdown. The returned callable is a plain
    function and is safe to invoke repeatedly.
    """
    parts: list[Callable[[], None]] = []
    if external_extension_host is not None and callable(getattr(external_extension_host, "close", None)):
        parts.append(external_extension_host.close)
    if provider_execution is not None and callable(getattr(provider_execution, "close", None)):
        parts.append(provider_execution.close)
    if not parts:
        return None

    def _run() -> None:
        first_error: BaseException | None = None
        for close_fn in parts:
            try:
                close_fn()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    return _run


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
    kernel_composition: KernelComposition | None = None,
    provider_execution: ProviderExecutionPort | None = None,
    provider_route_definitions: Mapping[str, ProviderRouteDefinition] | None = None,
    enable_external_extensions: bool = False,
    extension_state_root: Path | None = None,
    extension_artifact_root: Path | None = None,
) -> EngineRuntime:
    if enable_fixture_runs and not enable_application_state:
        raise ValueError("enable_fixture_runs requires enable_application_state")
    if enable_external_extensions and not enable_application_state:
        raise ValueError("external extensions require application state")
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
    host_settings = None
    external_extension_host = None
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
        list_models = ListModelsUseCase(sqlite_models, catalog_reader=catalog_reader)
        register_model = RegisterModelUseCase(sqlite_models)
        rename_model = RenameModelUseCase(sqlite_models)
        remove_model = RemoveModelUseCase(sqlite_models)
        list_connections = ListConnectionsUseCase(sqlite_connections)
        save_connection = SaveConnectionUseCase(sqlite_connections)

        if enable_fixture_runs or provider_execution is not None:
            run_provider = provider_execution
            run_routes = injected_routes
            if enable_fixture_runs:
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
            session_run_repository = SQLiteSessionRunRepository(application_database_path)
            run_repository = session_run_repository
            usage_repository = SqliteUsageRepository(application_database_path)
            usage_query = ReconciledUsageQueryUseCase(
                reader=session_run_repository,
                record_usage=RecordUsageUseCase(usage_repository),
                query_usage=QueryUsageUseCase(usage_repository),
            )
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
        repository = LegacyCodexModelRepository(
            legacy_agents_dir,
            default_connection_id=default_connection_id,
        )
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
        kernel_composition=kernel_composition,
        response_preflight=encode_frame,
        usage_query=usage_query,
        external_extension_host=external_extension_host,
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

    server = EngineServer(
        instance_lock=instance_lock,
        socket_server=socket_server,
        rendezvous_payload_builder=rendezvous_payload_builder,
        rendezvous_publish=rendezvous_publish,
        startup_callback=startup_callback,
        shutdown_callback=_compose_shutdown_callback(external_extension_host, provider_execution),
    )
    return EngineRuntime(
        server=server,
        rendezvous_path=rendezvous_path,
        enrollment=enrollment,
        application_database_path=application_database_path,
    )
