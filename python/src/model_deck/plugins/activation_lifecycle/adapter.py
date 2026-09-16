"""Process-backed implementation of the extension activation lifecycle port."""
from __future__ import annotations

import hashlib
import inspect
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from model_deck.engine.extensions.ports import (
    ExecutableArtifact,
    ExtensionActivationLifecycle,
    ExtensionRecord,
    ExtensionStatus,
    FrozenData,
    LifecycleConflictError,
    SelectedInstallation,
    ValidatedActivation,
)
from model_deck.engine.jobs.ports import JobOwner, PluginJobRepository
from model_deck.engine.plugin_authority import ActivationIdentity
from model_deck.engine.plugin_data.versioning import (
    PluginDataBinding,
    VersionedPluginDataStore,
)
from model_deck.plugins.lifecycle_session import LifecycleSession, SessionState
from model_deck.plugins.process_runtime import (
    ProcessRuntime,
    ProcessRuntimeConfig,
    ProviderChannel,
)
from model_deck.plugins.process_runtime.health import (
    WorkerHealthState,
    WorkerLossCode,
    WorkerLossEvent,
)
from model_deck.plugins.process_runtime.invocation_channel import (
    BrokerRequestHandler,
    InvocationChannel,
)

DEFAULT_LIVENESS_POLL_S = 0.1
"""How often the supervision thread re-checks each serving child.

`ProcessRuntime` reports an unexpected child death through
`worker_loss_listener` the moment its reader thread notices. That report is the
fast path. This poll is the slow, independent one: it asks each serving runtime
for its invocation channel, which fails as soon as the runtime has stopped, and
turns that failure into the same `WorkerLossEvent`. Keeping both means an
activation is still settled when the runtime cannot name a listener — for
example while `worker_loss_listener` is not yet accepted by the installed
`ProcessRuntime`.
"""

_RUNTIME_ACCEPTS_LOSS_LISTENER = (
    "worker_loss_listener" in inspect.signature(ProcessRuntime).parameters
)
"""Whether the installed `ProcessRuntime` takes a loss listener at construction.

Checked once, from the real signature, rather than by catching `TypeError`
around every construction, so an unrelated argument mistake still raises.
"""


class ActivationLifecycleConfigurationError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid process activation lifecycle configuration")


class ActivationLifecycleConflictError(LifecycleConflictError):
    def __init__(self) -> None:
        super().__init__("process activation lifecycle conflict")


class ActivationLifecycleOperationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("process activation lifecycle operation failed")


@dataclass(frozen=True, slots=True)
class ResolvedArtifactLaunch:
    """Trusted launch material for one exact immutable artifact."""

    executable: ExecutableArtifact
    runtime_config: ProcessRuntimeConfig
    allowed_broker_methods: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.executable) is not ExecutableArtifact:
            raise ActivationLifecycleConfigurationError()
        if type(self.runtime_config) is not ProcessRuntimeConfig:
            raise ActivationLifecycleConfigurationError()
        if type(self.allowed_broker_methods) is not tuple or any(
            type(method) is not str or not method
            for method in self.allowed_broker_methods
        ):
            raise ActivationLifecycleConfigurationError()


@runtime_checkable
class ArtifactLaunchResolver(Protocol):
    def resolve(self, executable: ExecutableArtifact) -> ResolvedArtifactLaunch: ...


@runtime_checkable
class ActivationJobSettlement(Protocol):
    """The only job-repository capability this adapter needs.

    The adapter settles work owned by an activation it is tearing down; it
    never creates, claims, resumes, or reads a job. Gating construction on this
    narrow shape instead of the whole `PluginJobRepository` protocol keeps the
    configuration check honest about the dependency and keeps the adapter from
    rejecting a real repository because that protocol grew a method the
    adapter does not call.
    """

    def list_active_for_activation(self, owner: JobOwner) -> list: ...

    def mark_worker_crashed(self, owner: JobOwner): ...


@dataclass(frozen=True, slots=True)
class WorkerLossNotice:
    """One settled unexpected worker loss, reported after cleanup finished.

    `jobs_interrupted` counts the durable jobs this settlement moved out of
    QUEUED/RUNNING. They are never replayed. `settled_cleanly` is False when
    revoking authority, deactivating the session, or closing the runtime
    raised; the activation is unpublished either way.
    """

    extension_id: str
    activation_id: str
    failure_code: WorkerLossCode
    at_monotonic: float
    exit_code: int | None
    jobs_interrupted: int
    settled_cleanly: bool


