from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json


@dataclass(frozen=True)
class DynamicImportAllowlist:
    module_patterns: frozenset[str]
    file_globs: frozenset[str]


DEFAULT_ALLOWLIST_PATH = Path(__file__).resolve().parent / "dynamic_import_allowlist.json"


def load_dynamic_allowlist(path: Path | None = None) -> DynamicImportAllowlist:
    target = path or DEFAULT_ALLOWLIST_PATH
    if not target.is_file():
        return DynamicImportAllowlist(frozenset(), frozenset())
    payload = json.loads(target.read_text(encoding="utf-8"))
    return DynamicImportAllowlist(
        module_patterns=frozenset(payload.get("module_patterns", [])),
        file_globs=frozenset(payload.get("file_globs", [])),
    )


def dynamic_import_allowed(
    *,
    file_relpath: str,
    module_expr: str,
    allowlist: DynamicImportAllowlist,
) -> bool:
    if module_expr in allowlist.module_patterns:
        return True
    for pattern in allowlist.file_globs:
        if file_relpath.endswith(pattern) or file_relpath == pattern:
            return True
    return False
