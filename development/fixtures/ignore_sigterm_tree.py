#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _ignore_signal(_signum, _frame) -> None:
    return None


signal.signal(signal.SIGTERM, _ignore_signal)
signal.signal(signal.SIGINT, _ignore_signal)


def main() -> int:
    marker = Path(sys.argv[1])
    role = sys.argv[2] if len(sys.argv) > 2 else "root"
    with marker.open("a", encoding="utf-8") as handle:
        handle.write(f"{role}:{os.getpid()}\n")
    if role == "root":
        subprocess.Popen(
            [sys.executable, __file__, str(marker), "child"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    elif role == "child":
        subprocess.Popen(
            [sys.executable, __file__, str(marker), "leaf"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    time.sleep(3600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
