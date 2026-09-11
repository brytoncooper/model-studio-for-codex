"""Locate the installed Codex desktop runtime without shell-path discovery."""
import os
from pathlib import Path
import plistlib
from xml.parsers.expat import ExpatError


def discover_runtime(applications_dir=None):
    applications = Path(applications_dir) if applications_dir is not None else Path("/Applications")
    for name in ("ChatGPT.app", "Codex.app"):
        application = applications / name
        executable = application / "Contents/Resources/codex"
        try:
            with (application / "Contents/Info.plist").open("rb") as source:
                metadata = plistlib.load(source)
            if (not isinstance(metadata, dict) or metadata.get("CFBundleIdentifier") != "com.openai.codex"
                    or not executable.is_file() or not os.access(executable, os.X_OK)):
                continue
        except (OSError, ValueError, plistlib.InvalidFileException, ExpatError):
            continue
        return {"application_path": str(application), "executable_path": str(executable),
                "bundle_identifier": "com.openai.codex"}
    raise RuntimeError("No supported Codex desktop runtime was found in Applications.")
