import hashlib
import json
import shutil
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from model_deck.engine.extensions.ports import (
    ExecutableArtifact,
    ExtensionStatus,
    LifecycleAction,
    LifecyclePhase,
    LifecycleReceipt,
    LifecycleRequest,
)
from model_deck.engine.jobs.ports import (
    CreateJobCommand,
    JobOwner,
    JobResumeConflictError,
)
from model_deck.engine.plugin_authority import AuthorityDeniedError
from model_deck.plugins.artifact_store import stage_archive
from model_deck.adapters.platform.macos.extension_lease import ExtensionEngineLease
from model_deck.adapters.platform.macos.instance_lock import FileInstanceLock
from model_deck.adapters.storage.sqlite_extension_lifecycle import SQLiteExtensionLifecycleRepository
from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.adapters.storage.sqlite_versioned_plugin_data import SQLiteVersionedPluginDataStore
from model_deck.plugins.authoring import pack_project_archive
from model_deck.plugins.external_host import (
    ExternalExtensionHost,
    HostConflictError,
    HostNotServingError,
    HostDependencies,
)
from model_deck.plugins.external_host.host import CHILD_REAPED, SHUTDOWN_QUIESCED
from model_deck.plugins.activation_lifecycle.restart import SUPERVISION_HEALTHY
from model_deck.plugins.process_runtime.health import WorkerHealthState

def _host(root: Path, **kwargs):
    return ExternalExtensionHost(root, dependencies=HostDependencies(
        SQLiteExtensionLifecycleRepository,
        lambda path: SQLitePluginJobRepository(path, checkpoint_validator=lambda _s, _v: None),
        SQLiteVersionedPluginDataStore, FileInstanceLock, ExtensionEngineLease,
    ), **kwargs)

PRINCIPAL = "70000000-0000-4000-8000-000000000001"


def test_external_extension_host_public_boundary_exists() -> None:
    assert ExternalExtensionHost is not None

