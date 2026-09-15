import unittest
from pathlib import Path

from development.architecture.checker import ArchitectureChecker
from development.architecture.graph import layer_for_module


FIXTURES = Path(__file__).resolve().parent / "development" / "architecture" / "fixtures"
REPO = Path(__file__).resolve().parent


class ArchitectureGraphTests(unittest.TestCase):
    def test_dynamic_import_aliases_inside_functions_are_detected(self):
        from development.architecture.ast_imports import collect_dynamic_imports, parse_source
        examples = [
            "def load():\n    import importlib as dynamic\n    dynamic.import_module('sqlite3')\n",
            "def load():\n    from importlib import import_module as load_module\n    load_module('sqlite3')\n",
        ]
        for source in examples:
            with self.subTest(source=source):
                records = collect_dynamic_imports(parse_source(source))
                self.assertEqual([record.module_expr for record in records], ["sqlite3"])

    def test_layer_mapping_for_planned_packages(self):
        self.assertEqual(layer_for_module("model_deck.kernel.registry").value, "kernel")
        self.assertEqual(layer_for_module("model_deck.engine.model_library.use_cases").value, "engine")
        self.assertEqual(layer_for_module("model_deck.engine.builtins.descriptors").value, "engine")
        self.assertEqual(
            layer_for_module("model_deck.integrations.providers.openrouter.client").value,
            "providers",
        )
        self.assertEqual(layer_for_module("model_deck.bootstrap.composition").value, "bootstrap")
        self.assertEqual(layer_for_module("model_deck").value, "bootstrap")
        self.assertEqual(layer_for_module("model_deck.cli.main").value, "bootstrap")
        self.assertEqual(layer_for_module("model_deck.cli_like").value, "unknown")
        self.assertEqual(layer_for_module("model_deck.plugins").value, "plugin_runtime")
        self.assertEqual(layer_for_module("model_deck.plugins.process_runtime.runtime").value, "plugin_runtime")
        self.assertEqual(layer_for_module("model_deck.plugins_like").value, "unknown")
        self.assertEqual(layer_for_module("model_deck_plugin.notebook.panel").value, "plugin")
        self.assertEqual(layer_for_module("model_deck_sdk.client").value, "plugin")


