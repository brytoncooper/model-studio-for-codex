from __future__ import annotations

import re
from pathlib import Path

from development.guard.paths import repo_root

_TEST_METHOD = re.compile(r"^\s+def test_", re.MULTILINE)


def python_unittest_inventory(source_root: Path) -> dict[str, int]:
    root = source_root.resolve()
    counts: dict[str, int] = {}
    for path in sorted(root.glob("test_*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        counts[path.stem] = len(_TEST_METHOD.findall(text))
    return counts


def swift_self_test_modes() -> tuple[str, ...]:
    return (
        "--self-test-keychain",
        "--self-test-companion",
        "--self-test-model-browser",
        "--self-test-usage",
    )


def baseline_record(source_root: Path) -> dict[str, object]:
    inventory = python_unittest_inventory(source_root)
    return {
        "python_test_modules": len(inventory),
        "python_test_methods": sum(inventory.values()),
        "python_modules": inventory,
        "swift_self_test_modes": list(swift_self_test_modes()),
    }
