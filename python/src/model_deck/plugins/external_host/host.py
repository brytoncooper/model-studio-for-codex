"""Generic, SQLite-backed external extension host composition."""
from __future__ import annotations

import hashlib
import io
import json
import secrets
import sqlite3
import sys
import threading
import uuid
import zipfile
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
    LifecyclePhase,
    LifecycleReceipt,
    LifecycleRequest,
)
from model_deck.engine.extensions.service import ExtensionLifecycleService
from model_deck.engine.jobs.ports import ResumeInvocationRequest, ResumeTarget
from model_deck.engine.jobs.service import PluginJobBroker
from model_deck.engine.jobs.use_cases import (
    CancelJobUseCase,
    GetJobUseCase,
    ResumeJobUseCase,
)
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
from model_deck.plugins.activation_lifecycle.restart import (
    ActivationRestartSupervisor,
    DEFAULT_RESTART_POLICY,
    ExtensionSupervisionReport,
)
from model_deck.plugins.artifact_store import stage_archive
from model_deck.plugins.authoring.validation import validate_project_archive
from model_deck.plugins.process_runtime import ProcessRuntimeConfig
from model_deck.plugins.process_runtime.health import RestartPolicy
from model_deck.plugins.panel_validation import validate_panel_semantics
from model_deck.plugins.schema_bundle import PluginSchemaBundle
from model_deck_contracts.validator import validate_schema_ref

_STORED_INVOKE_RESULT_KEY = "__model_deck_invoke_result_v1__"
_LIFECYCLE_RECOVERY_PAGE_SIZE = 256
_SHUTDOWN_DEADLINE_MS = 1000

SHUTDOWN_QUIESCED = "quiesced"
SHUTDOWN_SETTLED = "settled"
SHUTDOWN_FAILED = "failed"
CHILD_REAPED = "reaped"
CHILD_ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class ExtensionShutdownEntry:
    """What happened to one extension when the host closed.

    `outcome` is quiesced when a live child was drained and reaped, settled
    when there was no child left to ask and only its jobs needed interrupting,
    and failed when quiesce raised — in which case `failure_code` names the
    exception type. `child` says whether this shutdown actually reaped a child
    process or found none. `jobs_interrupted` counts jobs moved out of
    QUEUED/RUNNING; none of them are replayed.
    """

    extension_id: str
    outcome: str
    failure_code: str | None
    jobs_interrupted: int
    child: str


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    """Per-extension outcome of one host shutdown.

    Deliberately a value, not a log line: the engine's shutdown callback can
    retain it, and a test can assert on it, without this layer choosing a
    logging story.
    """

    entries: tuple[ExtensionShutdownEntry, ...] = ()

    def entry(self, extension_id: str) -> ExtensionShutdownEntry | None:
        for candidate in self.entries:
            if candidate.extension_id == extension_id:
                return candidate
        return None

    @property
    def jobs_interrupted(self) -> int:
        return sum(entry.jobs_interrupted for entry in self.entries)

    @property
    def failed_extension_ids(self) -> tuple[str, ...]:
        return tuple(
            entry.extension_id
            for entry in self.entries
            if entry.outcome == SHUTDOWN_FAILED
        )


@dataclass(frozen=True, slots=True)
class WorkerHeartbeatSettings:
    """How often each plugin worker is beaten at, and how long it may miss.

    The defaults are the process runtime's own. A test that has to observe an
    unresponsive worker inside a few seconds shortens them; nothing in
    production does.
    """

    interval_s: float = 5.0
    timeout_s: float = 2.0
    max_missed: int = 3


DEFAULT_WORKER_HEARTBEAT = WorkerHeartbeatSettings()


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
    "checkpoint",
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

    def drop(self, invocation_id: str) -> None:
        """Forget one captured context, which revokes the handle that names it.

        `PluginAuthority` resolves every handle through this store, so a
        context that is gone denies the handle permanently: invocation ids are
        never reissued, and the handle cannot be re-captured.
        """

        with self._lock:
            self._items.pop(invocation_id, None)


