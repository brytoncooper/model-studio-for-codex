from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from model_deck.adapters.credentials.file_enrollment import FileEnrollmentCredentialStore
from model_deck.adapters.platform.macos.instance_lock import FileInstanceLock
from model_deck.adapters.platform.macos.isolated_roots import validate_isolated_roots
from model_deck.adapters.platform.macos.paths import IsolatedApplicationPaths
from model_deck.adapters.storage.json_catalog_cache import JsonFixtureCatalogCacheRepository
from model_deck.adapters.storage.sqlite_connection_repository import SQLiteConnectionRepository
from model_deck.adapters.storage.sqlite_model_repository import SQLiteModelRepository
from model_deck.adapters.transport.rendezvous import build_rendezvous_payload, publish_rendezvous_file
from model_deck.adapters.transport.unix_server import UnixSocketEngineServer
from model_deck.engine.dispatch import EngineDispatch
from model_deck.engine.connections.use_cases import ListConnectionsUseCase, SaveConnectionUseCase
from model_deck.engine.model_library.use_cases import (
    ListModelsUseCase,
    RegisterModelUseCase,
    RemoveModelUseCase,
    RenameModelUseCase,
)
from model_deck.engine.server import EngineServer
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
from model_deck.engine.routing.ports import CapabilityFeature, CapabilityTriState, ExecutionMode
from model_deck.engine.runs.use_cases import (
    CancelRunUseCase,
    GetRunUseCase,
    RunApplicationCoordinator,
    StartRunUseCase,
    SubmitToolResultUseCase,
)
from model_deck.engine.sessions.use_cases import (
    CreateSessionUseCase,
    GetSessionUseCase,
    SelectSessionModelUseCase,
)
from model_deck.integrations.hosts.codex.legacy_models import LegacyCodexModelRepository
from model_deck_contracts.negotiation import ApiVersion
from model_deck_contracts.paths import repo_root as contracts_repo_root


@dataclass(frozen=True, slots=True)
class EngineRuntime:
    server: EngineServer
    rendezvous_path: Path
    enrollment: FileEnrollmentCredentialStore
    application_database_path: Path | None = None


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
) -> EngineRuntime:
    if enable_fixture_runs and not enable_application_state:
        raise ValueError("enable_fixture_runs requires enable_application_state")

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

        if enable_fixture_runs:
            session_run_repository = SQLiteSessionRunRepository(application_database_path)
            run_repository = session_run_repository
            route_resolver = RegisteredRouteResolver(
                model_repository=sqlite_models,
                connection_repository=sqlite_connections,
                provider_route_definitions={
                    DETERMINISTIC_PROVIDER_ID: ProviderRouteDefinition(
                        execution_mode=ExecutionMode.CUSTOM,
                        capability_features=(
                            CapabilityFeature("tools", CapabilityTriState.UNSUPPORTED),
                        ),
                        capability_snapshot_ref="ref:capability.fixture",
                    ),
                },
            )
            event_replay = LiveRunEventReplay()
            run_coordinator = RunApplicationCoordinator(
                session_run_repository,
                event_publisher=event_replay,
            )
            deterministic_provider = DeterministicProviderExecutionPort(
                auto_advance=True,
                script=(
                    EmitStarted(),
                    EmitContent("fixture text"),
                    EmitTerminalCompleted(),
                ),
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
                deterministic_provider,
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

    server = EngineServer(
        instance_lock=instance_lock,
        socket_server=socket_server,
        rendezvous_payload_builder=rendezvous_payload_builder,
        rendezvous_publish=rendezvous_publish,
    )
    return EngineRuntime(
        server=server,
        rendezvous_path=rendezvous_path,
        enrollment=enrollment,
        application_database_path=application_database_path,
    )
