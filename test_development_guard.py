import os
import tempfile
import unittest
from pathlib import Path

from development.guard.paths import (
    DevelopmentGuardError,
    actual_user_home,
    canonicalize_path,
    is_protected_path,
    protected_roots,
    repo_root,
)
from development.guard.roots import validate_isolated_roots
from development.inventory import baseline_record, python_unittest_inventory


def architecture_repo() -> Path:
    return repo_root(Path(__file__).resolve().parent)


class DevelopmentGuardTests(unittest.TestCase):
    def test_repo_root_points_at_model_deck(self):
        root = architecture_repo()
        self.assertTrue((root / "build.sh").is_file())

    def test_default_app_output_is_protected(self):
        root = architecture_repo()
        default_app = root.parent / "Model Deck.app"
        self.assertTrue(is_protected_path(default_app, source_root=root))

    def test_application_support_is_protected(self):
        root = architecture_repo()
        support = actual_user_home() / "Library" / "Application Support" / "Model Deck"
        self.assertTrue(is_protected_path(support, source_root=root))

    def test_codex_directory_is_protected(self):
        root = architecture_repo()
        codex = actual_user_home() / ".codex"
        self.assertTrue(is_protected_path(codex, source_root=root))

    def test_rejects_ancestor_containing_application_support(self):
        root = architecture_repo()
        home = actual_user_home()
        with tempfile.TemporaryDirectory() as artifact:
            with self.assertRaises(DevelopmentGuardError):
                validate_isolated_roots(home, artifact, source_root=root)

    def test_isolated_roots_accept_disjoint_temp_dirs(self):
        root = architecture_repo()
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as artifact:
            validate_isolated_roots(state, artifact, source_root=root)

    def test_tmp_resolves_to_private_tmp_canonical_prefix(self):
        root = architecture_repo()
        with tempfile.TemporaryDirectory(dir="/tmp") as state, tempfile.TemporaryDirectory() as artifact:
            state_canonical = canonicalize_path(Path(state))
            self.assertTrue(str(state_canonical).startswith("/private/tmp/") or str(state_canonical).startswith("/tmp/"))
            validate_isolated_roots(state, artifact, source_root=root)

    def test_rejects_protected_state_root(self):
        root = architecture_repo()
        protected = protected_roots(root)[0]
        with tempfile.TemporaryDirectory() as artifact:
            with self.assertRaises(DevelopmentGuardError):
                validate_isolated_roots(protected, artifact, source_root=root)

    def test_rejects_same_state_and_artifact(self):
        root = architecture_repo()
        with tempfile.TemporaryDirectory() as state:
            with self.assertRaises(DevelopmentGuardError):
                validate_isolated_roots(state, state, source_root=root)

    def test_rejects_nested_roots(self):
        root = architecture_repo()
        with tempfile.TemporaryDirectory() as parent:
            child = Path(parent) / "child"
            child.mkdir()
            with self.assertRaises(DevelopmentGuardError):
                validate_isolated_roots(parent, child, source_root=root)

    def test_rejects_existing_file_as_state_root(self):
        root = architecture_repo()
        with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as artifact:
            blocker = Path(parent) / "not-a-dir"
            blocker.write_text("x", encoding="utf-8")
            with self.assertRaises(DevelopmentGuardError):
                validate_isolated_roots(blocker, artifact, source_root=root)

    def test_rejects_symlink_alias_for_state_root(self):
        root = architecture_repo()
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as artifact:
            alias = Path(state) / "alias"
            alias.symlink_to(state)
            with self.assertRaises(DevelopmentGuardError):
                validate_isolated_roots(alias, artifact, source_root=root)

    def test_rejects_parent_symlink_alias(self):
        root = architecture_repo()
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as artifact:
            real = Path(temp) / "real"
            real.mkdir()
            link = Path(temp) / "linked"
            link.symlink_to(real)
            state = link / "state"
            state.mkdir()
            with self.assertRaises(DevelopmentGuardError):
                validate_isolated_roots(state, artifact, source_root=root)

    def test_rejects_symlink_to_protected_target(self):
        root = architecture_repo()
        protected = protected_roots(root)[0]
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as artifact:
            alias = Path(temp) / "bundle-alias"
            try:
                alias.symlink_to(protected)
            except OSError:
                self.skipTest("cannot create symlink to protected path in this environment")
            with self.assertRaises(DevelopmentGuardError):
                validate_isolated_roots(alias, artifact, source_root=root)

    def test_baseline_inventory_lists_python_tests(self):
        root = architecture_repo()
        record = baseline_record(root)
        self.assertGreater(record["python_test_modules"], 0)
        self.assertIn("test_development_guard", record["python_modules"])

    def test_python_inventory_counts_methods_without_importing(self):
        root = architecture_repo()
        counts = python_unittest_inventory(root)
        self.assertGreater(counts["test_development_guard"], 0)


    def test_rejects_active_source_checkout_as_state_root(self):
        root = architecture_repo()
        with tempfile.TemporaryDirectory() as artifact:
            with self.assertRaises(DevelopmentGuardError):
                validate_isolated_roots(root, artifact, source_root=root)

    def test_rejects_paths_inside_source_outside_work_subpaths(self):
        root = architecture_repo()
        with tempfile.TemporaryDirectory() as state:
            with self.assertRaises(DevelopmentGuardError):
                validate_isolated_roots(state, root / "local_router.py", source_root=root)

    def test_permits_work_worktrees_subpath(self):
        root = architecture_repo()
        worktrees = root / "work" / "worktrees"
        worktrees.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(worktrees), prefix="guard-") as base:
            state = Path(base) / "state"
            artifact = Path(base) / "dist"
            state.mkdir()
            artifact.mkdir()
            validate_isolated_roots(state, artifact, source_root=root)

    def test_rejects_sibling_source_checkout_as_artifact_root(self):
        root = architecture_repo()
        sibling = root.parent / "Model Deck"
        if not (sibling / "build.sh").is_file():
            self.skipTest("sibling Model Deck checkout not present")
        with tempfile.TemporaryDirectory() as state:
            with self.assertRaises(DevelopmentGuardError):
                validate_isolated_roots(state, sibling, source_root=root)

    def test_rejects_sibling_work_subdirectory_as_state_root(self):
        root = architecture_repo()
        sibling = root.parent / "Model Deck"
        if not (sibling / "build.sh").is_file():
            self.skipTest("sibling Model Deck checkout not present")
        sibling_work = sibling / "work"
        with tempfile.TemporaryDirectory() as artifact:
            with self.assertRaises(DevelopmentGuardError):
                validate_isolated_roots(sibling_work, artifact, source_root=root)

    def test_rejects_deep_path_under_sibling_work(self):
        root = architecture_repo()
        sibling = root.parent / "Model Deck"
        if not (sibling / "build.sh").is_file():
            self.skipTest("sibling Model Deck checkout not present")
        deep = sibling / "work" / "nested" / "state"
        with tempfile.TemporaryDirectory() as artifact:
            with self.assertRaises(DevelopmentGuardError):
                validate_isolated_roots(deep, artifact, source_root=root)

    def test_permits_work_isolated_subpath(self):
        root = architecture_repo()
        isolated = root / "work" / "isolated"
        isolated.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(isolated), prefix="guard-") as base:
            state = Path(base) / "state"
            artifact = Path(base) / "dist"
            state.mkdir()
            artifact.mkdir()
            validate_isolated_roots(state, artifact, source_root=root)

    def test_legitimate_temp_symlink_requires_exact_system_paths(self):
        from development.guard.paths import legitimate_temp_symlink

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "target"
            target.mkdir()
            fake = Path(temp) / "tmp"
            fake.symlink_to(target)
            self.assertFalse(legitimate_temp_symlink(fake))

        if Path("/tmp").is_symlink():
            self.assertTrue(legitimate_temp_symlink(Path("/tmp")))
        if Path("/var").is_symlink():
            self.assertTrue(legitimate_temp_symlink(Path("/var")))