class _Resolver:
    def __init__(self, host: "ExternalExtensionHost") -> None:
        self._host = host

    def resolve(self, executable: ExecutableArtifact) -> ResolvedArtifactLaunch:
        manifest = self._host._manifest(executable.extension_id, executable.artifact_id)
        artifact = Path(manifest["artifact_path"])
        entrypoint = manifest["document"]["entrypoint"]
        if entrypoint["runtime"] != "python":
            raise HostConflictError("unsupported extension runtime")
        permissions = self._host._approved_scopes(executable)
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
                heartbeat_interval_s=self._host._heartbeat.interval_s,
                heartbeat_timeout_s=self._host._heartbeat.timeout_s,
                heartbeat_max_missed=self._host._heartbeat.max_missed,
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
            resumable_operations=self._host._operation_is_resumable,
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


@dataclass(frozen=True, slots=True)
class _PreparedResume:
    """What one `prepare` installed, and therefore what `abandon` must undo."""

    handle: str
    invocation_id: str
    resume_operation_id: str
    serving: Any


class _ResumeInvoker:
    """Carry one interrupted job back to the plugin that owns it.

    `prepare` has to do two things before the durable row moves: resolve the
    activation that is serving right now, and capture a fresh invocation
    authority for `"<operation>.resume"`. The repository rebinds the job to
    both, so the resumed worker's first broker call authorizes against the new
    activation rather than the dead one's.

    That authority is live the moment `prepare` returns, while the durable row
    has not moved yet, so every way out of the window between `prepare` and
    `invoke` has to give it back. `begin_resume` raising (a lost idempotency
    race, a job that turned out not to be resumable) is the ordinary case.
    `abandon` is the explicit undo; `ExternalExtensionHost.job_resume` brackets
    the whole use case so a resume that never reaches `invoke` is abandoned
    even when it fails somewhere nobody thought to name.
    """

    def __init__(self, host: "ExternalExtensionHost") -> None:
        self._host = host
        self._lock = threading.Lock()
        self._pending: dict[str, _PreparedResume] = {}
        self._invoked_operation_ids: set[str] = set()
        self._scope = threading.local()

    def pending_job_ids(self) -> tuple[str, ...]:
        """Jobs that are prepared but not yet invoked or abandoned."""

        with self._lock:
            return tuple(self._pending)

    def begin_request(self) -> None:
        """Open the bracket one `jobs.resume` call runs inside."""

        self._scope.prepared = []

    def end_request(self) -> None:
        """Close the bracket, abandoning whatever never reached `invoke`."""

        prepared = getattr(self._scope, "prepared", None)
        self._scope.prepared = None
        for job_id in prepared or ():
            self._release(job_id)

    def abandon(self, request: ResumeInvocationRequest) -> None:
        """Undo a `prepare` whose resume will not happen after all."""

        self._release(request.job_id)

    def _release(self, job_id: str) -> None:
        """Give back the authority one `prepare` installed for `job_id`.

        Dropping the captured context is what actually revokes the handle.
        The operation authority goes with it unless another resume still needs
        it: a concurrent prepare for the same operation, or one that already
        reached `invoke` and whose worker authorizes against it.
        """

        with self._lock:
            prepared = self._pending.pop(job_id, None)
            if prepared is None:
                return
            operation_id = prepared.resume_operation_id
            still_needed = operation_id in self._invoked_operation_ids or any(
                other.resume_operation_id == operation_id
                for other in self._pending.values()
            )
        self._host._contexts.drop(prepared.invocation_id)
        if not still_needed:
            self._host._operations.pop(operation_id, None)

    def prepare(self, request: ResumeInvocationRequest) -> ResumeTarget | None:
        host = self._host
        record = host._repository.get(request.plugin_id)
        if record is None or record.status is not ExtensionStatus.ENABLED:
            return None
        serving = host._activation.serving(request.plugin_id)
        if serving is None:
            return None
        deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
        grants = frozenset(record.selected.approved_scopes)
        resume_operation = request.resume_operation_id
        host._origins[request.origin_principal_id] = OriginState(
            request.origin_principal_id, host._engine_id, host._audience,
            frozenset({"read", "write"}), grants, grants, deadline, 0,
        )
        host._operations[resume_operation] = OperationAuthority(
            resume_operation, frozenset({"read", "write"}), grants, grants,
        )
        handle = host._plugin_authority.issue(
            serving.identity, request.origin_principal_id, resume_operation,
            expires_at=deadline,
        )
        context = host._plugin_authority.capture(handle, serving.identity)
        with self._lock:
            self._pending[request.job_id] = _PreparedResume(
                handle=handle,
                invocation_id=context.invocation_id,
                resume_operation_id=resume_operation,
                serving=serving,
            )
        scope = getattr(self._scope, "prepared", None)
        if isinstance(scope, list):
            scope.append(request.job_id)
        return ResumeTarget(
            activation_id=serving.identity.activation_id,
            invocation_id=context.invocation_id,
        )

    def invoke(self, request: ResumeInvocationRequest, target: ResumeTarget) -> None:
        with self._lock:
            prepared = self._pending.pop(request.job_id, None)
            if prepared is not None:
                # The durable row is already RUNNING and bound to this
                # invocation, so the operation authority has to outlive this
                # call whether or not the worker answers: a later abandon of
                # the same operation must not deny the running worker.
                self._invoked_operation_ids.add(prepared.resume_operation_id)
        if prepared is None:
            raise HostNotServingError(request.plugin_id)
        handle, serving = prepared.handle, prepared.serving
        state = self._host._authority.activation(serving.identity)
        if state is None:
            raise HostNotServingError(request.plugin_id)
        serving.invocation_channel.invoke(
            request.resume_operation_id,
            request.invocation_params(),
            {
                "activation_id": serving.identity.activation_id,
                "plugin_id": request.plugin_id,
                "invocation_handle": handle,
                "revocation_generation": state.revocation_generation,
            },
            timeout_s=self._host._timeout_s,
        )


