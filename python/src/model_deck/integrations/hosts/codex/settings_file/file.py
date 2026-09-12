"""Descriptor-relative, cooperating-writer persistence for Codex settings."""
from __future__ import annotations

import fcntl
import os
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from model_deck_contracts.validator import validate_schema_ref
from model_deck.engine.host_settings.ports import (
    SettingsConflictError, SettingsDeniedError, SettingsExhaustedError,
    SettingsInternalError, SettingsInvalidError, SettingsNotFoundError,
    SettingsUnsupportedError, SettingsVersionMismatchError,
)
from ..settings_document import (
    DocumentContext, DocumentError, FieldSpec, ProtectedDeniedError, SectionSpec,
    SourceTooLargeError, assert_save_allowed, preview_candidate, read_snapshot,
    sha256_hex, validate_candidate,
)

_LIMIT = 262144
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass(frozen=True)
class SettingsFileSpecs:
    context: DocumentContext
    sections: tuple[SectionSpec, ...]
    fields: tuple[FieldSpec, ...]


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class _Directory:
    """Keep the entire no-follow directory chain open and detect rebinding."""
    def __init__(self, path: Path):
        self.fds: list[int] = []
        self.links: list[tuple[int, str, int]] = []
        try:
            self.fds.append(os.open("/", _DIRECTORY))
            for part in path.parts[1:]:
                parent = self.fds[-1]
                child = os.open(part, _DIRECTORY, dir_fd=parent)
                self.fds.append(child)
                self.links.append((parent, part, child))
        except BaseException:
            self.close()
            raise

    @property
    def fd(self) -> int:
        return self.fds[-1]

    def check(self) -> None:
        for parent, name, child in self.links:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or _identity(current) != _identity(os.fstat(child)):
                raise SettingsConflictError("settings directory changed", reason="target_changed")

    def close(self) -> None:
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()


@contextmanager
def _safe_errors() -> Iterator[None]:
    try:
        yield
    except (SettingsConflictError, SettingsDeniedError, SettingsExhaustedError,
            SettingsInvalidError, SettingsNotFoundError, SettingsUnsupportedError, SettingsVersionMismatchError):
        raise
    except ProtectedDeniedError:
        raise SettingsDeniedError("protected settings changed") from None
    except SourceTooLargeError:
        raise SettingsExhaustedError("settings document exceeds bound") from None
    except DocumentError as error:
        if error.code == "conflict":
            raise SettingsConflictError("settings candidate changed", reason="candidate_changed") from None
        if error.code == "unsupported":
            raise SettingsUnsupportedError("settings edit unsupported") from None
        raise SettingsInvalidError("settings document rejected") from None
    except Exception:
        raise SettingsInternalError("settings file operation failed") from None


