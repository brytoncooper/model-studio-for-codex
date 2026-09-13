"""macOS instance-lock adapter for serialized extension lifecycle execution."""
from __future__ import annotations

import os
import threading
from contextlib import AbstractContextManager, contextmanager
from typing import Iterator
from weakref import WeakKeyDictionary

from model_deck.adapters.platform.macos.instance_lock import FileInstanceLock
from model_deck.engine.extensions.ports import ExtensionLifecycleRepository
from model_deck.engine.extensions.service import LifecycleExecutionConflictError


class ExtensionEngineLeaseContractError(RuntimeError):
    pass


class ExtensionEngineLeaseBindingError(RuntimeError):
    pass


class ExtensionEngineLeaseProcessError(RuntimeError):
    pass


class _ExecutionOwnerRegistry:
    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.operation_ids: set[str] = set()


_REGISTRIES_GUARD = threading.Lock()
_REGISTRIES: WeakKeyDictionary[FileInstanceLock, _ExecutionOwnerRegistry] = (
    WeakKeyDictionary()
)


def _registry_for(instance_lock: FileInstanceLock) -> _ExecutionOwnerRegistry:
    with _REGISTRIES_GUARD:
        registry = _REGISTRIES.get(instance_lock)
        if registry is None:
            registry = _ExecutionOwnerRegistry()
            _REGISTRIES[instance_lock] = registry
        return registry


class ExtensionEngineLease:
    """Binds one held process instance lock to one trusted lifecycle repository."""

    def __init__(
        self,
        instance_lock: FileInstanceLock,
        repository: ExtensionLifecycleRepository,
    ) -> None:
        if not isinstance(instance_lock, FileInstanceLock):
            raise ExtensionEngineLeaseContractError("invalid instance lock")
        if not isinstance(repository, ExtensionLifecycleRepository):
            raise ExtensionEngineLeaseContractError(
                "invalid extension lifecycle repository"
            )
        self._instance_lock = instance_lock
        self._repository = repository
        self._process_id = os.getpid()
        self._execution_registry = _registry_for(instance_lock)

    def assert_held_for(self, repository: ExtensionLifecycleRepository) -> None:
        self._assert_original_process()
        if repository is not self._repository:
            raise ExtensionEngineLeaseBindingError(
                "repository is not bound to this extension engine lease"
            )
        self._instance_lock.assert_held()

    def execution_owner(
        self,
        repository: ExtensionLifecycleRepository,
        operation_id: str,
    ) -> AbstractContextManager[None]:
        if type(operation_id) is not str or not operation_id:
            raise ExtensionEngineLeaseContractError("invalid lifecycle operation id")
        return self._execution_owner(repository, operation_id)

    @contextmanager
    def _execution_owner(
        self,
        repository: ExtensionLifecycleRepository,
        operation_id: str,
    ) -> Iterator[None]:
        self.assert_held_for(repository)
        with self._execution_registry.guard:
            self.assert_held_for(repository)
            if operation_id in self._execution_registry.operation_ids:
                raise LifecycleExecutionConflictError()
            self._execution_registry.operation_ids.add(operation_id)
        try:
            yield
        finally:
            with self._execution_registry.guard:
                self._execution_registry.operation_ids.remove(operation_id)

    def _assert_original_process(self) -> None:
        if os.getpid() != self._process_id:
            raise ExtensionEngineLeaseProcessError(
                "extension engine lease cannot be used by a forked process"
            )
