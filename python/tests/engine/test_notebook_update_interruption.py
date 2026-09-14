"""Crash/reopen recovery at the durable SWITCHED update boundary."""
from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import time
from pathlib import Path
from threading import Thread

import pytest

from model_deck.adapters.platform.macos.extension_lease import ExtensionEngineLease
from model_deck.adapters.platform.macos.instance_lock import FileInstanceLock
from model_deck.adapters.storage.sqlite_extension_lifecycle import (
    SQLiteExtensionLifecycleRepository,
)
from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.adapters.storage.sqlite_versioned_plugin_data import SQLiteVersionedPluginDataStore
from model_deck.engine.extensions.ports import LifecycleAction, LifecyclePhase, LifecycleReceipt
from model_deck.plugins.authoring import pack_project_archive
from model_deck.plugins.external_host import ExternalExtensionHost, HostDependencies


PRINCIPAL = "70000000-0000-4000-8000-000000000001"


def _host_dependencies(repository_factory):
    return HostDependencies(
        repository_factory,
        lambda path: SQLitePluginJobRepository(path, checkpoint_validator=lambda _s, _v: None),
        SQLiteVersionedPluginDataStore, FileInstanceLock, ExtensionEngineLease,
    )


def _run_interrupted_update(
    roots: dict[str, str], archive: str, expected: int, key: str,
    boundary_entered, activation_details, release_boundary,
):
    class HeldRepository(SQLiteExtensionLifecycleRepository):
        def settle(self, operation_id, *, expected_phase_revision):
            pending = self.recover()
            operation = next(
                (item for item in pending if item.request.operation_id == operation_id), None
            )
            if (
                operation is not None
                and operation.request.action is LifecycleAction.UPDATE
                and operation.phase is LifecyclePhase.SWITCHED
            ):
                boundary_entered.set()
                while not release_boundary.is_set():
                    time.sleep(0.01)
            return super().settle(operation_id, expected_phase_revision=expected_phase_revision)

    host = ExternalExtensionHost(
        Path(roots["state"]), artifact_root=Path(roots["artifacts"]),
        dependencies=_host_dependencies(HeldRepository),
    )
    try:
        def run():
            host.update(
                "org.example.notebook", archive, principal=PRINCIPAL,
                idempotency_key=key, expected_revision=expected,
            )
        worker = Thread(target=run)
        worker.start()
        if not boundary_entered.wait(20):
            activation_details.send({"error": "settle boundary not reached"})
            return
        owned = host._activation._serving["org.example.notebook"]
        activation_details.send({
            "activation_id": owned.identity.activation_id,
            "pid": owned.runtime._proc.pid,
        })
        worker.join(20)
    finally:
        host.close()


def _package_notebook_candidate(source: Path, root: Path, version: str) -> Path:
    copy = root / version
    shutil.copytree(source, copy)
    manifest = json.loads((copy / "manifest.json").read_text())
    manifest["version"] = version
    (copy / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    plugin = copy / "plugin.py"
    text = plugin.read_text().replace(
        'PLUGIN_VERSION = "1.0.0"',
        f'PLUGIN_VERSION = "{version}"',
    )
    text = text.replace('"Session Notebook"', f'"Session Notebook {version}"', 1)
    text = text.replace('"Refresh notes"', f'"Refresh notes {version}"', 1)
    plugin.write_text(text)
    panel = copy / "panels" / "notebook-list.json"
    panel.write_text(
        panel.read_text().replace("Refresh notes", f"Refresh notes {version}", 1)
    )
    archive = root / f"{version}.zip"
    pack_project_archive(copy, output_path=archive)
    return archive


def _open_host(root: Path, artifacts: Path) -> ExternalExtensionHost:
    return ExternalExtensionHost(
        root, artifact_root=artifacts,
        dependencies=_host_dependencies(SQLiteExtensionLifecycleRepository),
    )


def _wait_for_process_exit(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.01)
    return False


def test_notebook_update_recovers_after_switched_boundary(tmp_path: Path) -> None:
    source = Path(__file__).parents[3] / "examples" / "session-notebook"
    a = _package_notebook_candidate(source, tmp_path, "1.0.0")
    b = _package_notebook_candidate(source, tmp_path, "1.1.0")
    state, artifacts = tmp_path / "state", tmp_path / "artifacts"
    host = _open_host(state, artifacts)
    try:
        installed = host.install(
            a,
            principal=PRINCIPAL,
            idempotency_key="install",
            expected_revision=0,
        )
        assert isinstance(installed, LifecycleReceipt) and installed.record
        enabled = host.enable(
            "org.example.notebook", principal=PRINCIPAL, idempotency_key="enable",
            expected_revision=installed.record.revision,
        )
        assert isinstance(enabled, LifecycleReceipt) and enabled.record
        note = host.invoke_result(
            "org.example.notebook.notes.create",
            {"title": "kept", "body": "A", "metadata": {}},
            principal=PRINCIPAL, idempotency_key="note",
        )["output"]
        serving = host._activation.serving("org.example.notebook")
        assert serving is not None
        old_activation = serving.identity.activation_id
        expected = host.get_extension("org.example.notebook").revision
    finally:
        host.close()

    ctx = multiprocessing.get_context("spawn")
    entered, done, details = ctx.Event(), ctx.Event(), ctx.Pipe(False)
    child = ctx.Process(
        target=_run_interrupted_update,
        args=(
            {"state": str(state), "artifacts": str(artifacts)}, str(b), expected,
            "update-b", entered, details[1], done,
        ),
    )
    child.start()
    assert entered.wait(20)
    report = details[0].recv()
    assert report["activation_id"]
    pid = report["pid"]
    child.terminate()
    child.join(10)
    done.set()
    assert _wait_for_process_exit(pid), f"owned plugin process {pid} remained alive"

    reopened = _open_host(state, artifacts)
    try:
        record = reopened.get_extension("org.example.notebook")
        assert record.selected.executable.version == "1.1.0"
        panel = reopened.panel_get("org.example.notebook.list")
        assert panel["root"]["children"][1]["label"] == "Refresh notes 1.1.0"
        recovered_serving = reopened._activation.serving("org.example.notebook")
        assert recovered_serving is not None
        recovered_activation = recovered_serving.identity.activation_id
        assert recovered_activation not in {old_activation, report["activation_id"]}
        persisted = reopened.invoke_result(
            "org.example.notebook.notes.get",
            {"note_id": note["note_id"]},
            principal=PRINCIPAL,
            idempotency_key="read",
        )
        assert persisted["output"]["body"] == "A"
        assert not reopened._lifecycle.list_pending()
        replay = reopened.update(
            "org.example.notebook",
            b,
            principal=PRINCIPAL,
            idempotency_key="update-b",
            expected_revision=expected,
        )
        assert isinstance(replay, LifecycleReceipt)
        assert replay.record and replay.record.selected.executable.version == "1.1.0"
        assert not reopened._lifecycle.list_pending()
    finally:
        reopened.close()
