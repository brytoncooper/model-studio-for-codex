"""Unexpected worker loss, settled and replaced, through the real host.

Every test here runs a real packed extension in a real child process. The
child is the shipped Session Notebook project with two test-only hooks patched
in: a note titled CRASH-NOW makes it call ``os._exit`` mid-request, and one
titled HANG-NOW makes it stop answering. Nothing is faked below the host.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from model_deck.adapters.platform.macos.extension_lease import ExtensionEngineLease
from model_deck.adapters.platform.macos.instance_lock import FileInstanceLock
from model_deck.adapters.storage.sqlite_extension_lifecycle import (
    SQLiteExtensionLifecycleRepository,
)
from model_deck.adapters.storage.sqlite_plugin_jobs import SQLitePluginJobRepository
from model_deck.adapters.storage.sqlite_versioned_plugin_data import (
    SQLiteVersionedPluginDataStore,
)
from model_deck.engine.plugin_data.versioning import PluginDataBinding
from model_deck.plugins.authoring import pack_project_archive
from model_deck.plugins.external_host.host import (
    CHILD_ABSENT,
    CHILD_REAPED,
    ExternalExtensionHost,
    HostDependencies,
    SHUTDOWN_QUIESCED,
    SHUTDOWN_SETTLED,
    _BrokerFactory,
    _JOB_METHODS,
)
from model_deck.plugins.process_runtime.health import (
    RestartPolicy,
    WorkerHealthState,
    WorkerLossCode,
)

EXTENSION_ID = "org.example.notebook"
PRINCIPAL = "70000000-0000-4000-8000-000000000001"
CRASH_TITLE = "CRASH-NOW"
HANG_TITLE = "HANG-NOW"

FAST_RESTARTS = RestartPolicy(
    max_attempts=3,
    initial_backoff_s=0.05,
    multiplier=2.0,
    max_backoff_s=0.4,
    reset_after_healthy_s=60.0,
)

_HOOK_SOURCE = '''

def _supervision_test_hook(invocation_input) -> None:
    """Crash or hang on command. Patched in by the worker-supervision tests."""
    if not isinstance(invocation_input, dict):
        return
    marker = invocation_input.get("title")
    if marker == "CRASH-NOW":
        _supervision_os._exit(9)
    if marker == "HANG-NOW":
        _supervision_time.sleep(600.0)


def _supervision_export_delay() -> None:
    """Park the export worker thread so its job stays unfinished."""
    _supervision_time.sleep(600.0)

'''


def build_crashable_project(source_root: Path, destination: Path) -> Path:
    """Copy the Session Notebook project and patch the two test hooks in.

    The manifest, schemas, and panels are untouched, so the archive validates
    and installs exactly like the shipped project; only ``plugin.py`` gains the
    hooks.
    """

    shutil.copytree(source_root, destination)
    plugin_path = destination / "plugin.py"
    source = plugin_path.read_text()
    patched = source.replace(
        "import queue\n",
        "import os as _supervision_os\nimport queue\nimport time as _supervision_time\n",
        1,
    )
    if patched == source:
        raise AssertionError("session-notebook plugin.py no longer imports queue")
    with_hook = patched.replace("\ndef main() -> None:", _HOOK_SOURCE + "\ndef main() -> None:", 1)
    if with_hook == patched:
        raise AssertionError("session-notebook plugin.py no longer defines main()")
    called = with_hook.replace(
        "    ) -> dict[str, Any]:\n        if operation_id == CREATE_NOTE:",
        "    ) -> dict[str, Any]:\n        _supervision_test_hook(invocation_input)\n"
        "        if operation_id == CREATE_NOTE:",
        1,
    )
    if called == with_hook:
        raise AssertionError("session-notebook plugin.py no longer dispatches CREATE_NOTE")
    delayed = called.replace(
        "    def _check_cancelled(self, job_id: str) -> bool:\n",
        "    def _check_cancelled(self, job_id: str) -> bool:\n"
        "        _supervision_export_delay()\n",
        1,
    )
    if delayed == called:
        raise AssertionError("session-notebook plugin.py no longer defines _check_cancelled")
    plugin_path.write_text(delayed)
    return destination


class WorkerSupervisionTestCase(unittest.TestCase):
    """Shared real-host fixture: one installed, enabled, crashable extension."""

    restart_policy = FAST_RESTARTS
    timeout_s = 5.0

    def setUp(self) -> None:
        self._temporaries: list[tempfile.TemporaryDirectory[str]] = []
        self._hosts: list[ExternalExtensionHost] = []
        project_root = Path(__file__).resolve().parents[3]
        self.archive = self.directory() / "crashable.zip"
        project = build_crashable_project(
            project_root / "examples" / "session-notebook",
            self.directory() / "project",
        )
        pack_project_archive(project, output_path=self.archive)
        self.state_root = self.directory() / "host"

    def tearDown(self) -> None:
        for host in reversed(self._hosts):
            try:
                host.close()
            except Exception:
                pass
        for temporary in reversed(self._temporaries):
            temporary.cleanup()

    def directory(self) -> Path:
        temporary = tempfile.TemporaryDirectory(prefix="mdsup-", dir="/private/tmp")
        self._temporaries.append(temporary)
        return Path(temporary.name).resolve()

    def open_host(self, **overrides) -> ExternalExtensionHost:
        host = ExternalExtensionHost(
            self.state_root,
            timeout_s=overrides.pop("timeout_s", self.timeout_s),
            restart_policy=overrides.pop("restart_policy", self.restart_policy),
            dependencies=HostDependencies(
                SQLiteExtensionLifecycleRepository,
                lambda path: SQLitePluginJobRepository(
                    path, checkpoint_validator=lambda _schema, _value: None
                ),
                SQLiteVersionedPluginDataStore,
                FileInstanceLock,
                ExtensionEngineLease,
            ),
            **overrides,
        )
        self._hosts.append(host)
        return host

    def close_host(self, host: ExternalExtensionHost):
        report = host.close()
        self._hosts.remove(host)
        return report

    def install_and_enable(self, host: ExternalExtensionHost) -> None:
        installed = host.install(
            self.archive, principal=PRINCIPAL, idempotency_key="install"
        )
        host.enable(
            EXTENSION_ID,
            principal=PRINCIPAL,
            idempotency_key="enable",
            expected_revision=installed.record.revision,
        )
        self.assertIsNotNone(host._activation.serving(EXTENSION_ID))

    def start_unfinished_job(self, host: ExternalExtensionHost, key: str) -> str:
        """Start an export whose worker thread parks, so the job stays open."""

        started = host.invoke_result(
            f"{EXTENSION_ID}.export.start",
            {},
            principal=PRINCIPAL,
            idempotency_key=key,
        )
        job_id = started["job_id"]
        self.assertIn(
            host.job_get({"job_id": job_id}, principal=PRINCIPAL)["state"],
            {"queued", "running"},
        )
        return job_id

    def crash_worker(self, host: ExternalExtensionHost, key: str) -> None:
        """Ask the child to exit mid-request; the invoke never answers."""

        with self.assertRaises(Exception):
            host.invoke(
                f"{EXTENSION_ID}.notes.create",
                {"title": CRASH_TITLE, "body": "dying", "metadata": {}},
                principal=PRINCIPAL,
                idempotency_key=key,
            )

    def wait_until(self, predicate, *, timeout_s: float = 20.0, what: str = "condition"):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail(f"timed out waiting for {what}")

    def wait_for_serving(self, host: ExternalExtensionHost):
        return self.wait_until(
            lambda: host._activation.serving(EXTENSION_ID),
            what="a replacement activation to serve",
        )

    def wait_for_loss(self, host: ExternalExtensionHost):
        return self.wait_until(
            lambda: host._activation.worker_supervision(EXTENSION_ID).last_loss,
            what="the worker loss to be settled",
        )


class WorkerLossSettlementTests(WorkerSupervisionTestCase):
    def test_crashed_worker_interrupts_its_jobs_and_is_replaced_afresh(self) -> None:
        host = self.open_host()
        self.install_and_enable(host)
        job_id = self.start_unfinished_job(host, "export-before-crash")
        first_identity = host._activation.serving(EXTENSION_ID).identity

        self.crash_worker(host, "crash-once")
        loss = self.wait_for_loss(host)

        self.assertEqual(loss.extension_id, EXTENSION_ID)
        self.assertEqual(loss.activation_id, first_identity.activation_id)
        self.assertIsInstance(loss.failure_code, WorkerLossCode)
        self.assertGreater(loss.at_monotonic, 0.0)
        self.assertGreaterEqual(loss.jobs_interrupted, 1)
        self.assertTrue(loss.settled_cleanly)
        self.assertEqual(
            host.job_get({"job_id": job_id}, principal=PRINCIPAL)["state"],
            "interrupted",
        )

        serving = self.wait_for_serving(host)
        self.assertNotEqual(serving.identity.activation_id, first_identity.activation_id)
        self.assertEqual(
            host._activation.worker_supervision(EXTENSION_ID).health_state,
            WorkerHealthState.HEALTHY,
        )
        listed = host.invoke(
            f"{EXTENSION_ID}.notes.list",
            {},
            principal=PRINCIPAL,
            idempotency_key="list-after-restart",
        )
        self.assertIn("notes", listed)

    def test_interrupted_job_is_not_resumed_by_the_replacement(self) -> None:
        host = self.open_host()
        self.install_and_enable(host)
        job_id = self.start_unfinished_job(host, "export-not-resumed")

        self.crash_worker(host, "crash-no-resume")
        self.wait_for_loss(host)
        self.wait_for_serving(host)

        time.sleep(0.5)
        snapshot = host.job_get({"job_id": job_id}, principal=PRINCIPAL)
        self.assertEqual(snapshot["state"], "interrupted")
        self.assertNotIn("output", snapshot)

    def test_replaced_worker_cannot_complete_its_job_with_a_stale_identity(self) -> None:
        host = self.open_host()
        self.install_and_enable(host)
        job_id = self.start_unfinished_job(host, "export-stale-owner")
        old = host._activation.serving(EXTENSION_ID)
        old_identity = old.identity
        old_generation = host._authority.activation(old_identity).revocation_generation
        old_record = host.get_extension(EXTENSION_ID)
        # Prove the same call is refused only because the identity went stale:
        # while the activation is still serving, the broker accepts it.
        self.assertTrue(
            host._plugin_authority.issue(
                old_identity,
                PRINCIPAL,
                f"{EXTENSION_ID}.export.start",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )

        self.crash_worker(host, "crash-stale-owner")
        self.wait_for_loss(host)
        replacement = self.wait_for_serving(host)
        self.assertNotEqual(
            replacement.identity.activation_id, old_identity.activation_id
        )

        # Exactly the broker dispatch the dead activation was bound to. Its
        # process may still be alive; its authority is not.
        old_broker = _BrokerFactory(host).create(
            old_identity,
            PluginDataBinding(
                EXTENSION_ID,
                old_record.selected.data_ref,
                old_record.selected.activation_generation,
            ),
            _JOB_METHODS,
        )
        with self.assertRaises(PermissionError):
            old_broker(
                old_identity.activation_id,
                "plugin.v1.broker.jobs.complete",
                {"job_id": job_id, "output": {"content": "smuggled"}},
            )
        # It cannot obtain fresh authority either, so there is no way around it.
        with self.assertRaises(Exception):
            host._plugin_authority.issue(
                old_identity,
                PRINCIPAL,
                f"{EXTENSION_ID}.export.start",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )

        self.assertEqual(
            host.job_get({"job_id": job_id}, principal=PRINCIPAL)["state"],
            "interrupted",
        )
        revoked = host._authority.activation(old_identity)
        self.assertGreater(revoked.revocation_generation, old_generation)

    def test_operator_disable_and_worker_loss_each_settle_at_most_once(self) -> None:
        host = self.open_host()
        self.install_and_enable(host)
        job_id = self.start_unfinished_job(host, "export-disable-race")

        self.crash_worker(host, "crash-then-disable")
        self.wait_for_loss(host)
        record = host.get_extension(EXTENSION_ID)
        host.disable(
            EXTENSION_ID,
            principal=PRINCIPAL,
            idempotency_key="disable-after-crash",
            expected_revision=record.revision,
        )

        self.assertIsNone(host._activation.serving(EXTENSION_ID))
        self.assertEqual(
            host.job_get({"job_id": job_id}, principal=PRINCIPAL)["state"],
            "interrupted",
        )
        self.assertFalse(host.supervision_report(EXTENSION_ID).gave_up)

    def test_quiesce_of_a_healthy_worker_reports_no_loss(self) -> None:
        host = self.open_host()
        self.install_and_enable(host)
        record = host.get_extension(EXTENSION_ID)

        host.disable(
            EXTENSION_ID,
            principal=PRINCIPAL,
            idempotency_key="clean-disable",
            expected_revision=record.revision,
        )
        time.sleep(0.3)

        supervision = host._activation.worker_supervision(EXTENSION_ID)
        self.assertIsNone(supervision.last_loss)
        self.assertEqual(supervision.health_state, WorkerHealthState.STARTING)
        self.assertEqual(host.supervision_report(EXTENSION_ID).restart_attempts, 0)

    def test_hung_worker_is_lost_on_the_exchange_deadline_and_replaced(self) -> None:
        host = self.open_host(timeout_s=1.0)
        self.install_and_enable(host)

        with self.assertRaises(Exception):
            host.invoke(
                f"{EXTENSION_ID}.notes.create",
                {"title": HANG_TITLE, "body": "stuck", "metadata": {}},
                principal=PRINCIPAL,
                idempotency_key="hang-once",
            )

        loss = self.wait_for_loss(host)
        self.assertIn(
            loss.failure_code,
            {WorkerLossCode.TIMEOUT, WorkerLossCode.UNRESPONSIVE, WorkerLossCode.EXITED},
        )
        self.wait_for_serving(host)


class ShutdownReportTests(WorkerSupervisionTestCase):
    def test_shutdown_quiesces_a_live_worker_and_counts_its_jobs(self) -> None:
        host = self.open_host()
        self.install_and_enable(host)
        job_id = self.start_unfinished_job(host, "export-at-shutdown")

        report = self.close_host(host)

        entry = report.entry(EXTENSION_ID)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.outcome, SHUTDOWN_QUIESCED)
        self.assertIsNone(entry.failure_code)
        self.assertEqual(entry.child, CHILD_REAPED)
        self.assertGreaterEqual(entry.jobs_interrupted, 1)
        self.assertEqual(report.failed_extension_ids, ())

        reopened = self.open_host()
        self.assertEqual(
            reopened.job_get({"job_id": job_id}, principal=PRINCIPAL)["state"],
            "interrupted",
        )

    def test_shutdown_settles_jobs_when_the_worker_already_died(self) -> None:
        # No restarts at all: the crashed activation stays gone, so shutdown is
        # the thing that has to notice its jobs.
        host = self.open_host(
            restart_policy=RestartPolicy(
                max_attempts=0,
                initial_backoff_s=0.05,
                multiplier=2.0,
                max_backoff_s=0.4,
                reset_after_healthy_s=60.0,
            )
        )
        self.install_and_enable(host)
        job_id = self.start_unfinished_job(host, "export-orphaned")
        self.crash_worker(host, "crash-no-restart")
        self.wait_for_loss(host)
        self.assertTrue(
            self.wait_until(
                lambda: host.supervision_report(EXTENSION_ID).gave_up,
                what="the supervisor to give up",
            )
        )

        report = self.close_host(host)

        entry = report.entry(EXTENSION_ID)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.outcome, SHUTDOWN_SETTLED)
        self.assertEqual(entry.child, CHILD_ABSENT)

        reopened = self.open_host()
        self.assertEqual(
            reopened.job_get({"job_id": job_id}, principal=PRINCIPAL)["state"],
            "interrupted",
        )

    def test_one_failing_quiesce_does_not_skip_the_other_extensions(self) -> None:
        host = self.open_host()
        self.install_and_enable(host)
        second_id = "org.example.second"
        self.install_second_extension(host, second_id)

        broken = host._activation.quiesce

        def quiesce(operation_id, record, *, deadline_ms):
            if record.extension_id == EXTENSION_ID:
                raise RuntimeError("deliberate shutdown failure")
            return broken(operation_id, record, deadline_ms=deadline_ms)

        host._activation.quiesce = quiesce
        report = self.close_host(host)

        first = report.entry(EXTENSION_ID)
        second = report.entry(second_id)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.failure_code, "RuntimeError")
        self.assertEqual(report.failed_extension_ids, (EXTENSION_ID,))
        self.assertEqual(second.outcome, SHUTDOWN_QUIESCED)

    def install_second_extension(self, host: ExternalExtensionHost, extension_id: str) -> None:
        project = self.directory() / "second"
        build_crashable_project(
            Path(__file__).resolve().parents[3] / "examples" / "session-notebook",
            project,
        )
        manifest_path = project / "manifest.json"
        document = json.loads(manifest_path.read_text())
        document["id"] = extension_id
        document["contributes"]["operations"] = [
            {**item, "id": item["id"].replace(EXTENSION_ID, extension_id)}
            for item in document["contributes"]["operations"]
        ]
        document["contributes"]["panels"] = [
            {**item, "id": item["id"].replace(EXTENSION_ID, extension_id)}
            for item in document["contributes"].get("panels", ())
        ]
        manifest_path.write_text(json.dumps(document, indent=2) + "\n")
        for panel in (project / "panels").glob("*.json"):
            panel.write_text(panel.read_text().replace(EXTENSION_ID, extension_id))
        plugin = project / "plugin.py"
        plugin.write_text(plugin.read_text().replace(EXTENSION_ID, extension_id))
        archive = self.directory() / "second.zip"
        pack_project_archive(project, output_path=archive)
        installed = host.install(
            archive, principal=PRINCIPAL, idempotency_key=f"install-{extension_id}"
        )
        host.enable(
            extension_id,
            principal=PRINCIPAL,
            idempotency_key=f"enable-{extension_id}",
            expected_revision=installed.record.revision,
        )


class StartupSweepTests(WorkerSupervisionTestCase):
    def test_reopening_interrupts_jobs_left_running_by_a_previous_process(self) -> None:
        host = self.open_host()
        self.install_and_enable(host)
        job_id = self.start_unfinished_job(host, "export-survives")
        activation_id = host._activation.serving(EXTENSION_ID).identity.activation_id

        # Abandon the process the way a crash does: drop the host without
        # quiescing, leaving the durable job exactly as the worker left it.
        host._supervisor.close()
        host._activation.close()
        host._lock.release()
        self._hosts.remove(host)

        jobs = SQLitePluginJobRepository(
            self.state_root / "host.sqlite3",
            checkpoint_validator=lambda _schema, _value: None,
        )
        from model_deck.engine.jobs.ports import GetJobCommand

        self.assertIn(
            jobs.get(GetJobCommand(job_id)).state.value, {"queued", "running"}
        )
        self.assertEqual(jobs.get(GetJobCommand(job_id)).activation_id, activation_id)

        reopened = self.open_host()

        self.assertEqual(
            reopened.job_get({"job_id": job_id}, principal=PRINCIPAL)["state"],
            "interrupted",
        )


if __name__ == "__main__":
    unittest.main()