class DevelopmentGuardAuthorityTests(unittest.TestCase):
    def test_development_guard_error_aliases_authority(self):
        import sys

        python_src = str(architecture_repo() / "python" / "src")
        if python_src not in sys.path:
            sys.path.insert(0, python_src)
        from model_deck_root_guard.paths import IsolatedRootGuardError

        self.assertIs(DevelopmentGuardError, IsolatedRootGuardError)

    def test_validate_isolated_roots_is_shared_authority(self):
        import sys

        python_src = str(architecture_repo() / "python" / "src")
        if python_src not in sys.path:
            sys.path.insert(0, python_src)
        from model_deck_root_guard.roots import validate_isolated_roots as authority_validate
        from development.guard.roots import validate_isolated_roots as dev_validate

        self.assertIs(dev_validate, authority_validate)

    def test_runtime_validate_isolated_roots_is_shared_authority(self):
        import sys

        python_src = str(architecture_repo() / "python" / "src")
        if python_src not in sys.path:
            sys.path.insert(0, python_src)
        from model_deck_root_guard.roots import validate_isolated_roots as authority_validate
        from model_deck.adapters.platform.macos.isolated_roots import (
            validate_isolated_roots as runtime_validate,
        )

        self.assertIs(runtime_validate, authority_validate)

    def test_dev_and_runtime_reject_same_protected_path(self):
        import sys

        python_src = str(architecture_repo() / "python" / "src")
        if python_src not in sys.path:
            sys.path.insert(0, python_src)
        from model_deck.adapters.platform.macos.isolated_roots import (
            validate_isolated_roots as runtime_validate,
        )

        root = architecture_repo()
        protected = protected_roots(root)[0]
        with tempfile.TemporaryDirectory() as artifact:
            with self.assertRaises(DevelopmentGuardError):
                validate_isolated_roots(protected, artifact, source_root=root)
            with self.assertRaises(DevelopmentGuardError):
                runtime_validate(protected, artifact, source_root=root)

    def test_dev_and_runtime_accept_disjoint_temp_roots(self):
        import sys

        python_src = str(architecture_repo() / "python" / "src")
        if python_src not in sys.path:
            sys.path.insert(0, python_src)
        from model_deck.adapters.platform.macos.isolated_roots import (
            validate_isolated_roots as runtime_validate,
        )

        root = architecture_repo()
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as artifact:
            dev_result = validate_isolated_roots(state, artifact, source_root=root)
            runtime_result = runtime_validate(state, artifact, source_root=root)
            self.assertEqual(dev_result, runtime_result)

if __name__ == "__main__":
    unittest.main()