def test_inspect_is_pure_and_reports_archive_digest(tmp_path: Path) -> None:
    project = Path(__file__).parents[3] / "examples" / "session-notebook"
    archive = tmp_path / "notebook.zip"
    pack_project_archive(project.resolve(), output_path=archive)
    host = _host(tmp_path / "host")
    try:
        before = tuple(host.list_extensions())
        result = host.inspect(archive)
        assert result["manifest"]["id"] == "org.example.notebook"
        assert result["provenance"]["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
        assert tuple(host.list_extensions()) == before
        assert not tuple((tmp_path / "host" / "artifacts").iterdir())
    finally:
        host.close()

def test_update_does_not_expand_approved_scopes(tmp_path: Path) -> None:
    source = Path(__file__).parents[3] / "examples" / "session-notebook"
    project = tmp_path / "v2"
    shutil.copytree(source, project)
    manifest_path = project / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = "2.0.0"
    manifest["permissions"] = ["storage.own", "jobs.own", "data.read"]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    plugin = project / "plugin.py"
    plugin.write_text(plugin.read_text().replace('PLUGIN_VERSION = "1.0.0"', 'PLUGIN_VERSION = "2.0.0"', 1))
    archive_a = tmp_path / "a.zip"
    archive_b = tmp_path / "b.zip"
    pack_project_archive(source.resolve(), output_path=archive_a)
    pack_project_archive(project.resolve(), output_path=archive_b)
    host = _host(tmp_path / "host")
    try:
        installed = host.install(archive_a, principal=PRINCIPAL, idempotency_key="install")
        enabled = host.enable("org.example.notebook", principal=PRINCIPAL, idempotency_key="enable", expected_revision=installed.record.revision)
        updated = host.update("org.example.notebook", archive_b, principal=PRINCIPAL, idempotency_key="update", expected_revision=enabled.record.revision)
        assert updated.record.selected.approved_scopes == ("storage.own", "jobs.own")
        result = host.invoke("org.example.notebook.notes.list", {}, principal=PRINCIPAL, idempotency_key="list")
        assert "notes" in result
    finally:
        host.close()


def test_artifact_root_canonicalizes_symlinked_ancestor(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    artifact_link = tmp_path / "artifact-link"
    artifact_link.symlink_to(artifact_root, target_is_directory=True)
    host = _host(tmp_path / "host", artifact_root=artifact_link)
    try:
        assert host._artifact_root.is_dir()
        assert host._artifact_root == host._artifact_root.resolve()
    finally:
        host.close()


def test_packed_notebook_lifecycle_persistence_and_disable(tmp_path: Path) -> None:
    project = Path(__file__).parents[3] / "examples" / "session-notebook"
    archive = tmp_path / "notebook.zip"
    pack_project_archive(project.resolve(), output_path=archive)
    root = tmp_path / "host"

    host = _host(root)
    installed = host.install(archive, principal=PRINCIPAL, idempotency_key="install")
    assert isinstance(installed, LifecycleReceipt)
    assert installed.record is not None
    assert installed.record.status is ExtensionStatus.INSTALLED
    enabled = host.enable("org.example.notebook", principal=PRINCIPAL,
                          idempotency_key="enable", expected_revision=installed.record.revision)
    assert isinstance(enabled, LifecycleReceipt)
    assert enabled.record is not None
    assert enabled.record.status is ExtensionStatus.ENABLED
    operation_ids = {item["id"] for item in host.operation_catalog()}
    assert {
        "org.example.notebook.notes.create",
        "org.example.notebook.notes.list",
        "org.example.notebook.notes.get",
        "org.example.notebook.notes.update",
        "org.example.notebook.notes.delete",
    }.issubset(operation_ids)
    assert len(host.ui_contributions()) == 2
    assert host.panel_get("org.example.notebook.list")["panel_id"] == "org.example.notebook.list"
    created_result = host.invoke_result(
        "org.example.notebook.notes.create", {"title": "First", "body": "Retained", "metadata": {}},
        principal=PRINCIPAL, idempotency_key="create-1",
    )
    created = created_result["output"]
    assert created_result["panel"]["panel_id"] == "org.example.notebook.editor"
    assert host.invoke(
        "org.example.notebook.notes.create", {"title": "First", "body": "Retained", "metadata": {}},
        principal=PRINCIPAL, idempotency_key="create-1",
    ) == created
    legacy_input = {"note_id": created["note_id"]}
    legacy_digest = host._digest({
        "operation": "org.example.notebook.notes.get",
        "input": legacy_input,
        "principal": PRINCIPAL,
    })
    with host._connect() as connection:
        connection.execute(
            "INSERT INTO external_host_invocations VALUES (?,?,?,'completed',?)",
            (PRINCIPAL, "legacy-get", legacy_digest, json.dumps(created)),
        )
    assert host.invoke_result(
        "org.example.notebook.notes.get",
        legacy_input,
        principal=PRINCIPAL,
        idempotency_key="legacy-get",
    ) == {"output": created}
    assert host.invoke(
        "org.example.notebook.notes.get",
        legacy_input,
        principal=PRINCIPAL,
        idempotency_key="legacy-get",
    ) == created
    updated_result = host.invoke_result(
        "org.example.notebook.notes.update",
        {
            "note_id": created["note_id"], "title": "First", "body": "Updated",
            "metadata": {}, "expected_revision": 1,
        },
        principal=PRINCIPAL, idempotency_key="update-1",
    )
    updated = updated_result["output"]
    assert updated["revision"] == 2
    save_button = updated_result["panel"]["root"]["children"][2]
    assert save_button["params"]["expected_revision"] == 2
    updated_again = host.invoke_result(
        "org.example.notebook.notes.update",
        {
            "note_id": created["note_id"], "title": "First", "body": "Updated twice",
            "metadata": {}, "expected_revision": updated["revision"],
        },
        principal=PRINCIPAL, idempotency_key="update-2",
    )["output"]
    assert updated_again["revision"] == 3
    with pytest.raises(HostConflictError):
        host.invoke_result(
            "org.example.notebook.notes.update",
            {
                "note_id": created["note_id"], "title": "Stale", "body": "Lost",
                "metadata": {}, "expected_revision": updated["revision"],
            },
            principal=PRINCIPAL, idempotency_key="update-stale",
        )
    listed = host.invoke_result(
        "org.example.notebook.notes.list", {}, principal=PRINCIPAL,
        idempotency_key="list-after-conflict",
    )
    assert listed["output"]["notes"][0]["body"] == "Updated twice"
    list_text = str(listed["panel"]["root"])
    assert "First" in list_text
    first_activation = host._activation.serving("org.example.notebook").identity.activation_id
    host.close()

    restarted = _host(root)
    second_activation = restarted._activation.serving("org.example.notebook").identity.activation_id
    assert second_activation != first_activation
    fetched = restarted.invoke(
        "org.example.notebook.notes.get", {"note_id": created["note_id"]},
        principal=PRINCIPAL, idempotency_key="get-1",
    )
    assert fetched["body"] == "Updated twice"
    record = restarted.get_extension("org.example.notebook")
    disabled = restarted.disable("org.example.notebook", principal=PRINCIPAL,
                                 idempotency_key="disable", expected_revision=record.revision)
    assert isinstance(disabled, LifecycleReceipt)
    with pytest.raises(HostNotServingError):
        restarted.invoke("org.example.notebook.notes.get", {"note_id": created["note_id"]},
                         principal=PRINCIPAL, idempotency_key="get-disabled")
    assert disabled.record is not None
    reenabled = restarted.enable(
        "org.example.notebook",
        principal=PRINCIPAL,
        idempotency_key="reenable",
        expected_revision=disabled.record.revision,
    )
    assert isinstance(reenabled, LifecycleReceipt)
    retained = restarted.invoke(
        "org.example.notebook.notes.get",
        {"note_id": created["note_id"]},
        principal=PRINCIPAL,
        idempotency_key="get-reenabled",
    )
    assert retained["body"] == "Updated twice"
    assert reenabled.record is not None
    final_disable = restarted.disable(
        "org.example.notebook",
        principal=PRINCIPAL,
        idempotency_key="disable-final",
        expected_revision=reenabled.record.revision,
    )
    assert isinstance(final_disable, LifecycleReceipt)
    with pytest.raises(HostNotServingError):
        restarted.invoke(
            "org.example.notebook.notes.get",
            {"note_id": created["note_id"]},
            principal=PRINCIPAL,
            idempotency_key="get-final-disabled",
        )
    restarted.close()


def test_close_reports_every_extension_and_is_idempotent(tmp_path: Path) -> None:
    project = Path(__file__).parents[3] / "examples" / "session-notebook"
    archive = tmp_path / "notebook.zip"
    pack_project_archive(project.resolve(), output_path=archive)
    host = _host(tmp_path / "host")
    installed = host.install(archive, principal=PRINCIPAL, idempotency_key="install")
    host.enable(
        "org.example.notebook",
        principal=PRINCIPAL,
        idempotency_key="enable",
        expected_revision=installed.record.revision,
    )

    report = host.close()

    entry = report.entry("org.example.notebook")
    assert entry is not None
    assert entry.outcome == SHUTDOWN_QUIESCED
    assert entry.failure_code is None
    assert entry.child == CHILD_REAPED
    assert report.failed_extension_ids == ()
    # A second close changes nothing and must not touch the released lock.
    assert host.close() is report
    assert host.last_shutdown_report is report


def test_supervision_report_is_healthy_for_a_freshly_enabled_extension(
    tmp_path: Path,
) -> None:
    project = Path(__file__).parents[3] / "examples" / "session-notebook"
    archive = tmp_path / "notebook.zip"
    pack_project_archive(project.resolve(), output_path=archive)
    host = _host(tmp_path / "host")
    try:
        installed = host.install(archive, principal=PRINCIPAL, idempotency_key="install")
        host.enable(
            "org.example.notebook",
            principal=PRINCIPAL,
            idempotency_key="enable",
            expected_revision=installed.record.revision,
        )

        report = host.supervision_report("org.example.notebook")

        assert report.extension_id == "org.example.notebook"
        assert report.supervision_status == SUPERVISION_HEALTHY
        assert report.worker_state is WorkerHealthState.HEALTHY
        assert report.restart_attempts == 0
        assert report.last_failure_code is None
        assert report.gave_up is False
        assert report.next_attempt_in_s is None
        assert [item.extension_id for item in host.supervision_reports()] == [
            "org.example.notebook",
        ]
    finally:
        host.close()


def test_resume_that_fails_after_prepare_gives_back_the_authority(tmp_path: Path) -> None:
    """A resume that dies between `prepare` and `invoke` leaves nothing live.

    `prepare` issues a real invocation authority for `"<op>.resume"` before the
    durable row moves, so the window between it and `begin_resume` is the one
    place a handle can outlive the resume that asked for it. Losing the
    idempotency race is the ordinary way into that window.
    """

    project = Path(__file__).parents[3] / "examples" / "session-notebook"
    archive = tmp_path / "notebook.zip"
    pack_project_archive(project.resolve(), output_path=archive)
    host = _host(tmp_path / "host")
    try:
        installed = host.install(archive, principal=PRINCIPAL, idempotency_key="install")
        host.enable(
            "org.example.notebook",
            principal=PRINCIPAL,
            idempotency_key="enable",
            expected_revision=installed.record.revision,
        )
        serving = host._activation.serving("org.example.notebook")
        owner = JobOwner(
            plugin_id="org.example.notebook",
            activation_id=serving.identity.activation_id,
        )
        created = host._jobs.create(CreateJobCommand(
            owner=owner,
            invocation_id=str(uuid.uuid4()),
            operation_id="org.example.notebook.export.start",
            origin_principal_id=PRINCIPAL,
            resumable=True,
        ))
        host._jobs.mark_worker_crashed(owner)

        issued: list[str] = []
        real_issue = host._plugin_authority.issue

        def _remember_issued(*args, **kwargs):
            handle = real_issue(*args, **kwargs)
            issued.append(handle)
            return handle

        def _lose_the_idempotency_race(_command):
            raise JobResumeConflictError("idempotency key already used for another job")

        host._plugin_authority.issue = _remember_issued
        host._jobs.begin_resume = _lose_the_idempotency_race

        with pytest.raises(JobResumeConflictError):
            host.job_resume(
                {"job_id": created.job_id, "idempotency_key": "resume-1"},
                principal=PRINCIPAL,
            )

        # The failure happened after prepare, not before it.
        assert len(issued) == 1
        assert host._resume_invoker.pending_job_ids() == ()
        assert "org.example.notebook.export.start.resume" not in host._operations
        with pytest.raises(AuthorityDeniedError):
            host._plugin_authority.capture(issued[0], serving.identity)
    finally:
        host.close()
