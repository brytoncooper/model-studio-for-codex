"""B10 Codex runtime discovery, compatibility, and launch preparation.

This module owns the seam between Model Deck and the installed Codex desktop
application on macOS. It performs three orthogonal jobs:

* ``discover_runtime`` — locate a supported Codex desktop bundle inside an
  injected Applications directory and return a typed
  :class:`CodexRuntimeDescriptor`. The bundle preference order is
  ChatGPT.app, then Codex.app; only ``CFBundleIdentifier == "com.openai.codex"``
  bundles with an executable ``Contents/Resources/codex`` are accepted. The
  discovered descriptor also carries ``CFBundleShortVersionString`` as
  descriptive metadata only — it is **not** the protocol version and is
  never used for compatibility classification.
* ``classify_compatibility`` — classify the discovered bundle against a
  caller-supplied app-server protocol version and a caller-supplied set of
  supported protocol versions. Pure; never reads environment or files; the
  macOS bundle version plays no role in classification.
* ``capability_status`` / ``prepare_launch`` — combine compatibility with an
  injected :class:`CodexProcessProbe` to decide whether the runtime is
  available, already-running-unverified, or incompatible, and assemble a
  pure :class:`CodexLaunchPreparation` (no global config IO).

The module never mutates ``~/.codex`` or any other global configuration, never
spawns processes, and never inspects the host ``/Applications`` directory
unless the caller explicitly passes ``applications_dir=None``.
"""

from __future__ import annotations

import os
import plistlib
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, Sequence, runtime_checkable
from xml.parsers.expat import ExpatError

__all__ = [
    "SUPPORTED_BUNDLE_IDENTIFIER",
    "EXECUTABLE_RELATIVE_PATH",
    "BUNDLE_NAME_PREFERENCE",
    "APP_SERVER_ARGUMENT",
    "CodexRuntimeError",
    "CodexProcessProbe",
    "CodexRuntimeDescriptor",
    "CodexCompatibility",
    "CodexLaunchPreparation",
    "discover_runtime",
    "classify_compatibility",
    "capability_status",
    "prepare_launch",
]

SUPPORTED_BUNDLE_IDENTIFIER: str = "com.openai.codex"
EXECUTABLE_RELATIVE_PATH: str = "Contents/Resources/codex"
BUNDLE_NAME_PREFERENCE: tuple[str, ...] = ("ChatGPT.app", "Codex.app")
APP_SERVER_ARGUMENT: str = "app-server"

DEFAULT_APPLICATIONS_DIR: Path = Path("/Applications")


class CodexRuntimeError(Exception):
    """Raised when Codex runtime discovery or launch preparation refuses.

    Used for every recoverable caller-visible failure: no supported bundle
    found, observed protocol version unknown or unsupported, or an
    already-running Codex process refusing to be relaunched.
    """


