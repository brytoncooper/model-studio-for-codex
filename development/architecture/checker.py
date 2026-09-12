from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from development.architecture.ast_imports import (
    ImportRecord,
    collect_dynamic_imports,
    collect_imports,
    collect_service_locator_calls,
    parse_source,
    read_module_source,
)
from development.architecture.baseline import baseline_index, load_baseline
from development.architecture.dynamic_allowlist import (
    dynamic_import_allowed,
    load_dynamic_allowlist,
)
from development.architecture.forbidden import (
    forbidden_root_for_layer,
    stdlib_root_allowed_for_core,
)
from development.architecture.graph import (
    ALLOWED_LAYER_IMPORTS,
    Layer,
    PROVIDER_ROUTER_FORBIDDEN,
    engine_feature_root,
    is_intended_product_tree_module,
    is_model_deck_namespace_module,
    is_private_module,
    layer_for_module,
)
from development.architecture.types import Violation


@dataclass(frozen=True)
class CheckResult:
    violations: tuple[Violation, ...]
    files_checked: int


@dataclass(frozen=True)
class ResolvedImport:
    target_module: str
    imported_names: tuple[str, ...]


class ArchitectureChecker:
    def __init__(
        self,
        *,
        repo_root: Path,
        baseline_path: Path | None = None,
        dynamic_allowlist_path: Path | None = None,
        module_name_overrides: dict[Path, str] | None = None,
    ) -> None:
        self._repo_root = repo_root.resolve()
        self._baseline = baseline_index(load_baseline(baseline_path))
        self._dynamic_allowlist = load_dynamic_allowlist(dynamic_allowlist_path)
        self._module_name_overrides = module_name_overrides or {}

    def check_paths(self, paths: list[Path]) -> CheckResult:
        violations: list[Violation] = []
        checked = 0
        for path in paths:
            if not path.is_file() or path.suffix != ".py":
                continue
            checked += 1
            violations.extend(self._check_file(path))
        return CheckResult(tuple(violations), checked)

    def discover_python_files(self, roots: list[Path]) -> list[Path]:
        files: list[Path] = []
        for root in roots:
            root = root.resolve()
            if root.is_file() and root.suffix == ".py":
                files.append(root)
                continue
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                rel = path.relative_to(self._repo_root).as_posix()
                if rel.startswith("development/architecture/fixtures/"):
                    continue
                files.append(path)
        return files

    def _is_package_init(self, path: Path) -> bool:
        if path in self._module_name_overrides:
            relpath = path.relative_to(self._repo_root).as_posix()
            if relpath.startswith("python/src/"):
                return relpath.endswith("/__init__.py")
            return path.name == "__init__.py"
        relpath = path.relative_to(self._repo_root).as_posix()
        return relpath.endswith("/__init__.py") or path.name == "__init__.py"

    def _logical_module(self, path: Path) -> str:
        if path in self._module_name_overrides:
            return self._module_name_overrides[path]
        relpath = path.relative_to(self._repo_root).as_posix()
        entry = self._baseline.get(relpath)
        if entry is not None:
            stem = Path(relpath).stem
            return f"legacy_baseline.{stem}"
        if relpath.startswith("python/src/"):
            tail = relpath[len("python/src/") :]
            if tail.endswith("/__init__.py"):
                tail = tail[: -len("/__init__.py")]
            elif tail.endswith(".py"):
                tail = tail[: -3]
            return tail.replace("/", ".")
        return relpath.replace("/", ".").removesuffix(".py")

    def _enforce_for_file(self, path: Path) -> bool:
        relpath = path.relative_to(self._repo_root).as_posix()
        entry = self._baseline.get(relpath)
        if entry is None:
            return True
        return entry.enforce

    def _severity(self, path: Path, *, rule_id: str, message: str, line: int | None) -> Violation:
        enforce = self._enforce_for_file(path)
        return Violation(
            path=path.relative_to(self._repo_root).as_posix(),
            rule_id=rule_id,
            message=message,
            line=line,
            severity="error" if enforce else "warning",
        )

    def _containing_package(self, module_name: str, *, is_package_init: bool) -> str:
        if is_package_init:
            return module_name
        if "." not in module_name:
            return ""
        return module_name.rsplit(".", 1)[0]

    def _resolve_import_targets(
        self,
        module_name: str,
        record: ImportRecord,
        *,
        is_package_init: bool,
    ) -> list[ResolvedImport]:
        if record.is_relative:
            package = self._containing_package(module_name, is_package_init=is_package_init)
            if not package and record.relative_level > 0:
                return []
            parts = package.split(".") if package else []
            up = record.relative_level - 1
            if up > 0:
                if up > len(parts):
                    parts = []
                else:
                    parts = parts[:-up]
            base = ".".join(part for part in parts if part)
            if record.module:
                target = ".".join(part for part in (base, record.module) if part)
            else:
                target = base
            if not target:
                return []
            if record.kind == "from" and record.names and not record.module:
                return [
                    ResolvedImport(
                        target_module=".".join(part for part in (target, name) if part),
                        imported_names=(name,),
                    )
                    for name in record.names
                ]
            return [ResolvedImport(target_module=target, imported_names=record.names)]

        if record.kind == "import" and record.module:
            return [ResolvedImport(target_module=record.module, imported_names=record.names)]

        if record.kind == "from" and record.module:
            parent = record.module
            if record.names:
                return [
                    ResolvedImport(
                        target_module=".".join((parent, name)),
                        imported_names=(name,),
                    )
                    for name in record.names
                ]
            return [ResolvedImport(target_module=parent, imported_names=record.names)]

        return []

    def _is_architecture_managed_import(self, target_module: str) -> bool:
        if target_module in PROVIDER_ROUTER_FORBIDDEN:
            return True
        prefixes = (
            "model_deck.",
            "model_deck_contracts",
            "model_deck_plugin.",
            "model_deck_sdk.",
            "legacy_baseline.",
        )
        return any(
            target_module == prefix.rstrip(".") or target_module.startswith(prefix)
            for prefix in prefixes
        )

    def _check_file(self, path: Path) -> list[Violation]:
        source = read_module_source(path)
        try:
            tree = parse_source(source, path=str(path))
        except SyntaxError as exc:
            return [
                self._severity(
                    path,
                    rule_id="syntax_error",
                    message=f"cannot analyze imports: {exc.msg}",
                    line=getattr(exc, "lineno", None),
                )
            ]
        module_name = self._logical_module(path)
        is_package_init = self._is_package_init(path)
        source_layer = layer_for_module(module_name)
        if module_name.startswith("legacy_baseline."):
            source_layer = Layer.LEGACY_BASELINE
        violations: list[Violation] = []
        relpath = path.relative_to(self._repo_root).as_posix()

        if is_intended_product_tree_module(module_name) and source_layer is Layer.UNKNOWN:
            violations.append(
                self._severity(
                    path,
                    rule_id="unknown_source_layer",
                    message=(
                        "module is under the engine/plugin tree but has no known layer: "
                        f"{module_name}"
                    ),
                    line=1,
                )
            )

        for record in collect_imports(tree):
            for resolved in self._resolve_import_targets(
                module_name, record, is_package_init=is_package_init
            ):
                violations.extend(
                    self._check_import(
                        path,
                        source_layer=source_layer,
                        source_module=module_name,
                        target_module=resolved.target_module,
                        line=record.line,
                        imported_names=resolved.imported_names,
                    )
                )

        for dynamic in collect_dynamic_imports(tree):
            if not dynamic_import_allowed(
                file_relpath=relpath,
                module_expr=dynamic.module_expr,
                allowlist=self._dynamic_allowlist,
            ):
                violations.append(
                    self._severity(
                        path,
                        rule_id="dynamic_import",
                        message=(
                            "dynamic import requires an explicit loader exception: "
                            f"{dynamic.module_expr!r}"
                        ),
                        line=dynamic.line,
                    )
                )

        for locator in collect_service_locator_calls(tree):
            if source_layer in {Layer.KERNEL, Layer.ENGINE, Layer.ADAPTERS, Layer.INTEGRATIONS}:
                violations.append(
                    self._severity(
                        path,
                        rule_id="service_locator",
                        message=f"service locator call is not allowed here: {locator.callee}",
                        line=locator.line,
                    )
                )

        return violations

    def _check_import(
        self,
        path: Path,
        *,
        source_layer: Layer,
        source_module: str,
        target_module: str,
        line: int,
        imported_names: tuple[str, ...] = (),
    ) -> list[Violation]:
        violations: list[Violation] = []
        root = target_module.split(".", 1)[0]
        if forbidden_root_for_layer(source_layer, root):
            violations.append(
                self._severity(
                    path,
                    rule_id="forbidden_platform",
                    message=(
                        f"{source_layer.value} must not import platform or product module "
                        f"{target_module!r}"
                    ),
                    line=line,
                )
            )

        managed = self._is_architecture_managed_import(target_module)
        if source_layer in {Layer.KERNEL, Layer.ENGINE} and not managed:
            if not stdlib_root_allowed_for_core(source_layer, root):
                violations.append(
                    self._severity(
                        path,
                        rule_id="forbidden_platform",
                        message=(
                            f"{source_layer.value} may only import stdlib allowlist modules "
                            f"or approved contracts; got {target_module!r}"
                        ),
                        line=line,
                    )
                )
                return violations

        if not managed:
            return violations

        target_layer = layer_for_module(target_module)
        if is_model_deck_namespace_module(target_module) and target_layer is Layer.UNKNOWN:
            violations.append(
                self._severity(
                    path,
                    rule_id="unknown_target_layer",
                    message=(
                        "model_deck import target has no known layer (fail closed): "
                        f"{target_module}"
                    ),
                    line=line,
                )
            )
            return violations

        allowed = ALLOWED_LAYER_IMPORTS.get(source_layer, frozenset())
        if target_layer not in allowed and target_layer is not Layer.UNKNOWN:
            violations.append(
                self._severity(
                    path,
                    rule_id="layer_import",
                    message=(
                        f"{source_module} ({source_layer.value}) cannot import "
                        f"{target_module} ({target_layer.value})"
                    ),
                    line=line,
                )
            )

        if source_layer is Layer.BOOTSTRAP:
            return violations

        private_names = tuple(name for name in imported_names if name.startswith("_"))
        effective_targets = [target_module]
        for name in private_names:
            if not target_module.endswith("." + name) and not target_module.split(".")[-1] == name:
                effective_targets.append(f"{target_module}.{name}")

        for effective in effective_targets:
            if is_private_module(effective) and source_layer not in {
                Layer.BOOTSTRAP,
                Layer.UNKNOWN,
            }:
                parent_prefix = source_module.rsplit(".", 1)[0] + "."
                if not effective.startswith(parent_prefix) and not effective.startswith(
                    source_module + "."
                ):
                    violations.append(
                        self._severity(
                            path,
                            rule_id="private_import",
                            message=f"cross-package private import is not allowed: {effective}",
                            line=line,
                        )
                    )

        if source_layer is Layer.ENGINE and target_layer is Layer.ENGINE:
            src_feature = engine_feature_root(source_module)
            tgt_feature = engine_feature_root(target_module)
            if src_feature and tgt_feature and src_feature != tgt_feature:
                if is_private_module(target_module) or "._" in target_module or private_names:
                    violations.append(
                        self._severity(
                            path,
                            rule_id="cross_feature_private",
                            message=(
                                "engine features must use public ports, not sibling private modules: "
                                f"{target_module}"
                            ),
                            line=line,
                        )
                    )

        if source_layer in {Layer.PROVIDERS, Layer.INTEGRATIONS, Layer.LEGACY_BASELINE}:
            leaf = target_module.split(".")[-1]
            if leaf in PROVIDER_ROUTER_FORBIDDEN or target_module in PROVIDER_ROUTER_FORBIDDEN:
                violations.append(
                    self._severity(
                        path,
                        rule_id="provider_router_back_import",
                        message=f"provider integration must not import router authority {target_module}",
                        line=line,
                    )
                )

        if source_layer is Layer.PLUGIN and target_module.startswith("model_deck.engine"):
            violations.append(
                self._severity(
                    path,
                    rule_id="plugin_private_engine",
                    message=f"plugins must not import engine internals: {target_module}",
                    line=line,
                )
            )

        if source_layer in {Layer.KERNEL, Layer.ENGINE} and target_module.startswith(
            "model_deck.adapters"
        ):
            violations.append(
                self._severity(
                    path,
                    rule_id="concrete_adapter_in_core",
                    message=f"core must depend on ports, not concrete adapters: {target_module}",
                    line=line,
                )
            )

        return violations

    def failing_violations(self, result: CheckResult) -> tuple[Violation, ...]:
        return tuple(v for v in result.violations if v.severity == "error")