@dataclass(frozen=True, slots=True)
class WorkerSupervisionState:
    """What this adapter knows about one extension's worker right now."""

    extension_id: str
    health_state: WorkerHealthState
    last_loss: WorkerLossNotice | None
    jobs_interrupted_at_last_settlement: int


@runtime_checkable
class WorkerLossObserver(Protocol):
    """Receives each settled loss once, after the adapter finished cleanup.

    The call happens on the adapter's supervision thread with no adapter lock
    held, so an observer may start a replacement activation. It must not raise.
    """

    def on_worker_loss_settled(self, notice: WorkerLossNotice) -> None: ...


class _WorkerLossGuard:
    """One activation's loss latch, created before the runtime it watches.

    It is the `WorkerLossListener` handed to `ProcessRuntime`, and it is also
    what the supervision poll reports through, so both detectors converge on
    one queued settlement. `finish()` is how an operator teardown claims the
    activation: a loss reported after that is ignored, which is what makes
    quiesce, disable, and an unexpected death mutually no-op.
    """

    def __init__(self, enqueue: Callable[["_WorkerLossGuard"], None]) -> None:
        self._lock = threading.Lock()
        self._enqueue = enqueue
        self._extension_id: str | None = None
        self._activation_ref: str | None = None
        self._event: WorkerLossEvent | None = None
        self._finished = False
        self._reported = False

    @property
    def extension_id(self) -> str | None:
        with self._lock:
            return self._extension_id

    @property
    def activation_ref(self) -> str | None:
        with self._lock:
            return self._activation_ref

    @property
    def event(self) -> WorkerLossEvent | None:
        with self._lock:
            return self._event

    def bind(self, extension_id: str, activation_ref: str) -> None:
        """Name the activation this guard belongs to, once validation made one.

        A worker that dies before this runs is handled by validation's own
        failure path, so an unbound guard reports nothing.
        """

        with self._lock:
            self._extension_id = extension_id
            self._activation_ref = activation_ref

    def finish(self) -> None:
        """Claim the activation for an operator teardown; ignore later losses."""

        with self._lock:
            self._finished = True

    def is_finished(self) -> bool:
        with self._lock:
            return self._finished

    def claim_settlement(self) -> bool:
        """True for the one caller that may settle this loss."""

        with self._lock:
            if self._finished:
                return False
            self._finished = True
            return True

    def on_worker_lost(self, event: WorkerLossEvent) -> None:
        """Record one loss and hand it to the supervision thread. Never blocks."""

        with self._lock:
            if self._finished or self._reported or self._activation_ref is None:
                return
            if not isinstance(event, WorkerLossEvent):
                return
            self._reported = True
            self._event = event
        self._enqueue(self)


@runtime_checkable
class ActivationAuthorityController(Protocol):
    """Trusted mutable activation state; workers never receive this object."""

    def register_non_serving(
        self,
        identity: ActivationIdentity,
        selected: SelectedInstallation,
    ) -> None: ...

    def revoke(self, identity: ActivationIdentity) -> None: ...

    def admit(
        self,
        identity: ActivationIdentity,
        selected: SelectedInstallation,
    ) -> None: ...

    def identity_for(
        self,
        selected: SelectedInstallation,
    ) -> ActivationIdentity | None: ...


@runtime_checkable
class ActivationBrokerFactory(Protocol):
    def create(
        self,
        identity: ActivationIdentity,
        binding: PluginDataBinding,
        allowed_methods: tuple[str, ...],
    ) -> BrokerRequestHandler: ...


@dataclass(frozen=True, slots=True)
class ServingActivation:
    """Read-only serving view. It deliberately contains no token or process handle."""

    identity: ActivationIdentity
    selected: SelectedInstallation
    invocation_channel: InvocationChannel
    provider_channel: ProviderChannel | None


