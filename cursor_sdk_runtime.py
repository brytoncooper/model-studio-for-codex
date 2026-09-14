"""Compatibility name for the package-owned Cursor SDK runtime."""
from __future__ import annotations

import sys
from pathlib import Path


try:
    from model_deck.integrations.providers.cursor import sdk_runtime as _runtime
except ModuleNotFoundError as error:
    if error.name != "model_deck":
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parent / "python" / "src"))
    from model_deck.integrations.providers.cursor import sdk_runtime as _runtime


if __name__ == "__main__":
    raise SystemExit(_runtime.main())

sys.modules[__name__] = _runtime