class ArchitectureCheckerFixtureTests(unittest.TestCase):
    def test_plugin_runtime_uses_public_contracts_and_owned_infrastructure(self):
        checker, result = self._check_fixture(
            "positive/plugin_runtime_public_imports.py", "model_deck.plugins.process_runtime.runtime"
        )
        self.assertEqual(checker.failing_violations(result), ())

    def test_bootstrap_can_compose_plugin_runtime(self):
        checker, result = self._check_fixture(
            "positive/bootstrap_imports_plugin_runtime.py", "model_deck.bootstrap.composition"
        )
        self.assertEqual(checker.failing_violations(result), ())

    def test_core_and_external_plugins_cannot_import_plugin_runtime(self):
        for module in (
            "model_deck.kernel.registry",
            "model_deck.engine.plugin_authority.service",
            "model_deck_plugin.notebook.panel",
            "model_deck_sdk.client",
        ):
            with self.subTest(module=module):
                checker, result = self._check_fixture("negative/imports_plugin_runtime.py", module)
                self.assertIn("layer_import", {v.rule_id for v in checker.failing_violations(result)})

    def test_plugin_runtime_cannot_import_host_or_provider(self):
        for fixture in (
            "negative/plugin_runtime_imports_host.py",
            "negative/plugin_runtime_imports_provider.py",
        ):
            with self.subTest(fixture=fixture):
                checker, result = self._check_fixture(fixture, "model_deck.plugins.process_runtime.runtime")
                self.assertIn("layer_import", {v.rule_id for v in checker.failing_violations(result)})

    def test_plugin_runtime_cannot_import_private_engine_modules_or_symbols(self):
        checker, result = self._check_fixture(
            "negative/plugin_runtime_imports_private_engine.py", "model_deck.plugins.process_runtime.runtime"
        )
        private_lines = {v.line for v in checker.failing_violations(result) if v.rule_id == "private_import"}
        self.assertEqual(private_lines, {1, 2})

    def test_cli_can_select_bootstrap(self):
        checker, result = self._check_fixture(
            "positive/cli_composition.py", "model_deck.cli.main"
        )
        self.assertEqual(checker.failing_violations(result), ())

    def test_core_cannot_import_cli_composition(self):
        for module in ("model_deck.kernel.registry", "model_deck.engine.sessions"):
            with self.subTest(module=module):
                checker, result = self._check_fixture("negative/core_imports_cli.py", module)
                self.assertTrue(checker.failing_violations(result))

    def _checker(self, overrides: dict[Path, str]) -> ArchitectureChecker:
        return ArchitectureChecker(repo_root=REPO, module_name_overrides=overrides)

    def _check_fixture(self, relpath: str, logical_module: str):
        path = FIXTURES / relpath
        checker = self._checker({path.resolve(): logical_module})
        result = checker.check_paths([path.resolve()])
        return checker, result

    def test_positive_kernel_contract_import_passes(self):
        checker, result = self._check_fixture(
            "positive/kernel_ok.py", "model_deck.kernel.registry"
        )
        self.assertEqual(checker.failing_violations(result), ())

    def test_positive_dynamic_loader_exception_passes(self):
        checker, result = self._check_fixture(
            "positive/allowed_dynamic_loader.py", "model_deck.bootstrap.loader"
        )
        self.assertEqual(checker.failing_violations(result), ())

    def test_negative_kernel_platform_import_fails(self):
        checker, result = self._check_fixture(
            "negative/kernel_imports_platform.py", "model_deck.kernel.locks"
        )
        failing = checker.failing_violations(result)
        self.assertTrue(any(v.rule_id == "forbidden_platform" for v in failing))

    def test_negative_engine_sqlite_fails(self):
        checker, result = self._check_fixture(
            "negative/engine_imports_sqlite.py", "model_deck.engine.connections.store"
        )
        failing = checker.failing_violations(result)
        self.assertTrue(any(v.rule_id == "forbidden_platform" for v in failing))

    def test_negative_provider_router_back_import_fails(self):
        checker, result = self._check_fixture(
            "negative/provider_router_back_import.py",
            "model_deck.integrations.providers.openrouter.adapter",
        )
        failing = checker.failing_violations(result)
        self.assertTrue(any(v.rule_id == "provider_router_back_import" for v in failing))

    def test_negative_cross_feature_private_fails(self):
        checker, result = self._check_fixture(
            "negative/cross_feature_private.py",
            "model_deck.engine.model_library.use_cases",
        )
        failing = checker.failing_violations(result)
        rules = {v.rule_id for v in failing}
        self.assertTrue({"cross_feature_private", "private_import"} & rules)

    def test_negative_builtin_descriptor_private_engine_fails(self):
        checker, result = self._check_fixture(
            "negative/builtin_private_engine.py",
            "model_deck.engine.builtins.descriptors",
        )
        failing = checker.failing_violations(result)
        rules = {v.rule_id for v in failing}
        self.assertTrue({"cross_feature_private", "private_import"} & rules)

    def test_negative_plugin_private_engine_fails(self):
        checker, result = self._check_fixture(
            "negative/plugin_private_engine.py", "model_deck_plugin.notebook.panel"
        )
        failing = checker.failing_violations(result)
        self.assertTrue(any(v.rule_id == "plugin_private_engine" for v in failing))

    def test_negative_dynamic_import_without_allowlist_fails(self):
        checker, result = self._check_fixture(
            "negative/dynamic_import_hidden.py", "model_deck.engine.extensions.loader"
        )
        failing = checker.failing_violations(result)
        self.assertTrue(any(v.rule_id == "dynamic_import" for v in failing))

    def test_negative_service_locator_fails(self):
        checker, result = self._check_fixture(
            "negative/service_locator.py", "model_deck.engine.routing.policy"
        )
        failing = checker.failing_violations(result)
        self.assertTrue(any(v.rule_id == "service_locator" for v in failing))

    def test_negative_engine_concrete_adapter_fails(self):
        checker, result = self._check_fixture(
            "negative/bootstrap_only_concrete.py", "model_deck.engine.connections.use_cases"
        )
        failing = checker.failing_violations(result)
        self.assertTrue(any(v.rule_id == "concrete_adapter_in_core" for v in failing))

    def test_baseline_legacy_is_report_only_until_enforced(self):
        path = REPO / "development" / "architecture" / "fixtures" / "negative" / "provider_router_back_import.py"
        baseline_path = REPO / "development" / "architecture" / "baseline_legacy.json"
        import json

        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        payload["files"].append(
            {
                "relpath": path.relative_to(REPO).as_posix(),
                "enforce": False,
                "note": "test fixture baseline entry",
            }
        )
        temp_baseline = REPO / "development" / "architecture" / "baseline_legacy.test.json"
        temp_baseline.write_text(json.dumps(payload), encoding="utf-8")
        try:
            checker_with_baseline = ArchitectureChecker(
                repo_root=REPO,
                baseline_path=temp_baseline,
                module_name_overrides={path.resolve(): "legacy_baseline.provider_router_back_import"},
            )
            result = checker_with_baseline.check_paths([path.resolve()])
            self.assertTrue(result.violations)
            self.assertTrue(all(v.severity == "warning" for v in result.violations))
            self.assertEqual(checker_with_baseline.failing_violations(result), ())
        finally:
            temp_baseline.unlink(missing_ok=True)



    def test_negative_from_model_deck_import_adapters_fails(self):
        checker, result = self._check_fixture(
            "negative/from_model_deck_import_adapters.py",
            "model_deck.kernel.registry",
        )
        failing = checker.failing_violations(result)
        self.assertTrue(any(v.rule_id == "layer_import" for v in failing))

    def test_negative_engine_imports_openai_fails(self):
        checker, result = self._check_fixture(
            "negative/engine_imports_openai.py",
            "model_deck.engine.connections.use_cases",
        )
        failing = checker.failing_violations(result)
        self.assertTrue(any(v.rule_id == "forbidden_platform" for v in failing))

    def test_negative_relative_import_private_member_fails(self):
        checker, result = self._check_fixture(
            "negative/relative_private_sibling.py",
            "model_deck.engine.model_library.use_cases",
        )
        failing = checker.failing_violations(result)
        rules = {v.rule_id for v in failing}
        self.assertTrue({"private_import", "cross_feature_private"} & rules)

    def test_positive_package_init_relative_import(self):
        path = FIXTURES / "positive" / "package_init_relative.py"
        checker = self._checker(
            {
                path.resolve(): "model_deck.engine.routing",
            }
        )
        result = checker.check_paths([path.resolve()])
        self.assertEqual(checker.failing_violations(result), ())


    def test_negative_engine_imports_http_client_fails(self):
        checker, result = self._check_fixture(
            "negative/engine_imports_http_client.py",
            "model_deck.engine.connections.use_cases",
        )
        failing = checker.failing_violations(result)
        self.assertTrue(any(v.rule_id == "forbidden_platform" for v in failing))

    def test_negative_import_alias_forbidden_sdk_fails(self):
        checker, result = self._check_fixture(
            "negative/import_alias_forbidden_sdk.py",
            "model_deck.engine.model_library.use_cases",
        )
        failing = checker.failing_violations(result)
        self.assertTrue(any(v.rule_id == "forbidden_platform" for v in failing))

    def test_negative_dynamic_import_importlib_alias_fails(self):
        checker, result = self._check_fixture(
            "negative/dynamic_import_importlib_alias.py",
            "model_deck.engine.extensions.loader",
        )
        failing = checker.failing_violations(result)
        self.assertTrue(any(v.rule_id == "dynamic_import" for v in failing))

    def test_fail_closed_unknown_source_in_engine_tree(self):
        path = FIXTURES / "positive" / "kernel_ok.py"
        checker = ArchitectureChecker(
            repo_root=REPO,
            module_name_overrides={
                path.resolve(): "model_deck.unregistered_feature.module",
            },
        )
        result = checker.check_paths([path.resolve()])
        failing = checker.failing_violations(result)
        self.assertTrue(any(v.rule_id == "unknown_source_layer" for v in failing))

    def test_fail_closed_unknown_model_deck_target(self):
        checker, result = self._check_fixture(
            "negative/unknown_model_deck_target.py",
            "model_deck.engine.routing.policy",
        )
        failing = checker.failing_violations(result)
        self.assertTrue(any(v.rule_id == "unknown_target_layer" for v in failing))


class ArchitectureCheckCliTests(unittest.TestCase):
    def test_discover_fixture_tree_passes_positive_cases(self):
        checker = ArchitectureChecker(
            repo_root=REPO,
            module_name_overrides={
                (FIXTURES / "positive" / "kernel_ok.py").resolve(): "model_deck.kernel.registry",
                (FIXTURES / "positive" / "allowed_dynamic_loader.py").resolve(): "model_deck.bootstrap.loader",
            },
        )
        roots = [FIXTURES / "positive"]
        files = checker.discover_python_files(roots)
        result = checker.check_paths(files)
        self.assertEqual(checker.failing_violations(result), ())


if __name__ == "__main__":
    unittest.main()