class ExternalExtensionHost:
    """Own installation, activation, discovery, invocation, and retained data."""

    def __init__(
        self,
        root: Path | str,
        *,
        artifact_root: Path | str | None = None,
        timeout_s: float = 5.0,
        restart_policy: RestartPolicy = DEFAULT_RESTART_POLICY,
        heartbeat: WorkerHeartbeatSettings = DEFAULT_WORKER_HEARTBEAT,
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
        if not isinstance(heartbeat, WorkerHeartbeatSettings):
            raise HostConflictError("heartbeat settings must be WorkerHeartbeatSettings")
        self._heartbeat = heartbeat
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
        self._resume_invoker = _ResumeInvoker(self)
        self._resume_job = ResumeJobUseCase(self._jobs, invoker=self._resume_invoker)
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
        self._supervisor = ActivationRestartSupervisor(
            lifecycle=self._activation,
            record_reader=self._record_or_none,
            policy=restart_policy,
        )
        self._activation.set_worker_loss_observer(self._supervisor)
        self._closed = False
        self.last_shutdown_report = ShutdownReport()
        self._recover_lifecycle_and_enabled_extensions()

    def close(self) -> ShutdownReport:
        """Quiesce every extension, settle orphaned jobs, and report per record.

        One extension that fails to quiesce never skips the rest: each record
        is isolated, and its failure becomes an entry rather than an exception.
        A record whose worker already died has no serving activation to drain,
        so its jobs are settled directly — otherwise they would stay RUNNING
        forever.
        """

        if self._closed:
            return self.last_shutdown_report
        self._closed = True
        entries: list[ExtensionShutdownEntry] = []
        try:
            self._supervisor.close()
            try:
                records = self.list_extensions()
            except Exception:
                records = ()
            for record in records:
                entries.append(self._shut_down_extension(record))
            self._activation.close()
        finally:
            self.last_shutdown_report = ShutdownReport(tuple(entries))
            self._lock.release()
        return self.last_shutdown_report

    def _shut_down_extension(self, record: ExtensionRecord) -> ExtensionShutdownEntry:
        extension_id = record.extension_id
        if self._activation.serving(extension_id) is None:
            interrupted = 0
            try:
                interrupted = self._activation.settle_orphaned_jobs(record)
            except Exception as error:
                return ExtensionShutdownEntry(
                    extension_id, SHUTDOWN_FAILED, type(error).__name__, 0, CHILD_ABSENT,
                )
            return ExtensionShutdownEntry(
                extension_id, SHUTDOWN_SETTLED, None, interrupted, CHILD_ABSENT,
            )
        try:
            self._activation.quiesce(
                str(uuid.uuid4()), record, deadline_ms=_SHUTDOWN_DEADLINE_MS,
            )
        except Exception as error:
            return ExtensionShutdownEntry(
                extension_id,
                SHUTDOWN_FAILED,
                type(error).__name__,
                self._activation.last_interrupted_job_count(extension_id),
                CHILD_REAPED,
            )
        return ExtensionShutdownEntry(
            extension_id,
            SHUTDOWN_QUIESCED,
            None,
            self._activation.last_interrupted_job_count(extension_id),
            CHILD_REAPED,
        )

    def supervision_report(self, extension_id: str) -> ExtensionSupervisionReport:
        """Worker health, restart attempts, last failure code, and gave_up.

        This is the only place the data is exposed. The frozen
        `extensions.get` result schema is a closed object with no free-form
        status or diagnostics member, so none of it can ride along on that
        contract without a contract addition.
        """

        return self._supervisor.report(extension_id)

    def supervision_reports(self) -> tuple[ExtensionSupervisionReport, ...]:
        """One supervision report per catalogued extension."""

        try:
            records = self.list_extensions()
        except Exception:
            return ()
        return tuple(
            self._supervisor.report(record.extension_id) for record in records
        )

    def _record_or_none(self, extension_id: str) -> ExtensionRecord | None:
        return self._repository.get(extension_id)

    def job_get(self, params: dict[str, Any], *, principal: str) -> dict[str, Any]:
        return self._get_job.execute(params, caller_principal_id=principal)

    def job_cancel(self, params: dict[str, Any], *, principal: str) -> dict[str, Any]:
        return self._cancel_job.execute(params, caller_principal_id=principal)

    def job_resume(self, params: dict[str, Any], *, principal: str) -> dict[str, Any]:
        """Resume one interrupted job, giving back the authority if it fails.

        The bracket is the point: `prepare` issues a live invocation authority
        before the durable row moves, and anything that raises after it —
        `begin_resume` losing an idempotency race most of all — would otherwise
        leave that authority installed with nothing left to use it.
        """

        self._resume_invoker.begin_request()
        try:
            return self._resume_job.execute(params, caller_principal_id=principal)
        finally:
            self._resume_invoker.end_request()

    def _operation_is_resumable(self, operation_id: str) -> bool:
        """Whether this operation's manifest declared its jobs resumable.

        The manifest entry is spread into the catalog untouched, so the
        optional `resumable` key arrives here exactly as the plugin author
        wrote it. An operation that never claimed it creates jobs that
        `jobs.resume` refuses, which is the safe direction.
        """

        return any(
            item["id"] == operation_id and bool(item.get("resumable"))
            for item in self.operation_catalog()
        )

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

    def inspect(self, archive_path: Path | str) -> dict[str, Any]:
        data = Path(archive_path).read_bytes()
        report = validate_project_archive(data)
        if not report.ok:
            raise HostConflictError("archive validation failed")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                manifest = json.loads(archive.read("manifest.json"))
        except (KeyError, ValueError, zipfile.BadZipFile):
            raise HostConflictError("archive validation failed") from None
        return {
            "manifest": manifest,
            "provenance": {"sha256": hashlib.sha256(data).hexdigest()},
        }

    def update(
        self,
        extension_id: str,
        archive_path: Path | str,
        *,
        principal: str,
        idempotency_key: str,
        expected_revision: int,
    ) -> LifecycleReceipt | LifecycleOperation:
        data = Path(archive_path).read_bytes()
        report = validate_project_archive(data)
        if not report.ok or report.manifest.identity.manifest_id != extension_id:
            raise HostConflictError("archive validation failed or manifest ID mismatch")
        staged = stage_archive(data, store_root=self._artifact_root)
        document = json.loads((Path(staged.artifact_path) / "manifest.json").read_text())
        self._persist_manifest(
            extension_id,
            staged.artifact_id,
            staged.artifact_path,
            document,
            activate_current=False,
        )
        artifact = ExecutableArtifact(staged.artifact_id, extension_id,
                                      report.manifest.identity.version,
                                      tuple(report.manifest.contributions.permissions))
        return self._execute(
            LifecycleAction.UPDATE,
            extension_id,
            principal,
            idempotency_key,
            expected_revision,
            candidate=artifact,
        )

    def remove(self, extension_id: str, *, principal: str, idempotency_key: str,
               expected_revision: int):
        return self._execute(LifecycleAction.REMOVE, extension_id, principal,
                             idempotency_key, expected_revision)

    def enable(self, extension_id: str, *, principal: str, idempotency_key: str,
               expected_revision: int) -> LifecycleReceipt | LifecycleOperation:
        record = self.get_extension(extension_id)
        manifest = self._manifest(
            extension_id,
            record.selected.executable.artifact_id,
        )
        permissions = tuple(manifest["document"].get("permissions", ()))
        # Re-enabling is the operator's way to clear a degraded extension, so
        # the ledger goes before the activation rather than after it.
        self._supervisor.reset(extension_id)
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
        # Drop any scheduled replacement first: a disable that raced a pending
        # restart must not be followed by the extension coming back up.
        self._supervisor.reset(extension_id)
        disabled = self._execute(LifecycleAction.DISABLE, extension_id, principal,
                                 idempotency_key, expected_revision)
        self._supervisor.reset(extension_id)
        return disabled

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
            document = self._manifest(record.extension_id, record.selected.executable.artifact_id)["document"]
            result.extend({"extension_id": record.extension_id, **item} for item in document["contributes"].get("operations", ()))
        return tuple(result)

    def ui_contributions(self) -> tuple[dict[str, str], ...]:
        result = []
        for record in self.list_extensions():
            if record.status is ExtensionStatus.ENABLED:
                result.extend({"extension_id": record.extension_id, **item}
                              for item in self._manifest(record.extension_id, record.selected.executable.artifact_id)["document"]["contributes"].get("panels", ()))
        return tuple(result)

    def panel_get(self, panel_id: str) -> dict[str, Any]:
        for descriptor in self.ui_contributions():
            if descriptor["id"] == panel_id:
                record = self.get_extension(descriptor["extension_id"])
                manifest = self._manifest(descriptor["extension_id"], record.selected.executable.artifact_id)
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
        record = self.get_extension(extension_id)
        manifest = self._manifest(extension_id, record.selected.executable.artifact_id)
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
        grants = frozenset(self.get_extension(extension_id).selected.approved_scopes)
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

    def _recover_lifecycle_and_enabled_extensions(self) -> None:
        records = self.list_extensions()
        # Startup sweep. A previous engine process can only have ended while
        # its plugin jobs were queued or running, and nothing will ever report
        # on them again, so they are interrupted before anything is re-admitted.
        # Doing it before the revocations below means a revoke that raises
        # cannot leave a job showing as running.
        for record in records:
            self._activation.settle_orphaned_jobs(record)
        for record in records:
            prior_identity = self._authority.identity_for(record.selected)
            if prior_identity is not None:
                self._authority.revoke(prior_identity)

        pending = self._all_pending_operations()
        recovered: set[str] = set()
        blocked = {operation.request.extension_id for operation in pending}
        for operation in pending:
            if operation.phase is LifecyclePhase.RESOLUTION_REQUIRED:
                continue
            result = self._lifecycle.recover_operation(operation)
            if isinstance(result, LifecycleReceipt) and result.record is not None:
                recovered.add(result.record.extension_id)

        for record in records:
            if (
                record.status is ExtensionStatus.ENABLED
                and record.extension_id not in blocked
                and record.extension_id not in recovered
            ):
                operation_id = str(uuid.uuid4())
                validated = self._activation.validate(operation_id, record.selected)
                self._activation.admit(
                    operation_id,
                    record,
                    validated,
                    expected_data_revision=validated.validated_data_revision,
                )

    def _all_pending_operations(self) -> tuple[LifecycleOperation, ...]:
        pending: list[LifecycleOperation] = []
        after_operation_id: str | None = None
        while True:
            page = self._lifecycle.list_pending(
                after_operation_id=after_operation_id,
                limit=_LIFECYCLE_RECOVERY_PAGE_SIZE,
            )
            pending.extend(page)
            if len(page) < _LIFECYCLE_RECOVERY_PAGE_SIZE:
                break
            after_operation_id = page[-1].request.operation_id
        return tuple(pending)

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _ensure_catalog(self) -> None:
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS external_host_catalog (extension_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, artifact_path TEXT NOT NULL, manifest_json TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS external_host_artifacts (artifact_id TEXT PRIMARY KEY, extension_id TEXT NOT NULL, artifact_path TEXT NOT NULL, manifest_json TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS external_host_invocations (principal TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_digest TEXT NOT NULL, state TEXT NOT NULL, output_json TEXT, PRIMARY KEY(principal,idempotency_key))")
            connection.execute("INSERT OR IGNORE INTO external_host_artifacts SELECT artifact_id,extension_id,artifact_path,manifest_json FROM external_host_catalog")

    def _persist_manifest(self, extension_id, artifact_id, artifact_path, document, *, activate_current=True) -> None:
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            prior = connection.execute("SELECT manifest_json FROM external_host_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if prior is not None and prior[0] != encoded:
                raise HostConflictError("artifact identity collision")
            connection.execute("INSERT OR IGNORE INTO external_host_artifacts VALUES (?,?,?,?)", (artifact_id, extension_id, artifact_path, encoded))
            if activate_current:
                connection.execute("INSERT OR REPLACE INTO external_host_catalog VALUES (?,?,?,?)", (extension_id, artifact_id, artifact_path, encoded))

    def _manifest(self, extension_id: str, artifact_id: str | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            if artifact_id is None:
                row = connection.execute("SELECT artifact_id,artifact_path,manifest_json FROM external_host_catalog WHERE extension_id=?", (extension_id,)).fetchone()
            else:
                row = connection.execute("SELECT artifact_id,artifact_path,manifest_json FROM external_host_artifacts WHERE extension_id=? AND artifact_id=?", (extension_id, artifact_id)).fetchone()
        if row is None:
            raise KeyError(extension_id)
        return {"artifact_id": row[0], "artifact_path": row[1], "document": json.loads(row[2])}

    def _approved_scopes(self, executable: ExecutableArtifact) -> tuple[str, ...]:
        for operation in self._all_pending_operations():
            candidate = operation.candidate
            if (
                candidate is not None
                and candidate.executable.artifact_id == executable.artifact_id
            ):
                return candidate.approved_scopes

            request = operation.request
            previous = operation.previous
            if previous is None:
                continue
            previous_artifact_id = previous.selected.executable.artifact_id
            if (
                request.action is LifecycleAction.CHANGE_GRANTS
                and previous_artifact_id == executable.artifact_id
            ):
                return request.approved_scopes
            if (
                request.action is LifecycleAction.ENABLE
                and previous_artifact_id == executable.artifact_id
            ):
                return previous.selected.approved_scopes
            if (
                request.action is LifecycleAction.UPDATE
                and request.candidate is not None
                and request.candidate.artifact_id == executable.artifact_id
            ):
                requested = frozenset(request.candidate.requested_scopes)
                return tuple(
                    scope
                    for scope in previous.selected.approved_scopes
                    if scope in requested
                )
        record = self._repository.get(executable.extension_id)
        if record is not None and record.selected.executable.artifact_id == executable.artifact_id:
            return record.selected.approved_scopes
        raise HostConflictError("artifact is not selected")

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
