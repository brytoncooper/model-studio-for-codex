from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LegacyBaselineEntry:
    relpath: str
    enforce: bool
    note: str = ""


DEFAULT_BASELINE_PATH = Path(__file__).resolve().parent / "baseline_legacy.json"


def load_baseline(path: Path | None = None) -> tuple[LegacyBaselineEntry, ...]:
    target = path or DEFAULT_BASELINE_PATH
    if not target.is_file():
        return ()
    payload = json.loads(target.read_text(encoding="utf-8"))
    entries: list[LegacyBaselineEntry] = []
    for item in payload.get("files", []):
        entries.append(
            LegacyBaselineEntry(
                relpath=str(item["relpath"]),
                enforce=bool(item.get("enforce", False)),
                note=str(item.get("note", "")),
            )
        )
    return tuple(entries)


def baseline_index(entries: tuple[LegacyBaselineEntry, ...]) -> dict[str, LegacyBaselineEntry]:
    return {entry.relpath: entry for entry in entries}
