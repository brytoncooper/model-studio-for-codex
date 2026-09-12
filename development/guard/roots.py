import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYTHON_SRC = str(_REPO_ROOT / "python" / "src")
if _PYTHON_SRC not in sys.path:
    sys.path.insert(0, _PYTHON_SRC)

from model_deck_root_guard.roots import validate_isolated_roots  # noqa: E402

__all__ = ["validate_isolated_roots"]
