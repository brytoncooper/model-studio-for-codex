from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ImportRecord:
    module: str | None
    names: tuple[str, ...]
    line: int
    is_relative: bool
    relative_level: int
    kind: str


@dataclass(frozen=True)
class DynamicImportRecord:
    module_expr: str
    line: int


@dataclass(frozen=True)
class ServiceLocatorRecord:
    callee: str
    line: int


def parse_source(source: str, *, path: str = "<unknown>") -> ast.Module:
    return ast.parse(source, filename=path)


def collect_imports(tree: ast.Module) -> list[ImportRecord]:
    records: list[ImportRecord] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                records.append(
                    ImportRecord(
                        module=alias.name,
                        names=(alias.asname or alias.name,),
                        line=node.lineno,
                        is_relative=False,
                        relative_level=0,
                        kind="import",
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            names = tuple(a.name for a in node.names)
            records.append(
                ImportRecord(
                    module=module,
                    names=names,
                    line=node.lineno,
                    is_relative=node.level > 0,
                    relative_level=node.level,
                    kind="from",
                )
            )
    return records


def _expr_label(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("*")
        return "".join(parts)
    return ast.unparse(node)


def _importlib_module_aliases(tree: ast.Module) -> frozenset[str]:
    aliases: set[str] = {"importlib"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root != "importlib":
                    continue
                bound = alias.asname or alias.name.split(".")[0]
                aliases.add(bound)
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if node.module.split(".", 1)[0] != "importlib":
                continue
            for alias in node.names:
                if alias.name in {"import_module", "__import__"}:
                    aliases.add(alias.asname or alias.name)
    return frozenset(aliases)



def collect_dynamic_imports(tree: ast.Module) -> list[DynamicImportRecord]:
    importlib_aliases = _importlib_module_aliases(tree)
    from_importlib_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".", 1)[0] == "importlib":
                for alias in node.names:
                    if alias.name in {"import_module", "__import__"}:
                        from_importlib_names.add(alias.asname or alias.name)
    records: list[DynamicImportRecord] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_dynamic = False
        if isinstance(func, ast.Name):
            if func.id == "__import__":
                is_dynamic = True
            elif func.id in from_importlib_names:
                is_dynamic = True
        elif isinstance(func, ast.Attribute):
            if func.attr in {"import_module", "__import__"}:
                if isinstance(func.value, ast.Name) and func.value.id in importlib_aliases:
                    is_dynamic = True
                elif ast.unparse(func) in {"importlib.import_module", "importlib.__import__"}:
                    is_dynamic = True
        if is_dynamic and node.args:
            records.append(DynamicImportRecord(_expr_label(node.args[0]), node.lineno))
    return records


SERVICE_LOCATOR_CALLEES = frozenset(
    {
        "get_service",
        "resolve_service",
        "service_locator",
        "get_implementation",
        "resolve_implementation",
    }
)


def collect_service_locator_calls(tree: ast.Module) -> list[ServiceLocatorRecord]:
    records: list[ServiceLocatorRecord] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in SERVICE_LOCATOR_CALLEES:
            records.append(ServiceLocatorRecord(func.id, node.lineno))
        elif isinstance(func, ast.Attribute) and func.attr in SERVICE_LOCATOR_CALLEES:
            records.append(ServiceLocatorRecord(ast.unparse(func), node.lineno))
    return records


def read_module_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")
