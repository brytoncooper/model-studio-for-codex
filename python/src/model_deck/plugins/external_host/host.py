"""Generic, SQLite-backed external extension host composition."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from model_deck.engine.extensions.ports import (
    ExecutableArtifact,
    ExtensionHostConflictError,
    ExtensionHostUnavailableError,
    ExtensionRecord,
    ExtensionStatus,
    LifecycleAction,
    LifecycleOperation,
    LifecycleReceipt,
    LifecycleRequest,
)
from model_deck.engine.extensions.service import ExtensionLifecycleService
from model_deck.engine.jobs.service import PluginJobBroker
from model_deck.engine.jobs.use_cases import CancelJobUseCase, GetJobUseCase
from model_deck.engine.jobs.wire import PluginJobWireAdapter
from model_deck.engine.plugin_authority import (
    ActivationState, AuthorityContext, OperationAuthority, OriginState, PluginAuthority,
)
from model_deck.engine.plugin_data.service import PluginDataBroker
from model_deck.engine.plugin_data.wire import PluginDataWireAdapter
from model_deck.plugins.activation_authority import SQLiteActivationAuthorityController
from model_deck.plugins.activation_lifecycle import (
    ProcessExtensionActivationLifecycle, ResolvedArtifactLaunch,
)
from model_deck.plugins.artifact_store import stage_archive
from model_deck.plugins.authoring.validation import validate_project_archive
from model_deck.plugins.process_runtime import ProcessRuntimeConfig
from model_deck.plugins.panel_validation import validate_panel_semantics
from model_deck.plugins.schema_bundle import PluginSchemaBundle
from model_deck_contracts.validator import validate_schema_ref

_STORED_INVOKE_RESULT_KEY = "__model_deck_invoke_result_v1__"


@dataclass(frozen=True, slots=True)
class HostDependencies:
    lifecycle_repository: Any
    jobs_repository: Any
    data_store: Any
    instance_lock: Any
    extension_lease: Any


class HostConflictError(ExtensionHostConflictError):
    pass


class HostNotServingError(ExtensionHostUnavailableError):
    pass


_BROKER_METHODS = (
    "plugin.v1.broker.storage.get", "plugin.v1.broker.storage.list",
    "plugin.v1.broker.storage.put", "plugin.v1.broker.storage.delete",
)
_JOB_OPERATION_NAMES = (
    "create",
    "progress",
    "complete",
    "fail",
    "check_cancelled",
)
_JOB_METHODS = tuple(
    f"plugin.v1.broker.jobs.{name}" for name in _JOB_OPERATION_NAMES
)
_JOB_GRANTS = {
    name: ("write", "jobs.own", "jobs.own")
    for name in _JOB_OPERATION_NAMES
}
_GRANTS = {
    "get": ("read", "storage.own", "storage.own"),
    "list": ("read", "storage.own", "storage.own"),
    "put": ("write", "storage.own", "storage.own"),
    "delete": ("write", "storage.own", "storage.own"),
}


class _Contexts:
    def __init__(self) -> None:
        self._items: dict[str, AuthorityContext] = {}
        self._lock = threading.Lock()

    def put(self, context: AuthorityContext) -> None:
        with self._lock:
            if context.invocation_id in self._items:
                raise HostConflictError("authority context collision")
            self._items[context.invocation_id] = context

    def get(self, invocation_id: str) -> AuthorityContext | None:
        with self._lock:
            return self._items.get(invocation_id)


class _Resolver:
    def __init__(self, host: "ExternalExtensionHost") -> None:
        self._host = host

    def resolve(self, executable: ExecutableArtifact) -> ResolvedArtifactLaunch:
        manifest = self._host._manifest(executable.extension_id)
        if manifest["artifact_id"] != executable.artifact_id:
            raise HostConflictError("catalog artifact mismatch")
        artifact = Path(manifest["artifact_path"])
        entrypoint = manifest["document"]["entrypoint"]
        if entrypoint["runtime"] != "python":
            raise HostConflictError("unsupported extension runtime")
        permissions = manifest["document"]["permissions"]
        allowed: list[str] = []
        if "storage.own" in permissions:
            allowed.extend(_BROKER_METHODS)
        if "jobs.own" in permissions:
            allowed.extend(_JOB_METHODS)
        return ResolvedArtifactLaunch(
            executable,
            ProcessRuntimeConfig(
                argv=(sys.executable, "-I", "-B", str(artifact / entrypoint["path"])),
                package_dir=str(artifact), timeout_s=self._host._timeout_s,
            ),
            allowed_broker_methods=tuple(allowed),
        )


class _BrokerFactory:
    def __init__(self, host: "ExternalExtensionHost") -> None:
        self._host = host

    def create(self, identity, binding, allowed_methods):
        repository = self._host._data.repository_for(binding)
        storage_broker = PluginDataBroker(
            authority=self._host._plugin_authority,
            repository=repository,
            grants=dict(_GRANTS),
            mutation_guard=self._host._data.mutation_barrier,
        )
        storage_wire = PluginDataWireAdapter(
            trusted_activation=identity,
            broker=storage_broker,
        )
        job_broker = PluginJobBroker(
            authority=self._host._plugin_authority,
            repository=self._host._jobs,
            grants=dict(_JOB_GRANTS),
            mutation_guard=self._host._data.mutation_barrier,
        )
        job_wire = PluginJobWireAdapter(
            trusted_activation=identity,
            broker=job_broker,
        )

        def dispatch(authenticated_activation_id, method, params):
            if method in _BROKER_METHODS:
                if method not in allowed_methods:
                    raise PermissionError("broker method not granted")
                return storage_wire(authenticated_activation_id, method, params)
            if method in _JOB_METHODS:
                if method not in allowed_methods:
                    raise PermissionError("broker method not granted")
                return job_wire(authenticated_activation_id, method, params)
            raise PermissionError("broker method not granted")

        return dispatch


class ExternalExtensionHost:
    """Own installation, activation, discovery, invocation, and retained data."""

    def __init__(
        self,
        root: Path | str,
        *,
        artifact_root: Path | str | None = None,
        timeout_s: float = 5.0,
        dependencies: HostDependencies,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._artifact_root = (
            Path(artifact_root).resolve()
            if artifact_root is not None
            else self.root / "artifacts"
        )
        if not self._artifact_root.is_absolute():
            raise HostConflictError("artifact root must be absolute")
        self._artifact_root.mkdir(mode=0o700, exist_ok=True)
        self._db = self.root / "host.sqlite3"
        self._timeout_s = timeout_s
        self._engine_id = str(uuid.uuid4())
        self._audience = "model-deck-external-extension"
        self._origins: dict[str, OriginState] = {}
        self._operations: dict[str, OperationAuthority] = {}
        self._contexts = _Contexts()
        self._repository = dependencies.lifecycle_repository(self._db)
        self._data = dependencies.data_store(self._db)
        self._jobs = dependencies.jobs_repository(self._db)
        self._get_job = GetJobUseCase(self._jobs)
        self._cancel_job = CancelJobUseCase(self._jobs)
        self._lock = dependencies.instance_lock(self.root / "instance.lock")
        if not self._lock.acquire(0.0):
            raise HostConflictError("host root is already owned")
        self._lease = dependencies.extension_lease(self._lock, self._repository)
        self._ensure_catalog()
        deadline = lambda: datetime.now(timezone.utc) + timedelta(days=1)
        self._authority = SQLiteActivationAuthorityController(
            self._db, engine_instance_id=self._engine_id, audience=self._audience,
            activation_policy=lambda identity, scopes: ActivationState(
                identity, frozenset({"read", "write"}), frozenset(scopes),
                frozenset(scopes), deadline(), 0,
            ),
            origin_reader=self._origins.get, operation_reader=self._operations.get,
            mutation_guard=self._data.mutation_barrier,
        )
        self._plugin_authority = PluginAuthority(
            engine_instance_id=self._engine_id, audience=self._audience,
            state=self._authority, contexts=self._contexts,
            clock=lambda: datetime.now(timezone.utc),
        )
        self._activation = ProcessExtensionActivationLifecycle(
            engine_instance_id=self._engine_id, audience=self._audience,
            api_major=1, api_minor=0, data_store=self._data,
            job_repository=self._jobs, launch_resolver=_Resolver(self),
            authority_controller=self._authority, broker_factory=_BrokerFactory(self),
            token_factory=lambda: secrets.token_urlsafe(32), nonce_factory=lambda: secrets.token_urlsafe(16),
        )
        self._lifecycle = ExtensionLifecycleService(
            repository=self._repository, data_lifecycle=self._data,
            activation_lifecycle=self._activation, engine_lease=self._lease,
        )
        self._recover_enabled()

    def close(self) -> None:
        for record in self.list_extensions():
            serving = self._activation.serving(record.extension_id)
            if serving is not None:
                self._activation.quiesce(str(uuid.uuid4()), record, deadline_ms=1000)
        self._lock.release()

    def job_get(self, params: dict[str, Any], *, principal: str) -> dict[str, Any]:
        return self._get_job.execute(params, caller_principal_id=principal)

    def job_cancel(self, params: dict[str, Any], *, principal: str) -> dict[str, Any]:
        return self._cancel_job.execute(params, caller_principal_id=principal)

    def install(self, archive_path: Path | str, *, principal: str, idempotency_key: str,
                expected_revision: int = 0) -> LifecycleReceipt | LifecycleOperation:
        data = Path(archive_path).read_bytes()
        report = validate_project_archive(data)
        if not report.ok:
            raise HostConflictError("archive validation failed")
        staged = stage_archive(data, store_root=self._artifact_root)
        document = json.loads((Path(staged.artifact_path) / "manifest.json").read_text())
        inspected = report.manifest
        self._persist_manifest(inspected.identity.manifest_id, staged.artifact_id, staged.artifact_path, document)
        artifact = ExecutableArtifact(staged.artifact_id, inspected.identity.manifest_id,
                                      inspected.identity.version, tuple(inspected.contributions.permissions))
        return self._execute(
            LifecycleAction.INSTALL,
            artifact.extension_id,
            principal,
            idempotency_key,
            expected_revision,
            candidate=artifact,
        )

    def enable(self, extension_id: str, *, principal: str, idempotency_key: str,
               expected_revision: int) -> LifecycleReceipt | LifecycleOperation:
        record = self.get_extension(extension_id)
        permissions = tuple(self._manifest(extension_id)["document"].get("permissions", ()))
        enabled = self._execute(LifecycleAction.ENABLE, extension_id, principal,
                                idempotency_key, expected_revision)
        if (
            not permissions
            or not isinstance(enabled, LifecycleReceipt)
            or enabled.record is None
            or enabled.record.status is not ExtensionStatus.ENABLED
            or enabled.record.selected.approved_scopes == permissions
        ):
            return enabled
        return self._execute(
            LifecycleAction.CHANGE_GRANTS,
            extension_id,
            principal,
            idempotency_key,
            enabled.record.revision,
            approved_scopes=permissions,
        )

    def disable(self, extension_id: str, *, principal: str, idempotency_key: str,
                expected_revision: int) -> LifecycleReceipt | LifecycleOperation:
        return self._execute(LifecycleAction.DISABLE, extension_id, principal,
                             idempotency_key, expected_revision)

    def get_extension(self, extension_id: str) -> ExtensionRecord:
        record = self._repository.get(extension_id)
        if record is None:
            raise KeyError(extension_id)
        return record

    def list_extensions(self) -> tuple[ExtensionRecord, ...]:
        with self._connect() as connection:
            ids = [row[0] for row in connection.execute("SELECT extension_id FROM external_host_catalog ORDER BY extension_id")]
        return tuple(record for item in ids if (record := self._repository.get(item)) is not None)

    extension_get = get_extension
    extension_list = list_extensions

    def operation_catalog(self) -> tuple[dict[str, Any], ...]:
        result = []
        for record in self.list_extensions():
            if record.status is not ExtensionStatus.ENABLED:
                continue
            document = self._manifest(record.extension_id)["document"]
            result.extend({"extension_id": record.extension_id, **item} for item in document["contributes"].get("operations", ()))
        return tuple(result)

    def ui_contributions(self) -> tuple[dict[str, str], ...]:
        result = []
        for record in self.list_extensions():
            if record.status is ExtensionStatus.ENABLED:
                result.extend({"extension_id": record.extension_id, **item}
                              for item in self._manifest(record.extension_id)["document"]["contributes"].get("panels", ()))
        return tuple(result)

    def panel_get(self, panel_id: str) -> dict[str, Any]:
        for descriptor in self.ui_contributions():
            if descriptor["id"] == panel_id:
                manifest = self._manifest(descriptor["extension_id"])
                panel = json.loads((Path(manifest["artifact_path"]) / descriptor["schema"]).read_text())
                validate_schema_ref("contracts/ui.panel.v1/tree.schema.json", panel)
                return panel
        raise KeyError(panel_id)

    def invoke(self, operation_id: str, input: Any, *, idempotency_key: str,
               principal: str) -> Any:
        return self.invoke_result(operation_id, input, idempotency_key=idempotency_key,
                                  principal=principal)["output"]

    def invoke_result(self, operation_id: str, input: Any, *, idempotency_key: str,
                      principal: str) -> dict[str, Any]:
        descriptor = next((item for item in self.operation_catalog() if item["id"] == operation_id), None)
        if descriptor is None:
            raise HostNotServingError(operation_id)
        extension_id = descriptor["extension_id"]
        manifest = self._manifest(extension_id)
        schemas = {path.relative_to(manifest["artifact_path"]).as_posix(): path.read_bytes()
                   for path in Path(manifest["artifact_path"]).rglob("*.schema.json")}
        bundle = PluginSchemaBundle.from_resources(schemas)
        checked_input = bundle.validate(descriptor["input_schema"], input)
        digest = self._digest({"operation": operation_id, "input": checked_input, "principal": principal})
        replay = self._claim_invocation(principal, idempotency_key, digest)
        if replay is not None:
            if (
                isinstance(replay, dict)
                and set(replay) == {_STORED_INVOKE_RESULT_KEY}
                and isinstance(replay[_STORED_INVOKE_RESULT_KEY], dict)
            ):
                return replay[_STORED_INVOKE_RESULT_KEY]
            return {"output": replay}
        serving = self._activation.serving(extension_id)
        if serving is None:
            raise HostNotServingError(extension_id)
        deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
        declared_effect = descriptor["effect"]
        effects = (
            frozenset({"read", "write"})
            if declared_effect == "write"
            else frozenset({declared_effect})
        )
        grants = frozenset(self._manifest(extension_id)["document"].get("permissions", ()))
        self._origins[principal] = OriginState(principal, self._engine_id, self._audience,
                                              frozenset({"read", "write"}), grants, grants, deadline, 0)
        self._operations[operation_id] = OperationAuthority(operation_id, effects, grants, grants)
        handle = self._plugin_authority.issue(serving.identity, principal, operation_id, expires_at=deadline)
        state = self._authority.activation(serving.identity)
        assert state is not None
        response = serving.invocation_channel.invoke(
            operation_id, checked_input,
            {"activation_id": serving.identity.activation_id, "plugin_id": extension_id,
             "invocation_handle": handle, "revocation_generation": state.revocation_generation},
            timeout_s=self._timeout_s,
        )
        if response.get("error", {}).get("code") == "conflict":
            raise HostConflictError("plugin invocation conflict")
        output = bundle.validate(descriptor["output_schema"], response["output"])
        envelope: dict[str, Any] = {"output": output}
        if "job_id" in response:
            job_id = response["job_id"]
            try:
                parsed_job_id = uuid.UUID(job_id)
            except (ValueError, TypeError, AttributeError):
                raise HostConflictError("plugin returned an invalid job id") from None
            if str(parsed_job_id).casefold() != str(job_id).casefold():
                raise HostConflictError("plugin returned an invalid job id")
            envelope["job_id"] = job_id
        if "panel" in response:
            panel = response["panel"]
            validate_schema_ref("contracts/ui.panel.v1/tree.schema.json", panel)
            contributed = {item["id"] for item in self.ui_contributions()
                           if item["extension_id"] == extension_id}
            if panel["panel_id"] not in contributed:
                raise HostConflictError("plugin returned an undeclared panel")
            operation_ids = {item["id"] for item in self.operation_catalog()
                             if item["extension_id"] == extension_id}
            report = validate_panel_semantics(
                panel, declared_panel_id=panel["panel_id"],
                declared_operation_ids=operation_ids,
            )
            if not report.ok:
                raise HostConflictError("plugin returned an invalid panel")
            envelope["panel"] = panel
        self._complete_invocation(
            principal,
            idempotency_key,
            digest,
            {_STORED_INVOKE_RESULT_KEY: envelope},
        )
        return envelope

    def _execute(self, action, extension_id, principal, key, revision, *, candidate=None, approved_scopes=()):
        semantic = {"action": action.value, "extension_id": extension_id, "expected_revision": revision,
                    "candidate": None if candidate is None else candidate.artifact_id,
                    "approved_scopes": list(approved_scopes)}
        request = LifecycleRequest(str(uuid.uuid4()), principal, action, extension_id, revision,
                                   self._digest(semantic), key, candidate, tuple(approved_scopes))
        return self._lifecycle.execute(request)

    def _recover_enabled(self) -> None:
        for record in self.list_extensions():
            if record.status is ExtensionStatus.ENABLED:
                operation_id = str(uuid.uuid4())
                validated = self._activation.validate(operation_id, record.selected)
                self._activation.admit(operation_id, record, validated,
                                       expected_data_revision=validated.validated_data_revision)

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _ensure_catalog(self) -> None:
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS external_host_catalog (extension_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, artifact_path TEXT NOT NULL, manifest_json TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS external_host_invocations (principal TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_digest TEXT NOT NULL, state TEXT NOT NULL, output_json TEXT, PRIMARY KEY(principal,idempotency_key))")

    def _persist_manifest(self, extension_id, artifact_id, artifact_path, document) -> None:
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            prior = connection.execute("SELECT artifact_id, manifest_json FROM external_host_catalog WHERE extension_id=?", (extension_id,)).fetchone()
            if prior is not None and (prior[0] != artifact_id or prior[1] != encoded):
                raise HostConflictError("extension already has a different catalog artifact")
            connection.execute("INSERT OR IGNORE INTO external_host_catalog VALUES (?,?,?,?)", (extension_id, artifact_id, artifact_path, encoded))

    def _manifest(self, extension_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT artifact_id,artifact_path,manifest_json FROM external_host_catalog WHERE extension_id=?", (extension_id,)).fetchone()
        if row is None:
            raise KeyError(extension_id)
        return {"artifact_id": row[0], "artifact_path": row[1], "document": json.loads(row[2])}

    def _claim_invocation(self, principal, key, digest):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT request_digest,state,output_json FROM external_host_invocations WHERE principal=? AND idempotency_key=?", (principal, key)).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise HostConflictError("idempotency payload conflict")
                if row[1] != "completed":
                    raise HostConflictError("invocation is pending; explicit recovery required")
                return json.loads(row[2])
            connection.execute("INSERT INTO external_host_invocations VALUES (?,?,?,'pending',NULL)", (principal, key, digest))
        return None

    def _complete_invocation(self, principal, key, digest, output):
        with self._connect() as connection:
            changed = connection.execute("UPDATE external_host_invocations SET state='completed',output_json=? WHERE principal=? AND idempotency_key=? AND request_digest=? AND state='pending'", (json.dumps(output, sort_keys=True, separators=(",", ":")), principal, key, digest)).rowcount
            if changed != 1:
                raise HostConflictError("invocation settlement conflict")

    def _connect(self):
        return sqlite3.connect(self._db)
