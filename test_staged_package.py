"""Filesystem/injected packaging tests plus benign temporary process supervision."""
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

_ROOT = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("model_deck_stage_test_subject", _ROOT / "scripts/package/stage.py")
stage = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = stage
_SPEC.loader.exec_module(stage)


class FixtureCommands:
    def __init__(self, inventory):
        self.inventory = inventory
        self.calls = []
        self.failure = None
        self.bad_layout = False
        self.changed_helper = False

    def __call__(self, argv, cwd, env):
        self.calls.append((list(argv), cwd, dict(env)))
        if self.failure and self.failure(argv):
            raise stage.StageError("injected command failure")
        if argv[0] == "/usr/bin/swift":
            build_root = Path(argv[argv.index("--scratch-path") + 1])
            release = build_root / "arm64-apple-macosx/release"
            if "--show-bin-path" in argv:
                return str(release)
            release.mkdir(parents=True)
            (release / "ModelDeck").write_bytes(b"fixture executable")
            for target, bundle in self.inventory["swift_bundles"].items():
                accessor = release / f"{target}.build/DerivedSources/resource_bundle_accessor.swift"
                accessor.parent.mkdir(parents=True)
                main = "resourceURL!" if self.bad_layout else "bundleURL"
                accessor.write_text(
                    f'let mainPath = Bundle.main.{main}.appendingPathComponent("{bundle}").path\n'
                    'let preferredBundle = Bundle(path: mainPath)\n'
                )
                (release / bundle).mkdir()
                (release / bundle / "fixture.json").write_text("{}")
        if self.changed_helper and argv[0] == "/usr/bin/codesign" and "--sign" in argv:
            (Path(argv[-1]) / "Contents/Helpers/OpenRouterCredentialHelper").write_bytes(b"changed")
        return ""


class StagedPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="model-deck-package-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.inventory = json.loads((_ROOT / "scripts/package/inventory.json").read_text())
        self.source = self.root / "source"
        self.source.mkdir()
        for relative in ["build.sh", "local_router.py", "macos/Package.swift", "OpenRouterCredentialHelper.swift",
                         *self.inventory["resources"], *self.inventory["icons"]]:
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture source {relative}\n")
        project = self.source / "python/pyproject.toml"
        project.parent.mkdir()
        project.write_text('[project]\nname = "model-deck"\nversion = "0.0.0"\ndependencies = ["jsonschema[format]==4.23.0", "tomlkit==0.13.3"]\n')
        self.vendor = self.root / "vendor"
        self.vendor.mkdir()
        for relative in self.inventory["vendor"]["required_files"]:
            path = self.vendor / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture package\n")
            if relative.split("/")[0] in ("model_deck", "model_deck_contracts", "model_deck_root_guard"):
                source_package = self.source / "python/src" / relative
                source_package.parent.mkdir(parents=True, exist_ok=True)
                source_package.write_bytes(path.read_bytes())
        for name, version in {"model-deck": "0.0.0", "tomlkit": "0.13.3", "jsonschema": "4.23.0"}.items():
            metadata = self.vendor / f"{name.replace('-', '_')}-{version}.dist-info/METADATA"
            metadata.parent.mkdir()
            requirements = "Requires-Dist: jsonschema[format]==4.23.0\nRequires-Dist: tomlkit==0.13.3\n" if name == "model-deck" else ""
            metadata.write_text(f"Name: {name}\nVersion: {version}\n{requirements}")
        self.write_vendor_manifest()
        helper = self.root / "helper-snapshot"
        helper.write_bytes(b"fixture signed helper")
        source_hash = self.root / "helper.source-sha256"
        source_hash.write_text(stage.sha256((self.source / "OpenRouterCredentialHelper.swift").read_bytes()) + "\n")
        python = self.root / "python"
        python.write_text("fixture interpreter; never executed")
        for name in ("scratch", "state", "artifacts"):
            (self.root / name).mkdir()
            (self.root / name / "keep.txt").write_text("caller-owned")
        self.inputs = stage.StageInputs(
            source_root=self.source, scratch_root=self.root / "scratch", state_root=self.root / "state",
            artifact_root=self.root / "artifacts", name="release-1", helper_snapshot=helper,
            helper_sha256=stage.sha256(helper.read_bytes()), helper_source_hash_file=source_hash,
            vendor_root=self.vendor, python_executable=python,
        )
        self.commands = FixtureCommands(self.inventory)

    def write_vendor_manifest(self):
        manifest = {
            "format_version": 1, "provenance": "isolated test fixtures; no real wheel claim",
            "requirements": ["jsonschema[format]==4.23.0", "tomlkit==0.13.3"],
            "distributions": {"model-deck": "0.0.0", "tomlkit": "0.13.3", "jsonschema": "4.23.0"},
            "files": {path.relative_to(self.vendor).as_posix(): stage.sha256(path.read_bytes())
                      for path in self.vendor.rglob("*") if path.is_file() and path.name != "vendor-manifest.json"},
        }
        (self.vendor / "vendor-manifest.json").write_text(json.dumps(manifest))

    @staticmethod
    def fixture_publish(parent_fd, source, destination):
        # Deterministic single-thread fixture only. Production uses RENAME_EXCL.
        try:
            os.stat(destination, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.rename(source, destination, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        else:
            raise FileExistsError(destination)

    def package(self, inputs=None, publisher=None):
        return stage.stage_app(inputs or self.inputs, command=self.commands,
                               publisher=publisher or self.fixture_publish)

    def assert_owned_children_cleaned(self):
        for parent in (self.inputs.scratch_root, self.inputs.state_root):
            self.assertEqual([path.name for path in parent.iterdir()], ["keep.txt"])
        self.assertFalse(any(path.name.startswith(".stage-output-") for path in self.inputs.artifact_root.iterdir()))
        self.assertEqual((self.inputs.artifact_root / "keep.txt").read_text(), "caller-owned")

    def test_stages_expected_bytes_resources_alias_and_inventory_without_launch(self):
        destination = self.package()
        app = destination / "Model Deck.app"
        self.assertEqual((app / "Contents/Helpers/OpenRouterCredentialHelper").read_bytes(), self.inputs.helper_snapshot.read_bytes())
        self.assertEqual(os.readlink(app / "Contents/MacOS/OpenRouterSettings"), "ModelDeck")
        for bundle in self.inventory["swift_bundles"].values():
            self.assertTrue((app / bundle / "fixture.json").is_file())
        plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        self.assertEqual(plist["PythonExecutable"], str(self.inputs.python_executable))
        self.assertEqual(plist["CFBundleIdentifier"], "com.cooper.model-deck")
        report = json.loads((destination / "inventory.json").read_text())
        self.assertFalse(report["app_signed"])
        self.assertEqual(report["vendor_manifest"]["distributions"]["model-deck"], "0.0.0")
        for record in report["files"]:
            if "sha256" in record:
                self.assertEqual(stage.sha256((destination / record["path"]).read_bytes()), record["sha256"])
        self.assert_owned_children_cleaned()

    def test_commands_use_explicit_isolated_release_paths_and_no_install_network_or_launch(self):
        self.package()
        for argv, cwd, environment in self.commands.calls:
            self.assertIn(argv[0], (str(self.inputs.python_executable), "/usr/bin/swift", "/usr/bin/codesign"))
            self.assertTrue(cwd.is_relative_to(self.inputs.scratch_root))
            self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertNotIn("MODEL_DECK_SIGNING_IDENTITY", environment)
            if argv[0] == "/usr/bin/swift":
                self.assertIn("--disable-automatic-resolution", argv)
                self.assertEqual(argv[argv.index("-c") + 1], "release")
                self.assertTrue(Path(argv[argv.index("--scratch-path") + 1]).is_relative_to(self.inputs.scratch_root))
                self.assertEqual(argv[argv.index("--package-path") + 1], str(self.source / "macos"))
        self.assertEqual(len(self.commands.calls), 4)

    def test_requested_signing_failure_never_falls_back(self):
        self.commands.failure = lambda argv: "--sign" in argv
        with self.assertRaisesRegex(stage.StageError, "injected"):
            self.package(replace(self.inputs, signing_identity="explicit certificate"))
        signing = [argv for argv, _, _ in self.commands.calls if "--sign" in argv]
        self.assertEqual(len(signing), 1)
        self.assertEqual(signing[0][signing[0].index("--sign") + 1], "explicit certificate")
        self.assert_owned_children_cleaned()

    def test_explicit_ad_hoc_signing_checks_staged_app_and_preserves_helper(self):
        destination = self.package(replace(self.inputs, signing_identity="-"))
        self.assertTrue(json.loads((destination / "inventory.json").read_text())["app_signed"])
        signing = [argv for argv, _, _ in self.commands.calls if "--sign" in argv]
        self.assertEqual(len(signing), 1)
        self.assertNotIn("--deep", signing[0])
        self.assertIn("--timestamp=none", signing[0])

    def test_changed_helper_after_signing_refuses_publication(self):
        self.commands.changed_helper = True
        with self.assertRaisesRegex(stage.StageError, "signing changed helper"):
            self.package(replace(self.inputs, signing_identity="-"))
        self.assertFalse((self.inputs.artifact_root / self.inputs.name).exists())
        self.assert_owned_children_cleaned()

    def test_bad_helper_binary_or_source_hash_refuses_before_commands(self):
        for inputs in (replace(self.inputs, helper_sha256="0" * 64),):
            with self.assertRaisesRegex(stage.StageError, "SHA256 mismatch"):
                self.package(inputs)
        self.inputs.helper_source_hash_file.write_text("0" * 64)
        with self.assertRaisesRegex(stage.StageError, "source hash mismatch"):
            self.package()
        self.assertEqual(self.commands.calls, [])

    def test_existing_destination_file_directory_or_symlink_is_preserved(self):
        destination = self.inputs.artifact_root / self.inputs.name
        for kind in ("file", "directory", "symlink"):
            if kind == "file":
                destination.write_text("keep")
            elif kind == "directory":
                destination.mkdir()
            else:
                destination.symlink_to(self.inputs.artifact_root / "missing")
            with self.assertRaisesRegex(stage.StageError, "already exists"):
                self.package()
            self.assertTrue(os.path.lexists(destination))
            destination.rmdir() if kind == "directory" else destination.unlink()
        self.assertEqual(self.commands.calls, [])

    def test_publication_race_keeps_competing_destination_and_cleans_only_ours(self):
        def race(parent_fd, source, destination):
            target = self.inputs.artifact_root / destination
            target.mkdir()
            (target / "keep").write_text("competing output")
            self.fixture_publish(parent_fd, source, destination)
        with self.assertRaises(FileExistsError):
            self.package(publisher=race)
        self.assertEqual((self.inputs.artifact_root / self.inputs.name / "keep").read_text(), "competing output")
        self.assert_owned_children_cleaned()

    def test_partial_build_failure_preserves_caller_roots(self):
        self.commands.failure = lambda argv: argv[0] == "/usr/bin/swift"
        with self.assertRaises(stage.StageError):
            self.package()
        self.assert_owned_children_cleaned()

    def test_unknown_accessor_refuses_instead_of_using_build_fallback(self):
        self.commands.bad_layout = True
        with self.assertRaisesRegex(stage.StageError, "unsupported Swift resource lookup"):
            self.package()
        self.assert_owned_children_cleaned()

    def test_swift_output_outside_scratch_refused(self):
        original = self.commands
        def escaped(argv, cwd, env):
            if "--show-bin-path" in argv:
                return "/Applications/release"
            return original(argv, cwd, env)
        self.commands = escaped
        with self.assertRaisesRegex(stage.StageError, "escaped owned scratch"):
            self.package()
        self.assert_owned_children_cleaned()

    def test_guard_refuses_source_protected_nested_and_symlink_roots(self):
        alias = self.root / "alias"
        alias.symlink_to(self.inputs.artifact_root, target_is_directory=True)
        candidates = [self.source, self.source / "nested", self.source.parent / "Model Deck.app", alias,
                      self.inputs.state_root / "nested"]
        for artifact in candidates:
            with self.subTest(artifact=artifact), self.assertRaises(ValueError):
                self.package(replace(self.inputs, artifact_root=artifact))
        self.assertEqual(self.commands.calls, [])

    def test_missing_engine_file_refuses_even_with_self_consistent_manifest(self):
        (self.vendor / "model_deck/integrations/clients/mcp/registry_snapshot.py").unlink()
        self.write_vendor_manifest()
        with self.assertRaisesRegex(stage.StageError, "required vendor file missing"):
            self.package()
        self.assertEqual(self.commands.calls, [])

    def test_vendor_tamper_extra_file_or_wrong_metadata_refuses_offline(self):
        target = self.vendor / "tomlkit/__init__.py"
        target.write_text("changed")
        with self.assertRaisesRegex(stage.StageError, "SHA256 mismatch"):
            self.package()
        self.write_vendor_manifest()
        metadata = self.vendor / "tomlkit-0.13.3.dist-info/METADATA"
        metadata.write_text("Name: tomlkit\nVersion: 0.1\n")
        self.write_vendor_manifest()
        with self.assertRaisesRegex(stage.StageError, "pinned dependencies"):
            self.package()
        self.assertEqual(self.commands.calls, [])

    def test_vendor_symlink_and_bytecode_refused(self):
        alias = self.vendor / "alias"
        alias.symlink_to(self.inputs.helper_snapshot)
        with self.assertRaisesRegex(stage.StageError, "aliases and bytecode"):
            self.package()
        alias.unlink()
        (self.vendor / "cache.pyc").write_bytes(b"cache")
        with self.assertRaisesRegex(stage.StageError, "aliases and bytecode"):
            self.package()
        self.assertEqual(self.commands.calls, [])

    def test_helper_symlink_and_hardlink_refused(self):
        alias = self.root / "helper-alias"
        alias.symlink_to(self.inputs.helper_snapshot)
        with self.assertRaises(ValueError):
            self.package(replace(self.inputs, helper_snapshot=alias))
        alias.unlink()
        os.link(self.inputs.helper_snapshot, alias)
        with self.assertRaisesRegex(stage.StageError, "single-link"):
            self.package(replace(self.inputs, helper_snapshot=alias))
        self.assertEqual(self.commands.calls, [])

    def test_cleanup_does_not_remove_replacement_child(self):
        parent = self.root / "cleanup"
        parent.mkdir()
        with stage.owned_temporary_directory(parent, "owned-") as child:
            original = parent / "moved-original"
            child.rename(original)
            child.mkdir()
            (child / "caller.txt").write_text("replacement")
        self.assertEqual((child / "caller.txt").read_text(), "replacement")
        self.assertTrue(original.is_dir())

    def test_command_timeout_signals_only_its_new_owned_process_group(self):
        process = Mock(pid=123456, returncode=-9)
        with self.supervision_primitives(ready=False), patch.object(stage.subprocess, "Popen", return_value=process) as create, patch.object(stage.os, "killpg") as kill:
            with self.assertRaises(stage.subprocess.TimeoutExpired):
                stage.run_command(["/fixture/compiler", "argument with spaces"], self.inputs.scratch_root, {})
        self.assertTrue(create.call_args.kwargs["start_new_session"])
        self.assertEqual(create.call_args.args[0][-2:], ["/fixture/compiler", "argument with spaces"])
        self.assertIn("subprocess.run", create.call_args.args[0][4])
        kill.assert_called_once_with(process.pid, stage.signal.SIGKILL)

    def test_normal_leader_exit_cleans_its_group_before_reaping(self):
        events = []
        process = Mock(pid=123456, returncode=-9)
        process.wait.side_effect = lambda **_: events.append("reap")
        with self.supervision_primitives(), patch.object(stage.subprocess, "Popen", return_value=process), \
             patch.object(stage.os, "killpg", side_effect=lambda *_: events.append("group cleanup")):
            stage.run_command(["/fixture/compiler"], self.inputs.scratch_root, {})
        self.assertEqual(events, ["group cleanup", "reap"])
        process.poll.assert_not_called()

    def test_nonzero_leader_exit_cleans_group_then_reports_failure(self):
        events = []
        process = Mock(pid=123456, returncode=-9)
        process.wait.side_effect = lambda **_: events.append("reap")
        with self.supervision_primitives(status=b"7\n"), patch.object(stage.subprocess, "Popen", return_value=process), \
             patch.object(stage.os, "killpg", side_effect=lambda *_: events.append("group cleanup")):
            with self.assertRaisesRegex(stage.StageError, "command failed"):
                stage.run_command(["/fixture/compiler"], self.inputs.scratch_root, {})
        self.assertEqual(events, ["group cleanup", "reap"])

    def test_non_macos_refuses_before_launch(self):
        with patch.object(stage.sys, "platform", "unsupported"), patch.object(stage.subprocess, "Popen") as create:
            with self.assertRaisesRegex(stage.StageError, "requires macOS"):
                stage.run_command(["/fixture/compiler"], self.inputs.scratch_root, {})
        create.assert_not_called()

    def test_guardian_eof_cleans_group_and_never_reports_success(self):
        process = Mock(pid=123456, returncode=0)
        with self.supervision_primitives(status=b""), patch.object(stage.subprocess, "Popen", return_value=process), \
             patch.object(stage.os, "killpg") as kill:
            with self.assertRaisesRegex(stage.StageError, "valid exit status"):
                stage.run_command(["/fixture/compiler"], self.inputs.scratch_root, {})
        kill.assert_called_once_with(process.pid, stage.signal.SIGKILL)
        process.wait.assert_called_once_with(timeout=10)

    @staticmethod
    def supervision_primitives(*, status=b"0\n", ready=True):
        stack = stage.ExitStack()
        stack.enter_context(patch.object(stage.select, "select", return_value=([1] if ready else [], [], [])))
        stack.enter_context(patch.object(stage.os, "read", return_value=status))
        return stack

    @unittest.skipUnless(sys.platform == "darwin", "macOS supervision fixture")
    def test_real_fast_exit_and_exec_failure_do_not_require_waitid(self):
        environment = {"PATH": "/usr/bin:/bin"}
        self.assertEqual(stage.run_command([sys.executable, "-I", "-B", "-c", "print('fixture')"],
                                          self.inputs.scratch_root, environment), "fixture")
        with self.assertRaisesRegex(stage.StageError, "command failed"):
            stage.run_command([str(self.root / "nonexistent-executable")], self.inputs.scratch_root, environment)

    @unittest.skipUnless(sys.platform == "darwin", "macOS supervision fixture")
    def test_real_parent_exit_stops_owned_child_writes(self):
        heartbeat = self.root / "child-heartbeat"
        child_code = (
            "import pathlib,sys,time; target=pathlib.Path(sys.argv[1]); "
            "target.write_text('ready'); time.sleep(0.4); target.write_text('survived'); time.sleep(5)"
        )
        parent_code = (
            "import subprocess,sys,pathlib,time; "
            "subprocess.Popen([sys.executable,'-I','-B','-c',sys.argv[1],sys.argv[2]]); "
            "target=pathlib.Path(sys.argv[2]); deadline=time.monotonic()+3; "
            "\nwhile not target.exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
            "raise SystemExit(0 if target.exists() else 1)"
        )
        stage.run_command([sys.executable, "-I", "-B", "-c", parent_code, child_code, str(heartbeat)],
                          self.inputs.scratch_root, {"PATH": "/usr/bin:/bin"})
        time.sleep(0.6)
        self.assertEqual(heartbeat.read_text(), "ready")

    @unittest.skipUnless(sys.platform == "darwin", "macOS supervision fixture")
    def test_real_timeout_terminates_the_owned_command(self):
        marker = self.root / "after-timeout"
        child = "import pathlib,sys,time; time.sleep(0.4); pathlib.Path(sys.argv[1]).write_text('survived')"
        with self.assertRaises(stage.subprocess.TimeoutExpired):
            stage.run_command([sys.executable, "-I", "-B", "-c", child, str(marker)],
                              self.inputs.scratch_root, {"PATH": "/usr/bin:/bin"}, timeout_seconds=0.1)
        time.sleep(0.5)
        self.assertFalse(marker.exists())

    def test_group_signal_failure_still_reaps_guardian(self):
        process = Mock(pid=123456, returncode=0)
        with self.supervision_primitives(), patch.object(stage.subprocess, "Popen", return_value=process), \
             patch.object(stage.os, "killpg", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                stage.run_command(["/fixture/compiler"], self.inputs.scratch_root, {})
        process.wait.assert_called_once_with(timeout=10)

    def test_missing_or_stale_engine_module_refuses_even_with_consistent_manifest(self):
        relative = "model_deck/integrations/clients/mcp/model_reads.py"
        source = self.source / "python/src" / relative
        source.write_text("# required source module\n")
        with self.assertRaisesRegex(stage.StageError, "differs from source"):
            self.package()
        vendor = self.vendor / relative
        vendor.write_text("# stale module\n")
        self.write_vendor_manifest()
        with self.assertRaisesRegex(stage.StageError, "differs from source"):
            self.package()
        self.assertEqual(self.commands.calls, [])

    def test_vendor_provenance_and_engine_requires_dist_are_required(self):
        manifest_path = self.vendor / "vendor-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["provenance"] = ""
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(stage.StageError, "explicit provenance"):
            self.package()
        self.write_vendor_manifest()
        metadata = self.vendor / "model_deck-0.0.0.dist-info/METADATA"
        metadata.write_text("Name: model-deck\nVersion: 0.0.0\n")
        self.write_vendor_manifest()
        with self.assertRaisesRegex(stage.StageError, "dependency metadata differs"):
            self.package()
        self.assertEqual(self.commands.calls, [])


if __name__ == "__main__":
    unittest.main()