class CodexSettingsFile:
    """Implements SettingsDocumentPort using only explicit paths and specs."""
    def __init__(self, *, config_file: Path, backup_dir: Path,
                 context_specs: Callable[[bytes, bool], SettingsFileSpecs]):
        self.config_file = Path(config_file)
        self.backup_dir = Path(backup_dir)
        for path in (self.config_file, self.backup_dir):
            if not path.is_absolute() or ".." in path.parts:
                raise SettingsInvalidError("absolute settings paths required")
        if self.config_file.name in ("", ".", "..") or not callable(context_specs):
            raise SettingsInvalidError("settings file configuration invalid")
        self._context_specs = context_specs

    @contextmanager
    def _locked(self) -> Iterator[_Directory]:
        directory = _Directory(self.config_file.parent)
        lock_fd = None
        try:
            name = "." + self.config_file.name + ".model-deck.lock"
            lock_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                lock_fd = os.open(name, lock_flags | os.O_CREAT | os.O_EXCL, 0o600,
                                  dir_fd=directory.fd)
            except FileExistsError:
                lock_fd = os.open(name, lock_flags, dir_fd=directory.fd)
            info = os.fstat(lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SettingsInvalidError("settings lock rejected")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if _identity(os.stat(name, dir_fd=directory.fd, follow_symlinks=False)) != _identity(info):
                raise SettingsConflictError("settings lock changed", reason="target_changed")
            directory.check()
            yield directory
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            directory.close()

    def _source(self, directory: _Directory) -> tuple[bytes, os.stat_result | None]:
        try:
            fd = os.open(self.config_file.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory.fd)
        except FileNotFoundError:
            return b"", None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise SettingsInvalidError("settings target is not a regular file")
            chunks = bytearray()
            while len(chunks) <= _LIMIT:
                chunk = os.read(fd, min(65536, _LIMIT + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            if len(chunks) > _LIMIT:
                raise SettingsExhaustedError("settings document exceeds bound")
            after = os.fstat(fd)
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise SettingsConflictError("settings source changed", reason="content_changed")
            return bytes(chunks), after
        finally:
            os.close(fd)

    def _specs(self, source: bytes, info: os.stat_result | None, host_id: str,
               document_id: str | None = None, expected: str | None = None,
               revision: str | None = None) -> SettingsFileSpecs:
        try:
            specs = self._context_specs(source, info is not None)
        except SettingsConflictError as error:
            allowed_reasons = {"conflict", "content_changed", "context_changed", "candidate_changed", "target_changed"}
            reason = error.reason if type(error.reason) is str and error.reason in allowed_reasons else "conflict"
            raise SettingsConflictError("settings context unavailable", reason=reason) from None
        except (SettingsDeniedError, SettingsExhaustedError, SettingsInvalidError,
                SettingsNotFoundError, SettingsUnsupportedError, SettingsVersionMismatchError) as error:
            # Reconstruct only known public classes, never a callback subclass or message.
            for error_type in (SettingsDeniedError, SettingsExhaustedError, SettingsInvalidError,
                               SettingsNotFoundError, SettingsUnsupportedError, SettingsVersionMismatchError):
                if isinstance(error, error_type):
                    raise error_type("settings context unavailable") from None
        context = specs.context
        if context.host_id != host_id or (document_id is not None and context.document_id != document_id):
            raise SettingsNotFoundError("settings identity unavailable")
        actual = sha256_hex(source) if info is not None else "absent"
        if context.exists != (info is not None) or context.document_revision != actual:
            raise SettingsInvalidError("settings context inconsistent")
        if expected is not None and expected != actual:
            raise SettingsConflictError("settings source changed", reason="content_changed")
        if revision is not None and revision != context.context_revision:
            raise SettingsConflictError("settings context changed", reason="context_changed")
        return specs

    def read(self, host_id: str) -> dict[str, Any]:
        with _safe_errors(), self._locked() as directory:
            source, info = self._source(directory)
            specs = self._specs(source, info, host_id)
            result = read_snapshot(source, specs.context, specs.sections, specs.fields)
            directory.check()
            return result

    def _candidate(self, function: Callable[..., dict[str, Any]], host_id: str,
                   document_id: str, expected: str, revision: str, draft: dict[str, Any]) -> dict[str, Any]:
        with _safe_errors(), self._locked() as directory:
            source, info = self._source(directory)
            specs = self._specs(source, info, host_id, document_id, expected, revision)
            result = function(source, specs.context, specs.sections, specs.fields, draft)
            directory.check()
            return result

    def validate(self, host_id: str, document_id: str, expected_content_hash: str,
                 context_revision: str, draft: dict[str, Any]) -> dict[str, Any]:
        return self._candidate(validate_candidate, host_id, document_id, expected_content_hash, context_revision, draft)

    def preview(self, host_id: str, document_id: str, expected_content_hash: str,
                context_revision: str, draft: dict[str, Any]) -> dict[str, Any]:
        return self._candidate(preview_candidate, host_id, document_id, expected_content_hash, context_revision, draft)

    @staticmethod
    def _write(fd: int, data: bytes) -> None:
        pending = memoryview(data)
        while pending:
            count = os.write(fd, pending)
            if count <= 0:
                raise OSError("short settings write")
            pending = pending[count:]
        os.fsync(fd)

    def save(self, host_id: str, document_id: str, expected_content_hash: str,
             context_revision: str, candidate_content_hash: str,
             candidate_raw_toml: str) -> dict[str, Any]:
        with _safe_errors(), self._locked() as directory:
            source, info = self._source(directory)
            specs = self._specs(source, info, host_id, document_id, expected_content_hash, context_revision)
            if specs.context.target.get("writable") is not True:
                raise SettingsDeniedError("settings target is not writable")
            actual = assert_save_allowed(source, candidate_raw_toml, candidate_content_hash)
            draft = {"kind": "raw", "raw_toml": candidate_raw_toml}
            preview = preview_candidate(source, specs.context, specs.sections, specs.fields, draft)
            if not preview["valid"]:
                raise SettingsInvalidError("settings candidate rejected")
            changed = info is None or source != candidate_raw_toml.encode("utf-8")
            result = {"saved": True, "changed": changed, "document_id": document_id,
                      "previous_content_hash": expected_content_hash, "document_revision": actual,
                      "backup": None, "application_effects": preview["preview"]["application_effects"],
                      "context_revision": context_revision}
            if changed:
                self._publish(directory, source, info, specs, draft, actual, result)
            else:
                directory.check()
            validate_schema_ref("contracts/engine.v1/methods/hosts.settings.save.result.schema.json", result)
            return result

    def _publish(self, directory: _Directory, source: bytes, info: os.stat_result | None,
                 specs: SettingsFileSpecs, draft: dict[str, Any], actual: str, result: dict[str, Any]) -> None:
        backups = _Directory(self.backup_dir)
        temporary = ".model-deck-settings-" + uuid.uuid4().hex
        temporary_fd = None
        try:
            directory.check()
            backups.check()
            if info is not None:
                backup_name = "settings-" + uuid.uuid4().hex + ".toml"
                backup_fd = os.open(backup_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                    0o600, dir_fd=backups.fd)
                try:
                    os.fchmod(backup_fd, 0o600)
                    self._write(backup_fd, source)
                except BaseException:
                    current = os.stat(backup_name, dir_fd=backups.fd, follow_symlinks=False)
                    if _identity(current) == _identity(os.fstat(backup_fd)):
                        os.unlink(backup_name, dir_fd=backups.fd)
                    raise
                finally:
                    os.close(backup_fd)
                os.fsync(backups.fd)
                result["backup"] = {"backup_id": backup_name, "display_path": str(self.backup_dir / backup_name)}
            temporary_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                   0o600, dir_fd=directory.fd)
            os.fchmod(temporary_fd, stat.S_IMODE(info.st_mode) if info is not None else 0o600)
            self._write(temporary_fd, draft["raw_toml"].encode("utf-8"))
            fresh, fresh_info = self._source(directory)
            if fresh != source or (fresh_info is None) != (info is None) or (
                    info is not None and fresh_info is not None and _identity(info) != _identity(fresh_info)):
                raise SettingsConflictError("settings source changed", reason="content_changed")
            current = self._specs(fresh, fresh_info, specs.context.host_id, specs.context.document_id,
                                  specs.context.document_revision, specs.context.context_revision)
            if current.context.target.get("writable") is not True:
                raise SettingsDeniedError("settings target is not writable")
            check = validate_candidate(fresh, current.context, current.sections, current.fields, draft)
            if not check["valid"] or check["candidate_content_hash"] != actual:
                raise SettingsInvalidError("settings candidate rejected")
            directory.check()
            backups.check()
            if _identity(os.stat(temporary, dir_fd=directory.fd, follow_symlinks=False)) != _identity(os.fstat(temporary_fd)):
                raise SettingsConflictError("settings temporary file changed", reason="target_changed")
            # Validate metadata before publication; no successful write with an invalid wire receipt.
            validate_schema_ref("contracts/engine.v1/methods/hosts.settings.save.result.schema.json", result)
            if info is None:
                os.link(temporary, self.config_file.name, src_dir_fd=directory.fd,
                        dst_dir_fd=directory.fd, follow_symlinks=False)
            else:
                os.replace(temporary, self.config_file.name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
            os.fsync(directory.fd)
            directory.check()
            backups.check()
        except FileExistsError:
            raise SettingsConflictError("settings target appeared", reason="content_changed") from None
        finally:
            if temporary_fd is not None:
                try:
                    current = os.stat(temporary, dir_fd=directory.fd, follow_symlinks=False)
                    if _identity(current) == _identity(os.fstat(temporary_fd)):
                        os.unlink(temporary, dir_fd=directory.fd)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(temporary_fd)
            backups.close()
