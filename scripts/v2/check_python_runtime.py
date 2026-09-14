#!/usr/bin/env python3
"""Validate the external Python runtime selected for a V2 application."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import re
import sys
from pathlib import Path
from typing import Callable


MINIMUM_PYTHON = (3, 11)
SUCCESS_PREFIX = "model-deck-python-runtime-ok"
EXACT_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_.,-]+\])?==(?P<version>[^;\s]+)$"
)
JSONSCHEMA_FORMAT_PROBES = {
    "date-time": ("2026-09-14T12:34:56Z", "not-a-date-time"),
    "uuid": ("123e4567-e89b-12d3-a456-426614174000", "not-a-uuid"),
}


class RuntimeCheckError(ValueError):
    """The selected interpreter cannot run the packaged V2 engine."""


def declared_dependencies(pyproject_path: Path) -> tuple[tuple[str, str], ...]:
    try:
        import tomllib
    except ModuleNotFoundError as error:
        raise RuntimeCheckError("Python 3.11 or newer is required") from error
    try:
        with pyproject_path.open("rb") as pyproject_file:
            document = tomllib.load(pyproject_file)
        requirements = document["project"]["dependencies"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise RuntimeCheckError(
            f"could not read dependencies from {pyproject_path}"
        ) from error

    dependencies: list[tuple[str, str]] = []
    for requirement in requirements:
        if not isinstance(requirement, str):
            raise RuntimeCheckError("python dependency must be a string")
        match = EXACT_REQUIREMENT.fullmatch(requirement)
        if match is None:
            raise RuntimeCheckError(
                f"python dependency must use an exact version: {requirement}"
            )
        dependencies.append((match.group("name"), match.group("version")))
    return tuple(dependencies)


def declared_contract_formats(pyproject_path: Path) -> tuple[str, ...]:
    schemas_root = (
        pyproject_path.parent
        / "src"
        / "model_deck_contracts"
        / "schemas"
        / "contracts"
    )
    if not schemas_root.is_dir():
        raise RuntimeCheckError(f"contract schemas are missing: {schemas_root}")

    formats: set[str] = set()

    def collect_formats(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "format" and isinstance(child, str):
                    formats.add(child)
                collect_formats(child)
        elif isinstance(value, list):
            for child in value:
                collect_formats(child)

    for schema_path in sorted(schemas_root.rglob("*.json")):
        try:
            document = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeCheckError(
                f"could not read contract schema: {schema_path}"
            ) from error
        collect_formats(document)
    return tuple(sorted(formats))


def validate_jsonschema_formats(jsonschema_module: object, formats: tuple[str, ...]) -> None:
    unsupported = sorted(set(formats) - JSONSCHEMA_FORMAT_PROBES.keys())
    if unsupported:
        raise RuntimeCheckError(
            "JSON Schema format probe is missing for: " + ", ".join(unsupported)
        )

    validator_type = getattr(jsonschema_module, "Draft202012Validator", None)
    format_checker = getattr(validator_type, "FORMAT_CHECKER", None)
    if validator_type is None or format_checker is None:
        raise RuntimeCheckError("jsonschema Draft 2020-12 format checker is unavailable")

    for format_name in formats:
        valid_value, invalid_value = JSONSCHEMA_FORMAT_PROBES[format_name]
        validator = validator_type(
            {"type": "string", "format": format_name},
            format_checker=format_checker,
        )
        if list(validator.iter_errors(valid_value)):
            raise RuntimeCheckError(
                f"jsonschema format checker rejected the valid {format_name} probe"
            )
        if not list(validator.iter_errors(invalid_value)):
            raise RuntimeCheckError(
                f"jsonschema format checker is unavailable for: {format_name}"
            )


def validate_runtime(
    pyproject_path: Path,
    *,
    version_info: tuple[int, ...] = tuple(sys.version_info),
    distribution_version: Callable[[str], str] = importlib.metadata.version,
    import_module: Callable[[str], object] = importlib.import_module,
) -> tuple[tuple[str, str], ...]:
    if version_info[:2] < MINIMUM_PYTHON:
        actual = ".".join(str(part) for part in version_info[:3])
        raise RuntimeCheckError(
            f"Python 3.11 or newer is required; selected interpreter is {actual}"
        )

    dependencies = declared_dependencies(pyproject_path)
    for distribution_name, expected_version in dependencies:
        try:
            actual_version = distribution_version(distribution_name)
        except importlib.metadata.PackageNotFoundError as error:
            raise RuntimeCheckError(
                f"required Python distribution is missing: {distribution_name}=={expected_version}"
            ) from error
        if actual_version != expected_version:
            raise RuntimeCheckError(
                f"Python distribution version mismatch for {distribution_name}: "
                f"expected {expected_version}, found {actual_version}"
            )
        module_name = distribution_name.replace("-", "_")
        try:
            imported_module = import_module(module_name)
        except Exception as error:
            raise RuntimeCheckError(
                f"required Python module cannot be imported: {module_name}"
            ) from error
        if distribution_name.lower().replace("-", "_") == "jsonschema":
            validate_jsonschema_formats(
                imported_module,
                declared_contract_formats(pyproject_path),
            )
    return dependencies


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if len(arguments) != 1:
        print("usage: check_python_runtime.py ABSOLUTE_PYPROJECT_PATH", file=sys.stderr)
        return 2
    pyproject_path = Path(arguments[0])
    if not pyproject_path.is_absolute():
        print("Python runtime check requires an absolute pyproject path", file=sys.stderr)
        return 2
    try:
        dependencies = validate_runtime(pyproject_path)
    except RuntimeCheckError as error:
        print(f"Python runtime check failed: {error}", file=sys.stderr)
        return 1

    versions = ",".join(f"{name}=={version}" for name, version in dependencies)
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    print(f"{SUCCESS_PREFIX} python={python_version} dependencies={versions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