@runtime_checkable
class CodexProcessProbe(Protocol):
    """Caller-supplied process probe for "is Codex currently running?".

    The runtime adapter is a pure adapter; it never inspects the host
    process table itself. The engine (or tests) inject a probe that owns
    the actual detection logic.
    """

    def is_codex_running(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class CodexRuntimeDescriptor:
    """One supported Codex desktop bundle discovered on the host.

    ``bundle_short_version`` carries the macOS
    ``CFBundleShortVersionString`` as descriptive metadata only — it may be
    ``None`` if the bundle omits the key, and it is **never** used by
    :func:`classify_compatibility`. The app-server protocol version is
    observed separately and passed into the classifier.
    """

    application_path: Path
    executable_path: Path
    bundle_identifier: str
    bundle_short_version: str | None


@dataclass(frozen=True, slots=True)
class CodexCompatibility:
    """Observed app-server protocol version vs. supported set.

    ``status`` is one of:

    * ``"supported"`` — the observed version is in the caller's supported
      set; ``reason`` is ``None``.
    * ``"unsupported"`` — the observed version was readable but is not in
      the supported set; ``reason`` names both.
    * ``"unknown"`` — the observed version could not be determined (e.g.
      the probe failed or was not invoked); ``reason`` explains why.

    ``protocol_version`` echoes the observed version (or ``None`` when
    unknown) and is independent from the bundle's
    ``CFBundleShortVersionString``.
    """

    status: Literal["supported", "unsupported", "unknown"]
    protocol_version: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class CodexLaunchPreparation:
    """Assembled argv for one Codex app-server launch.

    ``argv`` always begins with ``str(executable)`` followed by
    ``APP_SERVER_ARGUMENT`` so callers can pass the result directly to
    ``subprocess.Popen`` / ``os.execv`` without re-prefixing the executable.
    """

    executable: Path
    argv: tuple[str, ...]
    overrides: tuple[str, ...]


def _read_bundle_metadata(application: Path) -> dict[str, object] | None:
    """Read ``Contents/Info.plist`` defensively; return ``None`` on any failure."""
    info_plist = application / "Contents/Info.plist"
    try:
        with info_plist.open("rb") as source:
            metadata = plistlib.load(source)
    except (OSError, ValueError, plistlib.InvalidFileException, ExpatError):
        return None
    if not isinstance(metadata, dict):
        return None
    return metadata


def _bundle_short_version(metadata: dict[str, object]) -> str | None:
    raw = metadata.get("CFBundleShortVersionString")
    if isinstance(raw, str) and raw:
        return raw
    return None


def _executable_is_invokable(executable: Path) -> bool:
    return executable.is_file() and os.access(executable, os.X_OK)


def discover_runtime(
    applications_dir: Path | None = None,
) -> CodexRuntimeDescriptor:
    """Locate the installed Codex desktop runtime.

    Walks ``applications_dir`` (default ``/Applications``) in
    :data:`BUNDLE_NAME_PREFERENCE` order, accepting the first bundle whose
    ``Contents/Info.plist`` parses, reports
    ``CFBundleIdentifier == "com.openai.codex"``, and whose
    ``Contents/Resources/codex`` is a file with the executable bit set.
    The bundle's ``CFBundleShortVersionString`` (if present) is captured on
    the returned descriptor as descriptive metadata; classification must not
    rely on it.

    Raises:
        CodexRuntimeError: when no supported bundle exists in the searched
            directory. The function never inspects the host Applications
            directory unless ``applications_dir`` is ``None``.
    """
    root = applications_dir if applications_dir is not None else DEFAULT_APPLICATIONS_DIR
    applications = Path(root)
    for name in BUNDLE_NAME_PREFERENCE:
        application = applications / name
        metadata = _read_bundle_metadata(application)
        if metadata is None:
            continue
        if metadata.get("CFBundleIdentifier") != SUPPORTED_BUNDLE_IDENTIFIER:
            continue
        executable = application / EXECUTABLE_RELATIVE_PATH
        if not _executable_is_invokable(executable):
            continue
        return CodexRuntimeDescriptor(
            application_path=application,
            executable_path=executable,
            bundle_identifier=SUPPORTED_BUNDLE_IDENTIFIER,
            bundle_short_version=_bundle_short_version(metadata),
        )
    raise CodexRuntimeError(
        "No supported Codex desktop runtime was found in Applications.",
    )


def classify_compatibility(
    descriptor: CodexRuntimeDescriptor,
    *,
    observed_protocol_version: str | None,
    supported_protocol_versions: AbstractSet[str],
) -> CodexCompatibility:
    """Compare an observed app-server protocol version with a supported set.

    The macOS bundle's ``CFBundleShortVersionString`` is **not** part of
    this contract; it is descriptive metadata only. The caller supplies the
    observed protocol version (typically obtained from a probe or handshake
    with the running app-server) and the set of protocol versions Model
    Deck can speak. A single-element set expresses "require exact protocol".

    Rules:

    * ``observed_protocol_version is None`` → ``"unknown"``.
    * ``observed_protocol_version in supported_protocol_versions`` →
      ``"supported"``; ``reason`` is ``None``.
    * otherwise → ``"unsupported"``; ``reason`` names both.

    ``descriptor`` is accepted so future revisions can correlate bundle
    metadata with classification without changing this signature; today
    only ``observed_protocol_version`` and ``supported_protocol_versions``
    drive the outcome.
    """
    del descriptor  # not used today; reserved for future correlation
    if observed_protocol_version is None:
        return CodexCompatibility(
            status="unknown",
            protocol_version=None,
            reason="observed app-server protocol version is not known",
        )
    if observed_protocol_version in supported_protocol_versions:
        return CodexCompatibility(
            status="supported",
            protocol_version=observed_protocol_version,
            reason=None,
        )
    return CodexCompatibility(
        status="unsupported",
        protocol_version=observed_protocol_version,
        reason=(
            f"observed app-server protocol version {observed_protocol_version!r} "
            "is not in the supported set "
            f"{sorted(supported_protocol_versions)!r}"
        ),
    )


def _assert_probe_satisfies(process_probe: object) -> CodexProcessProbe:
    if not isinstance(process_probe, CodexProcessProbe):
        raise TypeError(
            "process_probe must satisfy CodexProcessProbe "
            "(missing is_codex_running)",
        )
    return process_probe


def capability_status(
    descriptor: CodexRuntimeDescriptor,
    *,
    process_probe: CodexProcessProbe,
    compatibility: CodexCompatibility,
) -> Literal["available", "already-running-unverified", "incompatible"]:
    """Decide whether the runtime can be launched right now.

    When ``compatibility.status`` is ``unsupported`` or ``unknown`` the
    capability is ``"incompatible"`` and ``process_probe`` is **not**
    consulted: callers and tests rely on the probe not being a side
    effect source.

    When compatibility is ``supported``, the probe decides between
    ``"available"`` (idle) and ``"already-running-unverified"``
    (Codex already up; the adapter does not verify whether the running
    instance speaks our protocol).

    ``descriptor`` is accepted for signature symmetry with
    :func:`prepare_launch` and to give future revisions a place to read
    bundle metadata without changing this signature.
    """
    _assert_probe_satisfies(process_probe)
    del descriptor  # not used today; reserved for future correlation
    if compatibility.status != "supported":
        return "incompatible"
    return "already-running-unverified" if process_probe.is_codex_running() else "available"


def prepare_launch(
    descriptor: CodexRuntimeDescriptor,
    *,
    argv: Sequence[str] = (),
    overrides: Sequence[str] = (),
    compatibility: CodexCompatibility,
    process_probe: CodexProcessProbe,
) -> CodexLaunchPreparation:
    """Assemble one Codex app-server launch argv without IO.

    Refuses (raises :class:`CodexRuntimeError`) when:

    * ``compatibility.status == "unknown"`` — observed protocol version
      could not be established; ``process_probe`` is **not** called.
    * ``compatibility.status == "unsupported"`` — observed protocol
      version disagrees with the supported set; ``process_probe`` is
      **not** called.
    * ``process_probe.is_codex_running() is True`` — Codex is already up;
      a second ``app-server`` would race the existing instance.

    On success returns a :class:`CodexLaunchPreparation` whose ``argv`` is
    ``(str(executable), APP_SERVER_ARGUMENT, *argv, *overrides)`` and
    whose ``overrides`` echoes the caller-provided overrides tuple. This
    function performs no filesystem or environment reads/writes; the
    adapter never consults or mutates global Codex configuration.
    """
    _assert_probe_satisfies(process_probe)
    if compatibility.status == "unknown":
        raise CodexRuntimeError(
            "Refusing to prepare Codex launch: "
            f"{compatibility.reason or 'observed protocol version is unknown'}",
        )
    if compatibility.status == "unsupported":
        raise CodexRuntimeError(
            "Refusing to prepare Codex launch: "
            f"{compatibility.reason or 'observed protocol version is unsupported'}",
        )
    if process_probe.is_codex_running():
        raise CodexRuntimeError(
            "Refusing to prepare Codex launch: Codex is already running "
            "(already-running-unverified).",
        )
    executable = descriptor.executable_path
    argv_tuple: tuple[str, ...] = (str(executable), APP_SERVER_ARGUMENT, *argv, *overrides)
    overrides_tuple: tuple[str, ...] = tuple(overrides)
    return CodexLaunchPreparation(
        executable=executable,
        argv=argv_tuple,
        overrides=overrides_tuple,
    )
