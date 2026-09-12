from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from model_deck.engine.connections.ports import ConnectionRepository
from model_deck.engine.model_library.ports import ModelRepository
from model_deck.integrations.hosts.codex.migration_preview.constants import (
    AGENT_MARKER,
    LEGACY_CONNECTION_PROVIDER_ID,
)
from model_deck.integrations.hosts.codex.migration_preview.identity import (
    connection_id_for_account,
    connection_id_for_keyless_endpoint,
    credential_ref,
    endpoint_config_ref,
    registration_id,
)
from model_deck.integrations.hosts.codex.migration_preview.inventory import (
    fixture_root_path,
    json_store_version,
    list_agent_files,
    read_source_bytes,
)
from model_deck.integrations.hosts.codex.migration_preview.managed_agent import (
    ManagedAgentView,
    _is_cursor_url,
    classify_agent_file,
)
from model_deck.integrations.hosts.codex.migration_preview.redaction import find_secret_field_names
from model_deck.integrations.hosts.codex.migration_preview.shape_validation import (
    PreferencesView,
    resolved_endpoint_wire,
    validate_display_names_document,
    validate_endpoints_document,
    validate_preferences_document,
)
from model_deck.integrations.hosts.codex.migration_preview.types import (
    LegacyImportPreviewPlan,
    ProposedConnection,
    ProposedRegisteredModel,
    SourceInventoryEntry,
)

_NON_READY = frozenset({"drift", "collision", "orphan"})


def _account_base_url(account: dict[str, Any]) -> str | None:
    base_url = account.get("baseURL")
    if isinstance(base_url, str) and base_url.strip():
        return base_url.rstrip("/")
    return "https://openrouter.ai/api/v1"


def _effective_account_endpoint(
    account_id: str,
    account: dict[str, Any],
    endpoints: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None, str | None]:
    override = endpoints.get(account_id)
    if override is None and account.get("hasKey", True) is False:
        override = endpoints.get(_account_base_url(account) or "")
    base_url = None
    wire = None
    endpoint_name = None
    if isinstance(override, dict):
        if isinstance(override.get("base_url"), str):
            base_url = override["base_url"].rstrip("/")
        if isinstance(override.get("wire"), str):
            wire = override["wire"]
        if isinstance(override.get("name"), str):
            endpoint_name = override["name"].strip()
    if base_url is None:
        base_url = _account_base_url(account)
    if wire is None and isinstance(account.get("wire"), str):
        wire = account["wire"]
    if endpoint_name is None and isinstance(account.get("name"), str):
        endpoint_name = account["name"].strip()
    return base_url, wire, endpoint_name


def _wire_preference_drift(agent_base_url: str, effective_wire: str | None, effective_base: str | None) -> bool:
    resolved = resolved_endpoint_wire(effective_wire, effective_base or agent_base_url)
    if _is_cursor_url(agent_base_url):
        return resolved != "cursor"
    return resolved == "cursor"


def _resolve_keyless_account_id(
    base_url: str,
    preferences_view: PreferencesView,
    endpoints_view: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None]:
    normalized = base_url.rstrip("/")
    matches: list[str] = []
    for account_id, account in preferences_view.accounts.items():
        if account.get("hasKey", True) is not False:
            continue
        effective_base, _, _ = _effective_account_endpoint(account_id, account, endpoints_view)
        if effective_base is not None and effective_base.rstrip("/") == normalized:
            matches.append(account_id)
    if not matches:
        return None, "managed agent missing keyless account match"
    if len(matches) > 1:
        return None, "ambiguous keyless account for managed agent base_url"
    return matches[0], None


def _connection_content_matches(current, row: ProposedConnection) -> bool:
    return (
        current.provider_id == row.provider_id
        and current.endpoint_config_ref == row.endpoint_config_ref
        and current.credential_ref == row.credential_ref
    )


def _model_content_matches(current, row: ProposedRegisteredModel) -> bool:
    return (
        current.provider_model_id == row.provider_model_id
        and current.connection_id == row.connection_id
        and current.display_name == row.display_name
    )


