from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, Mapping, Protocol, runtime_checkable


@runtime_checkable
class ApplicationPaths(Protocol):
    def state_root(self) -> Path: ...

    def cache_root(self) -> Path: ...

    def plugins_root(self) -> Path: ...


@runtime_checkable
class InstanceLock(Protocol):
    def acquire(self, timeout_seconds: float) -> bool: ...

    def release(self) -> None: ...


@runtime_checkable
class LocalTransport(Protocol):
    def rendezvous_id(self) -> str: ...

    def send_frame(self, payload: bytes) -> None: ...

    def receive_frame(self) -> bytes: ...


@runtime_checkable
class OwnedProcessSupervisor(Protocol):
    def spawn(self, executable: Path, args: list[str], env: Mapping[str, str]) -> int: ...

    def terminate(self, pid: int, grace_seconds: float) -> None: ...

    def is_running(self, pid: int) -> bool: ...


@runtime_checkable
class CredentialStore(Protocol):
    def resolve_opaque_ref(self, credential_ref: str) -> bytes: ...

    def enroll(self, enrollment_payload_handle: str) -> str: ...


@runtime_checkable
class AtomicFileWriter(Protocol):
    def write_bytes(self, path: Path, data: bytes) -> None: ...

    def write_text(self, path: Path, text: str, encoding: str = "utf-8") -> None: ...


@runtime_checkable
class WindowAttachment(Protocol):
    def attach(self, host_window_id: str, plugin_surface_id: str) -> None: ...

    def detach(self, host_window_id: str) -> None: ...
