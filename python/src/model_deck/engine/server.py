from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from model_deck_contracts.ports import InstanceLock


@runtime_checkable
class EngineSocketServer(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


class EngineServer:
    def __init__(
        self,
        instance_lock: InstanceLock,
        socket_server: EngineSocketServer,
        rendezvous_payload_builder: Callable[[], dict[str, Any]],
        rendezvous_publish: Callable[[dict[str, Any]], None],
    ) -> None:
        self._lock = instance_lock
        self._server = socket_server
        self._rendezvous_payload_builder = rendezvous_payload_builder
        self._rendezvous_publish = rendezvous_publish
        self._lock_held = False
        self._listener_started = False
        self._stop_lock = threading.Lock()
        self._stopped = True

    def start(self) -> None:
        with self._stop_lock:
            if not self._stopped and self._listener_started:
                return
            if not self._lock.acquire(0.0):
                raise RuntimeError("another engine instance holds the lock")
            self._lock_held = True
            listener_started = False
            started_ok = False
            try:
                self._server.start()
                listener_started = True
                self._listener_started = True
                self._rendezvous_publish(self._rendezvous_payload_builder())
                self._stopped = False
                started_ok = True
            finally:
                if not started_ok:
                    if listener_started:
                        try:
                            self._server.stop()
                        except Exception:
                            pass
                    self._listener_started = False
                    if self._lock_held:
                        self._lock.release()
                        self._lock_held = False

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped and not self._listener_started and not self._lock_held:
                return
            try:
                if self._listener_started:
                    self._server.stop()
            finally:
                if self._listener_started:
                    self._listener_started = False
                if self._lock_held:
                    self._lock.release()
                    self._lock_held = False
                self._stopped = True

    def serve_forever(self) -> None:
        self.start()
        try:
            while True:
                threading.Event().wait(3600)
        finally:
            self.stop()
