"""Preconditions shared by every Model Deck Python test package.

Process-isolation fixtures spawn ``sys.executable -I -c ...`` children that must
not be able to import ``model_deck``; an editable install breaks that proof.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ISOLATION_REMEDIATION = (
    "uv pip uninstall --python python/.venv/bin/python model-deck"
)

_PROBE = (
    "import importlib.util; "
    "print('present' if importlib.util.find_spec('model_deck') else 'absent')"
)


def _require_engine_absent_under_isolated_imports() -> None:
    probe = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.stdout.strip() != "absent":
        raise RuntimeError(
            "model_deck is importable under `python -I`, so the process-isolation "
            "fixtures cannot prove that external code runs without private engine "
            "imports. Remove the editable install and run the tests with "
            f"PYTHONPATH=src instead: {ISOLATION_REMEDIATION}"
        )


def _use_symlink_free_temporary_directory() -> None:
    resolved = os.path.realpath(tempfile.gettempdir())
    tempfile.tempdir = resolved
    os.environ["TMPDIR"] = resolved


_require_engine_absent_under_isolated_imports()
_use_symlink_free_temporary_directory()
