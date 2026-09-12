from __future__ import annotations

import json
import os
import secrets
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class InstanceIdentity:
    engine_instance_id: str
    instance_nonce: str
    credential: str


class FileEnrollmentCredentialStore:
    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root
        self._engine_dir = state_root / "engine"
        self._identity_path = self._engine_dir / "instance_identity.json"
        self._credential_path = self._engine_dir / "operator_credential"
        self._lock = threading.Lock()
        self._cached: InstanceIdentity | None = None

    @property
    def credential_path(self) -> Path:
        return self._credential_path

    @property
    def engine_instance_id(self) -> str:
        return self.load_or_create().engine_instance_id

    @property
    def instance_nonce(self) -> str:
        return self.load_or_create().instance_nonce

    def _refuse_symlink(self, path: Path) -> None:
        if path.is_symlink():
            raise OSError(f"refusing symlink path: {path}")

    def _chmod_required(self, path: Path, mode: int) -> None:
        os.chmod(path, mode)
        actual = stat.S_IMODE(path.stat().st_mode)
        if actual != mode:
            raise OSError(f"refusing path {path} with mode {actual:o}, expected {mode:o}")

    def _ensure_private_engine_dir(self) -> None:
        self._refuse_symlink(self._engine_dir)
        self._state_root.mkdir(parents=True, exist_ok=True)
        self._engine_dir.mkdir(parents=True, exist_ok=True)
        self._refuse_symlink(self._engine_dir)
        self._chmod_required(self._engine_dir, 0o700)

    def _ensure_private_file(self, path: Path) -> None:
        self._refuse_symlink(path)
        self._chmod_required(path, 0o600)

    def load_or_create(self) -> InstanceIdentity:
        with self._lock:
            if self._cached is not None:
                return self._cached
            self._ensure_private_engine_dir()
            if self._identity_path.is_file() and self._credential_path.is_file():
                self._refuse_symlink(self._identity_path)
                self._refuse_symlink(self._credential_path)
                self._ensure_private_file(self._identity_path)
                self._ensure_private_file(self._credential_path)
                identity = json.loads(self._identity_path.read_text(encoding="utf-8"))
                credential = self._credential_path.read_text(encoding="utf-8").strip()
                loaded = InstanceIdentity(
                    engine_instance_id=str(identity["engine_instance_id"]),
                    instance_nonce=str(identity["instance_nonce"]),
                    credential=credential,
                )
                self._cached = loaded
                return loaded
            engine_instance_id = str(uuid.uuid4())
            instance_nonce = secrets.token_urlsafe(16)
            credential = secrets.token_urlsafe(32)
            self._identity_path.write_text(
                json.dumps(
                    {"engine_instance_id": engine_instance_id, "instance_nonce": instance_nonce},
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            self._ensure_private_file(self._identity_path)
            self._credential_path.write_text(credential + chr(10), encoding="utf-8")
            self._ensure_private_file(self._credential_path)
            created = InstanceIdentity(engine_instance_id, instance_nonce, credential)
            self._cached = created
            return created

    def verify(self, engine_instance_id: str, instance_nonce: str, credential: str) -> bool:
        identity = self.load_or_create()
        return (
            identity.engine_instance_id == engine_instance_id
            and identity.instance_nonce == instance_nonce
            and secrets.compare_digest(identity.credential, credential)
        )
