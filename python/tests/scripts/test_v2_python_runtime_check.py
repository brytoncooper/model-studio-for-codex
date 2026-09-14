from __future__ import annotations

import importlib.metadata
import importlib.util
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CHECKER_PATH = REPOSITORY_ROOT / "scripts" / "v2" / "check_python_runtime.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("v2_python_runtime_check", CHECKER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {CHECKER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class V2PythonRuntimeCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.checker = load_checker()

    def write_pyproject(self, dependencies: list[str]) -> Path:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        path = Path(temporary_directory.name) / "pyproject.toml"
        quoted = ", ".join(f'"{dependency}"' for dependency in dependencies)
        path.write_text(
            f'[project]\nname = "fixture"\nversion = "0"\ndependencies = [{quoted}]\n',
            encoding="utf-8",
        )
        return path

    def test_declared_dependencies_preserve_exact_versions_and_extract_distribution(self) -> None:
        path = self.write_pyproject(["jsonschema[format]==4.23.0", "tomlkit==0.13.3"])
        self.assertEqual(
            self.checker.declared_dependencies(path),
            (("jsonschema", "4.23.0"), ("tomlkit", "0.13.3")),
        )

    def test_runtime_rejects_python_older_than_3_11(self) -> None:
        path = self.write_pyproject([])
        with self.assertRaisesRegex(self.checker.RuntimeCheckError, "3.11 or newer"):
            self.checker.validate_runtime(path, version_info=(3, 10, 14))

    def test_runtime_rejects_missing_and_mismatched_distributions(self) -> None:
        path = self.write_pyproject(["example-package==1.2.3"])

        def missing(_name: str) -> str:
            raise importlib.metadata.PackageNotFoundError

        with self.assertRaisesRegex(self.checker.RuntimeCheckError, "is missing"):
            self.checker.validate_runtime(path, distribution_version=missing)
        with self.assertRaisesRegex(self.checker.RuntimeCheckError, "version mismatch"):
            self.checker.validate_runtime(
                path,
                distribution_version=lambda _name: "9.9.9",
            )

    def test_runtime_imports_every_declared_distribution(self) -> None:
        path = self.write_pyproject(["example-package==1.2.3"])
        imported: list[str] = []
        dependencies = self.checker.validate_runtime(
            path,
            distribution_version=lambda _name: "1.2.3",
            import_module=lambda name: imported.append(name),
        )
        self.assertEqual(dependencies, (("example-package", "1.2.3"),))
        self.assertEqual(imported, ["example_package"])

    def test_jsonschema_format_probe_rejects_an_unavailable_extra(self) -> None:
        class MissingFormatValidator:
            FORMAT_CHECKER = object()

            def __init__(self, _schema: object, *, format_checker: object) -> None:
                self.format_checker = format_checker

            def iter_errors(self, _value: object):
                return iter(())

        fake_jsonschema = type(
            "FakeJsonschema",
            (),
            {"Draft202012Validator": MissingFormatValidator},
        )
        with self.assertRaisesRegex(
            self.checker.RuntimeCheckError,
            "format checker is unavailable for: date-time",
        ):
            self.checker.validate_jsonschema_formats(fake_jsonschema, ("date-time",))

    def test_runtime_probes_every_format_declared_by_contract_schemas(self) -> None:
        path = self.write_pyproject(["jsonschema[format]==4.23.0"])
        schema_root = (
            path.parent
            / "src"
            / "model_deck_contracts"
            / "schemas"
            / "contracts"
        )
        schema_root.mkdir(parents=True)
        (schema_root / "fixture.json").write_text(
            '{"type":"object","properties":{'
            '"at":{"type":"string","format":"date-time"},'
            '"id":{"type":"string","format":"uuid"}}}',
            encoding="utf-8",
        )
        imported = importlib.import_module("jsonschema")
        dependencies = self.checker.validate_runtime(
            path,
            distribution_version=lambda _name: "4.23.0",
            import_module=lambda _name: imported,
        )
        self.assertEqual(dependencies, (("jsonschema", "4.23.0"),))

    def test_runtime_rejects_non_exact_dependency_contracts(self) -> None:
        path = self.write_pyproject(["tomlkit>=0.13"])
        with self.assertRaisesRegex(self.checker.RuntimeCheckError, "exact version"):
            self.checker.declared_dependencies(path)


if __name__ == "__main__":
    unittest.main()