class _BrokerGate:
    """Late-bind a handler after the worker supplies its authenticated activation id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handler: BrokerRequestHandler | None = None

    def bind(self, handler: BrokerRequestHandler) -> None:
        if not callable(handler):
            raise ActivationLifecycleConfigurationError()
        with self._lock:
            if self._handler is not None:
                raise ActivationLifecycleConflictError()
            self._handler = handler

    def __call__(self, activation_id: str, method: str, params):
        with self._lock:
            handler = self._handler
        if handler is None:
            raise ActivationLifecycleOperationError()
        return handler(activation_id, method, params)


@dataclass(slots=True)
class _OwnedActivation:
    operation_id: str
    freeze_operation_id: str | None
    activation_operation_id: str
    validated: ValidatedActivation
    frozen: FrozenData | None
    identity: ActivationIdentity
    runtime: ProcessRuntime
    session: LifecycleSession
    serving_view: ServingActivation
    loss_guard: _WorkerLossGuard


class ProcessExtensionActivationLifecycle(ExtensionActivationLifecycle):
    """Own exact child processes and synchronize activation with versioned data."""

    def __init__(
        self,
        *,
        engine_instance_id: str,
        audience: str,
        api_major: int,
        api_minor: int,
        data_store: VersionedPluginDataStore,
        job_repository: PluginJobRepository,
        launch_resolver: ArtifactLaunchResolver,
        authority_controller: ActivationAuthorityController,
        broker_factory: ActivationBrokerFactory,
        token_factory: Callable[[], str],
        nonce_factory: Callable[[], str],
        activation_ref_factory: Callable[[], str] = lambda: f"ref:activation.{uuid.uuid4()}",
        worker_loss_observer: WorkerLossObserver | None = None,
        liveness_poll_s: float = DEFAULT_LIVENESS_POLL_S,
    ) -> None:
        if (
            type(engine_instance_id) is not str
            or not engine_instance_id
            or type(audience) is not str
            or not audience
            or type(api_major) is not int
            or api_major < 0
            or type(api_minor) is not int
            or api_minor < 0
            or not isinstance(data_store, VersionedPluginDataStore)
            or not isinstance(job_repository, ActivationJobSettlement)
            or not isinstance(launch_resolver, ArtifactLaunchResolver)
            or not isinstance(authority_controller, ActivationAuthorityController)
            or not isinstance(broker_factory, ActivationBrokerFactory)
            or not callable(token_factory)
            or not callable(nonce_factory)
            or not callable(activation_ref_factory)
            or (
                worker_loss_observer is not None
                and not isinstance(worker_loss_observer, WorkerLossObserver)
            )
            or type(liveness_poll_s) not in (int, float)
            or liveness_poll_s < 0
        ):
            raise ActivationLifecycleConfigurationError()
        self._engine_instance_id = engine_instance_id
        self._audience = audience
        self._api_major = api_major
        self._api_minor = api_minor
        self._data_store = data_store
        self._jobs = job_repository
        self._launch_resolver = launch_resolver
        self._authority = authority_controller
        self._broker_factory = broker_factory
        self._token_factory = token_factory
        self._nonce_factory = nonce_factory
        self._activation_ref_factory = activation_ref_factory
        self._lock = threading.RLock()
        self._effects = threading.Lock()
        self._by_ref: dict[str, _OwnedActivation] = {}
        self._operation_ref: dict[str, str] = {}
        self._serving: dict[str, _OwnedActivation] = {}
        self._quiesced: dict[str, ExtensionRecord] = {}
        self._loss_observer = worker_loss_observer
        self._liveness_poll_s = float(liveness_poll_s)
        self._losses: dict[str, WorkerLossNotice] = {}
        self._interrupted_counts: dict[str, int] = {}
        self._loss_queue: queue.SimpleQueue[_WorkerLossGuard] = queue.SimpleQueue()
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._closed = False

    def set_worker_loss_observer(self, observer: WorkerLossObserver | None) -> None:
        """Install the restart supervisor after both objects exist.

        Composition is circular by nature — the supervisor replaces activations
        through this lifecycle, and this lifecycle hands it each settled loss —
        so the edge is closed here rather than in either constructor.
        """

        if observer is not None and not isinstance(observer, WorkerLossObserver):
            raise ActivationLifecycleConfigurationError()
        with self._lock:
            self._loss_observer = observer

    def worker_supervision(self, extension_id: str) -> WorkerSupervisionState:
        """Report this extension's worker health as the adapter sees it.

        HEALTHY means an activation is published right now. DEAD means the last
        thing that happened to this extension's worker was an unexpected loss.
        STARTING covers everything else: never activated, or deliberately
        quiesced by an operator.
        """

        if type(extension_id) is not str or not extension_id:
            raise ActivationLifecycleConflictError()
        with self._lock:
            serving = self._serving.get(extension_id)
            loss = self._losses.get(extension_id)
            interrupted = self._interrupted_counts.get(extension_id, 0)
        if serving is not None:
            state = WorkerHealthState.HEALTHY
        elif loss is not None:
            state = WorkerHealthState.DEAD
        else:
            state = WorkerHealthState.STARTING
        return WorkerSupervisionState(extension_id, state, loss, interrupted)

    def settle_orphaned_jobs(self, record: ExtensionRecord) -> int:
        """Interrupt jobs owned by a prior activation nothing is serving.

        Used at startup, so a plugin job can never survive an engine restart as
        RUNNING, and at shutdown for a record whose worker already died. It is
        a no-op while an activation is published: that one is quiesced instead.
        Returns how many jobs it moved to INTERRUPTED; they are never replayed.
        """

        if type(record) is not ExtensionRecord:
            raise ActivationLifecycleConflictError()
        with self._effects:
            with self._lock:
                if self._serving.get(record.extension_id) is not None:
                    return 0
            identity = self._authority.identity_for(record.selected)
            if identity is None:
                return 0
            owner = JobOwner(identity.plugin_id, identity.activation_id)
            interrupted = len(self._jobs.mark_worker_crashed(owner).interrupted_job_ids)
            with self._lock:
                self._interrupted_counts[record.extension_id] = interrupted
            return interrupted

    def last_interrupted_job_count(self, extension_id: str) -> int:
        """Jobs interrupted by this extension's most recent settlement."""

        with self._lock:
            return self._interrupted_counts.get(extension_id, 0)

    def close(self) -> None:
        """Stop supervising. Owned activations are left to the caller's teardown."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._watch_thread
            self._watch_thread = None
        self._watch_stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def serving(self, extension_id: str) -> ServingActivation | None:
        if type(extension_id) is not str or not extension_id:
            raise ActivationLifecycleConflictError()
        with self._lock:
            owned = self._serving.get(extension_id)
            return None if owned is None else owned.serving_view

    def quiesce(
        self,
        operation_id: str,
        previous: ExtensionRecord,
        *,
        deadline_ms: int,
    ) -> None:
        if type(previous) is not ExtensionRecord:
            raise ActivationLifecycleConflictError()
        with self._effects:
            with self._lock:
                owned = self._serving.get(previous.extension_id)
                if owned is not None and owned.validated.selection != previous.selected:
                    raise ActivationLifecycleConflictError()
            identity = (
                owned.identity
                if owned is not None
                else self._authority.identity_for(previous.selected)
            )
            if identity is None:
                if previous.status is ExtensionStatus.ENABLED:
                    raise ActivationLifecycleConflictError()
                self._remember_quiesced(operation_id, previous)
                return
            self._require_identity(
                identity,
                previous.selected,
                require_current_engine=owned is not None,
            )
            errors: list[Exception] = []
            try:
                with self._data_store.mutation_barrier():
                    self._authority.revoke(identity)
            except Exception as error:
                errors.append(error)
            if owned is not None:
                with self._lock:
                    self._unpublish(owned)
            if owned is not None:
                try:
                    owned.runtime.run_drain(owned.session, deadline_ms)
                except Exception as error:
                    errors.append(error)
            self._finish_owned_work(identity, owned, errors)
            self._remember_quiesced(operation_id, previous)
            if errors:
                raise ActivationLifecycleOperationError() from None
            if owned is not None:
                with self._lock:
                    self._remove(owned)

    def validate(
        self,
        operation_id: str,
        candidate: SelectedInstallation,
    ) -> ValidatedActivation:
        if type(candidate) is not SelectedInstallation:
            raise ActivationLifecycleConflictError()
        with self._effects:
            old, old_was_serving = self._take_operation_candidate(operation_id)
            if old is not None:
                self._discard(old)
            launch = self._launch_resolver.resolve(candidate.executable)
            if (
                type(launch) is not ResolvedArtifactLaunch
                or launch.executable != candidate.executable
            ):
                raise ActivationLifecycleConflictError()
            gate = _BrokerGate()
            guard = _WorkerLossGuard(self._loss_queue.put)
            runtime_arguments = {
                "allowed_broker_methods": launch.allowed_broker_methods,
                "broker_request_handler": gate,
            }
            if _RUNTIME_ACCEPTS_LOSS_LISTENER:
                runtime_arguments["worker_loss_listener"] = guard
            runtime = ProcessRuntime(launch.runtime_config, **runtime_arguments)
            session: LifecycleSession | None = None
            identity: ActivationIdentity | None = None
            registered = False
            try:
                runtime.spawn()
                session = LifecycleSession(
                    expected_plugin_id=candidate.executable.extension_id,
                    expected_plugin_version=candidate.executable.version,
                    offered_api_major=self._api_major,
                    offered_api_minor=self._api_minor,
                    activation_token=self._token_factory(),
                    allowed_broker_methods=launch.allowed_broker_methods,
                )
                runtime.run_hello(session, self._nonce_factory())
                runtime.run_activation(session)
                activation_id = session.activation_id
                if activation_id is None:
                    raise ActivationLifecycleConflictError()
                identity = ActivationIdentity(
                    self._engine_instance_id,
                    self._audience,
                    activation_id,
                    candidate.executable.extension_id,
                    candidate.executable.version,
                )
                self._authority.register_non_serving(identity, candidate)
                registered = True
                handler = self._broker_factory.create(
                    identity,
                    PluginDataBinding(
                        candidate.executable.extension_id,
                        candidate.data_ref,
                        candidate.activation_generation,
                    ),
                    launch.allowed_broker_methods,
                )
                gate.bind(handler)
                previous = self._quiesced.get(operation_id)
                if not old_was_serving and self._uses_prior_freeze(previous, candidate):
                    assert previous is not None
                    freeze_operation_id = operation_id
                    frozen = self._data_store.freeze(operation_id, previous.selected)
                    revision = frozen.final_revision
                elif not old_was_serving and self._is_restored_prior(previous, candidate):
                    freeze_operation_id = None
                    frozen = None
                    revision = self._data_store.selected_revision(candidate)
                else:
                    # Already-selected generations need a new validation receipt:
                    # replaying their original activation receipt after thaw would
                    # not apply a new writable-state transition. A sealed stage can
                    # only be frozen by its lifecycle operation, so fall back to
                    # that operation after the fresh receipt is rejected.
                    freeze_operation_id = str(uuid.uuid4())
                    try:
                        frozen = self._data_store.freeze(freeze_operation_id, candidate)
                    except LifecycleConflictError:
                        freeze_operation_id = operation_id
                        try:
                            frozen = self._data_store.freeze(
                                freeze_operation_id,
                                candidate,
                            )
                        except LifecycleConflictError:
                            # After restart, a durable rollback may already have
                            # thawed a retained prior dataset for a new monotonic
                            # generation. It is sealed, so read its exact revision;
                            # activate_selected still proves the restoration.
                            freeze_operation_id = None
                            frozen = None
                            revision = self._data_store.selected_revision(candidate)
                        else:
                            revision = frozen.final_revision
                    else:
                        revision = frozen.final_revision
                activation_operation_id = freeze_operation_id or operation_id
                validated = ValidatedActivation(
                    self._activation_ref_factory(),
                    candidate,
                    revision,
                )
                invocation_channel = runtime.invocation_channel()
                serving_view = ServingActivation(
                    identity,
                    candidate,
                    invocation_channel,
                    runtime.provider_channel(),
                )
                owned = _OwnedActivation(
                    operation_id,
                    freeze_operation_id,
                    activation_operation_id,
                    validated,
                    frozen,
                    identity,
                    runtime,
                    session,
                    serving_view,
                    guard,
                )
                with self._lock:
                    if validated.activation_ref in self._by_ref:
                        raise ActivationLifecycleConflictError()
                    self._by_ref[validated.activation_ref] = owned
                    self._operation_ref[operation_id] = validated.activation_ref
                guard.bind(identity.plugin_id, validated.activation_ref)
                self._start_supervision()
                return validated
            except Exception:
                errors: list[Exception] = []
                if identity is not None and registered:
                    try:
                        with self._data_store.mutation_barrier():
                            self._authority.revoke(identity)
                    except Exception as error:
                        errors.append(error)
                    self._finish_owned_work(identity, None, errors)
                runtime.close()
                raise

    def revoke(
        self,
        operation_id: str,
        activation: ValidatedActivation | None,
    ) -> str:
        with self._effects:
            owned = None
            if activation is not None:
                if type(activation) is not ValidatedActivation:
                    raise ActivationLifecycleConflictError()
                with self._lock:
                    owned = self._by_ref.get(activation.activation_ref)
                    if owned is not None and (
                        owned.validated != activation
                        or owned.operation_id != operation_id
                    ):
                        raise ActivationLifecycleConflictError()
                if owned is not None:
                    errors: list[Exception] = []
                    try:
                        with self._data_store.mutation_barrier():
                            self._authority.revoke(owned.identity)
                    finally:
                        with self._lock:
                            self._unpublish(owned)
                        self._finish_owned_work(owned.identity, owned, errors)
                    if errors:
                        raise ActivationLifecycleOperationError() from None
                    with self._lock:
                        self._remove(owned)
            return self._revocation_ref(operation_id, activation)

    def admit(
        self,
        operation_id: str,
        record: ExtensionRecord,
        activation: ValidatedActivation | None,
        *,
        expected_data_revision: int,
    ) -> None:
        if type(record) is not ExtensionRecord or type(expected_data_revision) is not int:
            raise ActivationLifecycleConflictError()
        enabled = record.status is ExtensionStatus.ENABLED
        if enabled != (activation is not None):
            raise ActivationLifecycleConflictError()
        with self._effects:
            candidate = self._candidate_for_admission(record, activation)
            if candidate is not None and candidate.operation_id != operation_id:
                raise ActivationLifecycleConflictError()
            if (
                candidate is not None
                and expected_data_revision
                != candidate.validated.validated_data_revision
            ):
                raise ActivationLifecycleConflictError()
            with self._lock:
                old = self._serving.get(record.extension_id)
            if candidate is not None and old is candidate:
                return
            retired_old: _OwnedActivation | None = None
            cleanup_errors: list[Exception] = []
            try:
                with self._data_store.mutation_barrier():
                    if old is not None:
                        self._authority.revoke(old.identity)
                        with self._lock:
                            self._unpublish(old)
                        retired_old = old
                    if candidate is not None and candidate.frozen is not None:
                        assert candidate.freeze_operation_id is not None
                        self._data_store.thaw(
                            candidate.freeze_operation_id,
                            candidate.frozen,
                        )
                    elif candidate is None:
                        previous = self._quiesced.get(operation_id)
                        if self._is_normal_same_data_transition(previous, record):
                            assert previous is not None
                            frozen = self._data_store.freeze(
                                operation_id,
                                previous.selected,
                            )
                            self._data_store.thaw(operation_id, frozen)
                    self._data_store.activate_selected(
                        (
                            candidate.activation_operation_id
                            if candidate is not None
                            else operation_id
                        ),
                        record.selected,
                        expected_data_revision=expected_data_revision,
                        enabled=enabled,
                    )
                    if candidate is not None:
                        self._authority.admit(candidate.identity, record.selected)
                        with self._lock:
                            self._serving[record.extension_id] = candidate
            finally:
                if retired_old is not None:
                    self._finish_owned_work(
                        retired_old.identity,
                        retired_old,
                        cleanup_errors,
                    )
                    if not cleanup_errors:
                        with self._lock:
                            self._remove(retired_old)
            if cleanup_errors:
                raise ActivationLifecycleOperationError() from None

    def _candidate_for_admission(
        self,
        record: ExtensionRecord,
        activation: ValidatedActivation | None,
    ) -> _OwnedActivation | None:
        if activation is None:
            return None
        if activation.selection != record.selected:
            raise ActivationLifecycleConflictError()
        with self._lock:
            owned = self._by_ref.get(activation.activation_ref)
        if owned is None or owned.validated != activation:
            raise ActivationLifecycleConflictError()
        if activation.validated_data_revision < 0:
            raise ActivationLifecycleConflictError()
        return owned

    def _take_operation_candidate(
        self,
        operation_id: str,
    ) -> tuple[_OwnedActivation | None, bool]:
        with self._lock:
            ref = self._operation_ref.get(operation_id)
            if ref is None:
                return None, False
            owned = self._by_ref.get(ref)
            was_serving = (
                owned is not None
                and self._serving.get(owned.identity.plugin_id) is owned
            )
            return owned, was_serving

    def _discard(self, owned: _OwnedActivation) -> None:
        errors: list[Exception] = []
        try:
            with self._data_store.mutation_barrier():
                self._authority.revoke(owned.identity)
        finally:
            with self._lock:
                self._unpublish(owned)
            self._finish_owned_work(owned.identity, owned, errors)
        if errors:
            raise ActivationLifecycleOperationError() from None
        with self._lock:
            self._remove(owned)

    def _finish_owned_work(
        self,
        identity: ActivationIdentity,
        owned: _OwnedActivation | None,
        errors: list[Exception],
    ) -> int:
        owner = JobOwner(identity.plugin_id, identity.activation_id)
        interrupted = 0
        if owned is not None:
            # Claim the activation before closing its runtime, so the loss the
            # close itself provokes is recognised as this teardown's own.
            owned.loss_guard.finish()
        try:
            interrupted = len(self._jobs.mark_worker_crashed(owner).interrupted_job_ids)
        except Exception as error:
            errors.append(error)
        if owned is not None:
            try:
                if owned.session.state in (SessionState.ACTIVE, SessionState.DRAINING):
                    owned.session.deactivate()
            except Exception as error:
                errors.append(error)
            try:
                owned.runtime.close()
            except Exception as error:
                errors.append(error)
        try:
            if self._jobs.list_active_for_activation(owner):
                errors.append(ActivationLifecycleOperationError())
        except Exception as error:
            errors.append(error)
        with self._lock:
            self._interrupted_counts[identity.plugin_id] = interrupted
        return interrupted

    def _start_supervision(self) -> None:
        with self._lock:
            if self._closed or self._watch_thread is not None:
                return
            thread = threading.Thread(
                target=self._supervise,
                name="model-deck-activation-supervision",
                daemon=True,
            )
            self._watch_thread = thread
        thread.start()

    def _supervise(self) -> None:
        """Settle reported losses promptly; re-check serving children between."""

        poll_s = self._liveness_poll_s if self._liveness_poll_s > 0 else 0.5
        while not self._watch_stop.is_set():
            try:
                guard = self._loss_queue.get(timeout=poll_s)
            except queue.Empty:
                if self._liveness_poll_s > 0:
                    self._poll_serving_liveness()
                continue
            try:
                self._settle_worker_loss(guard)
            except Exception:
                # A settlement that cannot finish must not take the supervision
                # thread down; the activation stays unpublished either way.
                continue

    def _poll_serving_liveness(self) -> None:
        with self._lock:
            serving = tuple(self._serving.values())
        for owned in serving:
            if owned.loss_guard.is_finished():
                continue
            try:
                owned.runtime.invocation_channel()
            except Exception:
                owned.loss_guard.on_worker_lost(
                    WorkerLossEvent(
                        owned.identity.activation_id,
                        WorkerLossCode.EXITED,
                        time.monotonic(),
                    )
                )

    def _settle_worker_loss(self, guard: _WorkerLossGuard) -> None:
        """Tear down one activation whose worker died, then report it once.

        This is quiesce without the drain: there is no child left to ask. The
        `_effects` lock is what makes it exclusive with an operator quiesce or
        disable — whichever arrives first settles the activation, and the other
        finds it gone and does nothing.
        """

        activation_ref = guard.activation_ref
        event = guard.event
        extension_id = guard.extension_id
        if activation_ref is None or event is None or extension_id is None:
            return
        notice: WorkerLossNotice | None = None
        observer: WorkerLossObserver | None = None
        with self._effects:
            with self._lock:
                owned = self._by_ref.get(activation_ref)
            if owned is None or owned.loss_guard is not guard:
                guard.finish()
                return
            if not guard.claim_settlement():
                return
            errors: list[Exception] = []
            try:
                with self._data_store.mutation_barrier():
                    self._authority.revoke(owned.identity)
            except Exception as error:
                errors.append(error)
            with self._lock:
                self._unpublish(owned)
            interrupted = self._finish_owned_work(owned.identity, owned, errors)
            with self._lock:
                self._remove(owned)
            notice = WorkerLossNotice(
                extension_id,
                owned.identity.activation_id,
                event.failure_code,
                event.at_monotonic,
                event.exit_code,
                interrupted,
                not errors,
            )
            with self._lock:
                self._losses[extension_id] = notice
                observer = self._loss_observer
        if observer is not None:
            # Outside `_effects`: the observer may start a replacement, and a
            # replacement runs validate and admit, which need that lock.
            observer.on_worker_loss_settled(notice)

    def _remove(self, owned: _OwnedActivation) -> None:
        self._by_ref.pop(owned.validated.activation_ref, None)
        if self._operation_ref.get(owned.operation_id) == owned.validated.activation_ref:
            self._operation_ref.pop(owned.operation_id, None)
        self._unpublish(owned)

    def _unpublish(self, owned: _OwnedActivation) -> None:
        if self._serving.get(owned.identity.plugin_id) is owned:
            self._serving.pop(owned.identity.plugin_id, None)

    def _remember_quiesced(
        self,
        operation_id: str,
        previous: ExtensionRecord,
    ) -> None:
        with self._lock:
            stale_operations = tuple(
                stale_operation
                for stale_operation, stale_record in self._quiesced.items()
                if stale_operation != operation_id
                and stale_record.extension_id == previous.extension_id
            )
            for stale_operation in stale_operations:
                self._quiesced.pop(stale_operation, None)
            self._quiesced[operation_id] = previous

    def _require_identity(
        self,
        identity: ActivationIdentity,
        selected: SelectedInstallation,
        *,
        require_current_engine: bool,
    ) -> None:
        if (
            type(identity) is not ActivationIdentity
            or not identity.engine_instance_id
            or (
                require_current_engine
                and identity.engine_instance_id != self._engine_instance_id
            )
            or identity.audience != self._audience
            or identity.plugin_id != selected.executable.extension_id
            or identity.plugin_version != selected.executable.version
        ):
            raise ActivationLifecycleConflictError()

    @staticmethod
    def _uses_prior_freeze(
        previous: ExtensionRecord | None,
        candidate: SelectedInstallation,
    ) -> bool:
        return (
            previous is not None
            and previous.selected.data_ref == candidate.data_ref
            and previous.selected.executable == candidate.executable
            and candidate.activation_generation
            == previous.selected.activation_generation + 1
        )

    @staticmethod
    def _is_restored_prior(
        previous: ExtensionRecord | None,
        candidate: SelectedInstallation,
    ) -> bool:
        return (
            previous is not None
            and previous.selected.data_ref == candidate.data_ref
            and previous.selected.executable == candidate.executable
            and candidate.activation_generation
            > previous.selected.activation_generation + 1
        )

    @staticmethod
    def _is_normal_same_data_transition(
        previous: ExtensionRecord | None,
        record: ExtensionRecord,
    ) -> bool:
        if previous is None or record.selected == previous.selected:
            return False
        selected = record.selected
        prior = previous.selected
        return (
            selected.data_ref == prior.data_ref
            and selected.executable == prior.executable
            and 0 <= selected.activation_generation - prior.activation_generation <= 1
            and 0 <= selected.grant_generation - prior.grant_generation <= 1
        )

    @staticmethod
    def _revocation_ref(
        operation_id: str,
        activation: ValidatedActivation | None,
    ) -> str:
        activation_ref = "none" if activation is None else activation.activation_ref
        digest = hashlib.sha256(
            f"{operation_id}\0{activation_ref}".encode("utf-8")
        ).hexdigest()
        return f"ref:revocation.{digest}"


__all__ = [
    "ActivationAuthorityController",
    "ActivationBrokerFactory",
    "ActivationJobSettlement",
    "ActivationLifecycleConfigurationError",
    "ActivationLifecycleConflictError",
    "ActivationLifecycleOperationError",
    "ArtifactLaunchResolver",
    "DEFAULT_LIVENESS_POLL_S",
    "ProcessExtensionActivationLifecycle",
    "ResolvedArtifactLaunch",
    "ServingActivation",
    "WorkerLossNotice",
    "WorkerLossObserver",
    "WorkerSupervisionState",
]