def _propagate_connection_state_to_models(
    proposed_connections: list[ProposedConnection],
    proposed_models: list[ProposedRegisteredModel],
) -> list[ProposedRegisteredModel]:
    conn_by_id = {row.connection_id: row for row in proposed_connections}
    updated: list[ProposedRegisteredModel] = []
    for row in proposed_models:
        conn = conn_by_id.get(row.connection_id)
        if conn is None or conn.classification == "managed_match":
            updated.append(row)
            continue
        if row.classification == "managed_match":
            updated.append(
                replace(
                    row,
                    classification=conn.classification,
                    detail=conn.detail or "model blocked by connection classification",
                )
            )
        else:
            updated.append(row)
    return updated


def preview_legacy_import_from_fixture_root(
    fixture_root: Path,
    *,
    existing_connections: ConnectionRepository,
    existing_models: ModelRepository,
) -> LegacyImportPreviewPlan:
    root = fixture_root_path(fixture_root)
    diagnostics: list[str] = []
    sources: list[SourceInventoryEntry] = []

    json_documents: dict[str, Any | None] = {
        "preferences.json": None,
        "endpoints.json": None,
        "display-names.json": None,
    }
    preferences_view: PreferencesView | None = None
    endpoints_view: dict[str, dict[str, Any]] = {}
    display_names_view: dict[str, str] = {}

    for relative in ("preferences.json", "endpoints.json", "display-names.json"):
        captured = read_source_bytes(root, relative)
        if captured.is_symlink:
            sources.append(
                SourceInventoryEntry(
                    path=relative,
                    classification="symlink",
                    sha256=None,
                    store_version="missing",
                    detail="symlink source refused",
                )
            )
            continue
        if captured.data is None:
            sources.append(
                SourceInventoryEntry(
                    path=relative,
                    classification="missing",
                    sha256=None,
                    store_version="missing",
                    detail="source file absent",
                )
            )
            continue
        try:
            document = json.loads(captured.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            sources.append(
                SourceInventoryEntry(
                    path=relative,
                    classification="malformed",
                    sha256=captured.sha256,
                    store_version="unversioned",
                    detail="json parse failed",
                )
            )
            continue
        secret_fields = find_secret_field_names(document)
        if secret_fields:
            sources.append(
                SourceInventoryEntry(
                    path=relative,
                    classification="malformed",
                    sha256=captured.sha256,
                    store_version=json_store_version(document),
                    detail=f"secret field name: {secret_fields[0]}",
                )
            )
            continue
        shape_error = None
        if relative == "preferences.json":
            preferences_view, shape_error = validate_preferences_document(document)
        elif relative == "endpoints.json":
            endpoints_view, shape_error = validate_endpoints_document(document)
            if shape_error is None:
                assert endpoints_view is not None
        else:
            display_names_view, shape_error = validate_display_names_document(document)
            if shape_error is None:
                assert display_names_view is not None
        if shape_error is not None:
            sources.append(
                SourceInventoryEntry(
                    path=relative,
                    classification="malformed",
                    sha256=captured.sha256,
                    store_version=json_store_version(document),
                    detail=shape_error,
                )
            )
            continue
        sources.append(
            SourceInventoryEntry(
                path=relative,
                classification="managed_match",
                sha256=captured.sha256,
                store_version=json_store_version(document),
                detail=None,
            )
        )
        json_documents[relative] = document

    if preferences_view is None:
        preferences_view = PreferencesView(accounts={}, models=frozenset(), duplicate_account_ids=frozenset())

    managed_agents: list[ManagedAgentView] = []
    for relative_path, is_symlink in list_agent_files(root):
        if relative_path == "agents" and is_symlink:
            sources.append(
                SourceInventoryEntry(
                    path="agents",
                    classification="symlink",
                    sha256=None,
                    store_version="missing",
                    detail="agents directory symlink refused",
                )
            )
            continue
        if is_symlink:
            sources.append(
                SourceInventoryEntry(
                    path=relative_path,
                    classification="symlink",
                    sha256=None,
                    store_version="missing",
                    detail="agent symlink refused",
                )
            )
            continue
        captured = read_source_bytes(root, relative_path)
        if captured.data is None:
            continue
        filename = Path(relative_path).name
        parsed = classify_agent_file(relative_path, filename, captured.data)
        managed_store_version = (
            1
            if captured.data.startswith((AGENT_MARKER + "\n").encode("utf-8"))
            else "unversioned"
        )
        sources.append(
            SourceInventoryEntry(
                path=relative_path,
                classification=parsed.classification,
                sha256=captured.sha256,
                store_version=managed_store_version,
                detail=parsed.detail,
            )
        )
        if parsed.managed is not None:
            managed_agents.append(parsed.managed)

    proposed_connections: list[ProposedConnection] = []
    for account_id, account in sorted(preferences_view.accounts.items()):
        base_url, wire, endpoint_name = _effective_account_endpoint(account_id, account, endpoints_view)
        conn_id = connection_id_for_account(account_id)
        resolved_wire = resolved_endpoint_wire(wire, base_url)
        endpoint_ref = endpoint_config_ref(base_url or "https://openrouter.ai/api/v1", resolved_wire)
        cred_ref = credential_ref(account_id) if account.get("hasKey", True) is not False else None
        conn_class = "managed_match"
        conn_detail = None
        if account_id in preferences_view.duplicate_account_ids:
            conn_class = "collision"
            conn_detail = "duplicate preferences account id"
        account_base = account.get("baseURL")
        override = endpoints_view.get(account_id)
        if override is None and account.get("hasKey", True) is False:
            override = endpoints_view.get(_account_base_url(account) or "")
        if (
            isinstance(override, dict)
            and isinstance(override.get("base_url"), str)
            and isinstance(account_base, str)
            and override["base_url"].rstrip("/") != account_base.rstrip("/")
        ):
            conn_class = "drift"
            conn_detail = "endpoint override disagrees with preferences"
        proposed_connections.append(
            ProposedConnection(
                connection_id=conn_id,
                provider_id=LEGACY_CONNECTION_PROVIDER_ID,
                endpoint_config_ref=endpoint_ref,
                credential_ref=cred_ref,
                revision=1,
                classification=conn_class,
                source_paths=("preferences.json", "endpoints.json"),
                detail=conn_detail,
            )
        )

    proposed_models: list[ProposedRegisteredModel] = []
    for agent in sorted(managed_agents, key=lambda item: item.path):
        if agent.account_id is None:
            resolved_account_id, keyless_detail = _resolve_keyless_account_id(
                agent.base_url, preferences_view, endpoints_view
            )
            if resolved_account_id is None:
                classification = "collision" if "ambiguous" in (keyless_detail or "") else "orphan"
                detail = keyless_detail
                connection = connection_id_for_keyless_endpoint(agent.base_url)
            else:
                classification = "managed_match"
                detail = None
                connection = connection_id_for_account(resolved_account_id)
        elif agent.account_id not in preferences_view.accounts:
            classification = "orphan"
            detail = "managed agent account not present in preferences"
            connection = connection_id_for_account(agent.account_id)
        else:
            classification = "managed_match"
            detail = None
            connection = connection_id_for_account(agent.account_id)
            account = preferences_view.accounts[agent.account_id]
            effective_base, effective_wire, effective_name = _effective_account_endpoint(
                agent.account_id, account, endpoints_view
            )
            if agent.model not in preferences_view.models:
                classification = "drift"
                detail = "managed agent model absent from preferences models"
            elif effective_base is not None and agent.base_url.rstrip("/") != effective_base.rstrip("/"):
                classification = "drift"
                detail = "managed agent base_url disagrees with preferences"
            elif effective_name is not None and agent.endpoint_name != effective_name:
                classification = "drift"
                detail = "managed agent endpoint name disagrees with preferences"
            elif _wire_preference_drift(agent.base_url, effective_wire, effective_base):
                classification = "drift"
                detail = "managed agent wire disagrees with preferences"
        display = display_names_view.get(agent.model)
        if not isinstance(display, str) or not display.strip():
            display = agent.model.split("/", 1)[-1]
        reg_id = registration_id(agent.model, agent.role)
        proposed_models.append(
            ProposedRegisteredModel(
                registration_id=reg_id,
                provider_model_id=agent.model,
                connection_id=connection,
                display_name=display,
                revision=1,
                classification=classification,
                source_paths=(
                    agent.path,
                    "preferences.json",
                    "endpoints.json",
                    "display-names.json",
                ),
                detail=detail,
            )
        )

    model_groups: dict[str, list[int]] = {}
    for index, row in enumerate(proposed_models):
        model_groups.setdefault(row.provider_model_id, []).append(index)
    for indexes in model_groups.values():
        if len(indexes) < 2:
            continue
        connections = {proposed_models[index].connection_id for index in indexes}
        detail = (
            "model routed through conflicting connections"
            if len(connections) > 1
            else "duplicate managed model identity"
        )
        for index in indexes:
            row = proposed_models[index]
            proposed_models[index] = replace(
                row,
                classification="collision",
                detail=detail,
            )

    proposed_models = _propagate_connection_state_to_models(proposed_connections, proposed_models)

    existing_conn = {row.connection_id: row for row in existing_connections.list_connections()}
    existing_model_rows = existing_models.list_registered(connection_id=None)
    existing_by_registration = {row.registration_id: row for row in existing_model_rows}
    existing_by_provider: dict[str, list] = {}
    for row in existing_model_rows:
        existing_by_provider.setdefault(row.provider_model_id, []).append(row)

    for index, row in enumerate(proposed_models):
        matches = existing_by_provider.get(row.provider_model_id, [])
        for existing in matches:
            if existing.registration_id == row.registration_id:
                continue
            detail = (
                "existing model claims conflicting connection"
                if existing.connection_id != row.connection_id
                else "existing model has a different registration identity"
            )
            proposed_models[index] = replace(
                row,
                classification="collision",
                detail=detail,
            )
            break

    def reconcile_connection(row: ProposedConnection) -> ProposedConnection:
        current = existing_conn.get(row.connection_id)
        if current is None:
            return row
        if row.classification in _NON_READY:
            return row
        if _connection_content_matches(current, row):
            return row
        return replace(
            row,
            classification="drift",
            detail="connection content differs from existing engine record",
        )

    def reconcile_model(row: ProposedRegisteredModel) -> ProposedRegisteredModel:
        current = existing_by_registration.get(row.registration_id)
        if current is None:
            return row
        if row.classification in _NON_READY:
            return row
        if _model_content_matches(current, row):
            return row
        return replace(
            row,
            classification="drift",
            detail="registered model content differs from existing engine record",
        )

    proposed_connections = [reconcile_connection(row) for row in proposed_connections]
    proposed_models = [reconcile_model(row) for row in proposed_models]
    proposed_models = _propagate_connection_state_to_models(proposed_connections, proposed_models)

    import_ready_connections = tuple(
        row for row in proposed_connections if row.classification == "managed_match"
    )
    import_ready_models = tuple(row for row in proposed_models if row.classification == "managed_match")

    sources.sort(key=lambda item: item.path)
    proposed_connections.sort(key=lambda item: item.connection_id)
    proposed_models.sort(key=lambda item: item.registration_id)
    diagnostics.sort()

    return LegacyImportPreviewPlan(
        fixture_root=".",
        sources=tuple(sources),
        proposed_connections=tuple(proposed_connections),
        proposed_models=tuple(proposed_models),
        import_ready_connections=import_ready_connections,
        import_ready_models=import_ready_models,
        diagnostics=tuple(diagnostics),
    )
