"""Build a fresh, isolated legacy-compatible app artifact; never install or launch it."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import ctypes
from dataclasses import dataclass
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import select
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from typing import Callable, Sequence

_TOOL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_TOOL_ROOT / "python" / "src"))
from model_deck_root_guard.paths import (  # noqa: E402
    reject_protected_path,
    reject_symlink_parent_aliases,
)
from model_deck_root_guard.roots import validate_isolated_roots  # noqa: E402


class StageError(ValueError):
    """A required packaging precondition or postcondition was not satisfied."""


@dataclass(frozen=True)
class StageInputs:
    source_root: Path
    scratch_root: Path
    state_root: Path
    artifact_root: Path
    name: str
    helper_snapshot: Path
    helper_sha256: str
    helper_source_hash_file: Path
    vendor_root: Path
    python_executable: Path
    signing_identity: str | None = None


CommandRunner = Callable[[Sequence[str], Path, dict[str, str]], str]
DirectoryPublisher = Callable[[int, str, str], None]

_COMMAND_GUARDIAN = """import os,signal,subprocess,sys
status_fd = int(sys.argv[1])
try:
    status = subprocess.run(sys.argv[2:], check=False).returncode
except OSError:
    status = 127
os.write(status_fd, (str(status) + '\\n').encode('ascii'))
os.close(status_fd)
while True:
    signal.pause()
"""


def run_command(argv: Sequence[str], cwd: Path, env: dict[str, str], *, timeout_seconds: float = 600) -> str:
    """No shell, network package installation, app launch or runtime discovery."""
    if sys.platform != "darwin":
        raise StageError("staged command supervision requires macOS")
    if not 0 < timeout_seconds <= 600:
        raise StageError("command deadline must be positive and at most 600 seconds")
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise StageError("safe child-group supervision requires default SIGCHLD handling")
    with tempfile.TemporaryFile(dir=cwd) as output:
        status_read, status_write = os.pipe()
        process = None
        returncode = None
        try:
            # The guardian remains alive after the command exits, reserving our
            # process group until cleanup. The command never inherits the status fd.
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", "-c", _COMMAND_GUARDIAN, str(status_write), *argv],
                cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True, pass_fds=(status_write,),
            )
            os.close(status_write)
            status_write = -1
            ready, _, _ = select.select([status_read], [], [], timeout_seconds)
            if not ready:
                raise subprocess.TimeoutExpired(argv, timeout_seconds)
            status = os.read(status_read, 64)
            if not re.fullmatch(rb"-?[0-9]{1,10}\n", status):
                raise StageError("command guardian did not report a valid exit status")
            returncode = int(status)
        finally:
            for descriptor in (status_read, status_write):
                if descriptor >= 0:
                    os.close(descriptor)
            if process is not None:
                # The live guardian owns this group. Never poll/wait before signal.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                finally:
                    process.wait(timeout=10)
        if returncode:
            raise StageError(f"command failed: {Path(argv[0]).name} ({returncode})")
        output.seek(0)
        content = output.read(131073)
        if len(content) > 131072:
            raise StageError("command output exceeded 128 KiB")
        return content.decode("utf-8").strip()


def publish_exclusive(parent_fd: int, source: str, destination: str) -> None:
    """macOS RENAME_EXCL; never use overwrite-capable rename as a fallback."""
    if sys.platform != "darwin":
        raise StageError("exclusive publication requires macOS")
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameatx_np", None)
    if rename is None:
        raise StageError("exclusive publication unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(source), parent_fd, os.fsencode(destination), 4):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def file_bytes(path: Path) -> bytes:
    """Reject aliases and special files rather than silently follow them."""
    reject_symlink_parent_aliases(path, label="package input")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise StageError("package input must be a regular, single-link file")
        return stream.read()


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_new(path: Path, content: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
    path.chmod(mode)


def copy_regular_tree(source: Path, destination: Path) -> None:
    reject_symlink_parent_aliases(source, label="package tree")
    if source.is_symlink() or not source.is_dir():
        raise StageError("package tree must be a real directory")
    destination.mkdir()
    for child in sorted(source.iterdir()):
        if child.name == "__pycache__" or child.suffix in (".pyc", ".pyo"):
            raise StageError("bytecode is forbidden in staged resources")
        if child.is_symlink():
            raise StageError("symlinks are forbidden in supplied package trees")
        if child.is_dir():
            copy_regular_tree(child, destination / child.name)
        else:
            write_new(destination / child.name, file_bytes(child))


def normalize_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_vendor(vendor_root: Path, source_root: Path, required_files: list[str]) -> dict:
    """Check supplied provenance and exact bytes without importing vendor code."""
    manifest = json.loads(file_bytes(vendor_root / "vendor-manifest.json"))
    if manifest.get("format_version") != 1 or not isinstance(manifest.get("provenance"), str) or not manifest["provenance"].strip():
        raise StageError("vendor manifest requires version 1 and explicit provenance")
    project = tomllib.loads(file_bytes(source_root / "python/pyproject.toml").decode())["project"]
    if manifest.get("requirements") != project["dependencies"]:
        raise StageError("vendor requirements differ from the source project's declared dependencies")
    expected = {normalize_distribution(project["name"]): project["version"]}
    for requirement in project["dependencies"]:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9_.+-]+)", requirement)
        if match is None:
            raise StageError("unsupported dependency requirement; explicit inventory review required")
        expected[normalize_distribution(match[1])] = match[2]
    distributions = {}
    for metadata_path in sorted(vendor_root.glob("*.dist-info/METADATA")):
        metadata = Parser().parsestr(file_bytes(metadata_path).decode("utf-8"))
        name, version = metadata.get("Name"), metadata.get("Version")
        if not name or not version or normalize_distribution(name) in distributions:
            raise StageError("invalid or duplicate vendor distribution metadata")
        if normalize_distribution(name) == normalize_distribution(project["name"]):
            declared = sorted(value.replace(" ", "") for value in metadata.get_all("Requires-Dist", []))
            required = sorted(value.replace(" ", "") for value in project["dependencies"])
            if declared != required:
                raise StageError("engine wheel dependency metadata differs from source")
        distributions[normalize_distribution(name)] = version
    if any(distributions.get(name) != version for name, version in expected.items()):
        raise StageError("offline vendor metadata does not match the engine and pinned dependencies")
    if manifest.get("distributions") != distributions:
        raise StageError("vendor distribution manifest differs from supplied metadata")
    actual_files = {}
    for path in sorted(vendor_root.rglob("*")):
        if path.is_symlink() or path.name == "__pycache__" or path.suffix in (".pyc", ".pyo"):
            raise StageError("vendor aliases and bytecode are forbidden")
        if not path.is_dir() and path != vendor_root / "vendor-manifest.json":
            actual_files[path.relative_to(vendor_root).as_posix()] = sha256(file_bytes(path))
    if actual_files != manifest.get("files"):
        raise StageError("vendor file inventory or SHA256 mismatch")
    for relative in required_files:
        if relative not in actual_files:
            raise StageError(f"required vendor file missing: {relative}")
    for package_name in ("model_deck", "model_deck_contracts", "model_deck_root_guard"):
        package_source = source_root / "python/src" / package_name
        if not package_source.is_dir() or package_source.is_symlink():
            raise StageError("source engine packages are missing")
        package_files = [path for path in package_source.rglob("*") if path.suffix in (".py", ".json")]
        if not package_files:
            raise StageError("source engine package inventory is empty")
        for path in package_files:
            relative = path.relative_to(source_root / "python/src").as_posix()
            if actual_files.get(relative) != sha256(file_bytes(path)):
                raise StageError(f"vendor engine code/resource differs from source: {relative}")
    return manifest


def validate_inputs(inputs: StageInputs) -> dict:
    source = inputs.source_root
    if not source.is_absolute() or source.is_symlink():
        raise StageError("source_root must be an explicit real absolute directory")
    reject_symlink_parent_aliases(source, label="source_root")
    for required in ("build.sh", "macos/Package.swift", "OpenRouterCredentialHelper.swift"):
        file_bytes(source / required)
    validate_isolated_roots(inputs.state_root, inputs.artifact_root, inputs.scratch_root, source_root=source)
    for root in (inputs.scratch_root, inputs.state_root, inputs.artifact_root):
        if not root.is_dir():
            raise StageError("supply existing isolated root directories")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", inputs.name):
        raise StageError("output name must be a simple fresh child name")
    if os.path.lexists(inputs.artifact_root / inputs.name):
        raise StageError("output already exists")
    for supplied in (inputs.helper_snapshot, inputs.helper_source_hash_file, inputs.vendor_root):
        if not supplied.is_absolute():
            raise StageError("snapshot and vendor inputs must be absolute")
        reject_protected_path(supplied, label="supplied package input", source_root=source)
        if supplied.is_symlink():
            raise StageError("supplied package input must not be a symlink")
        for root in (inputs.scratch_root, inputs.state_root, inputs.artifact_root):
            if supplied.resolve() == root.resolve():
                raise StageError("package input must not be an output root")
    if not inputs.python_executable.is_absolute() or not inputs.python_executable.is_file():
        raise StageError("supply an absolute existing Python executable")
    if inputs.signing_identity is not None and not inputs.signing_identity.strip():
        raise StageError("signing identity must be explicit and nonempty")
    expected_source_hash = file_bytes(inputs.helper_source_hash_file).decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", inputs.helper_sha256):
        raise StageError("expected helper SHA256 must be lowercase hexadecimal")
    if sha256(file_bytes(inputs.helper_snapshot)) != inputs.helper_sha256:
        raise StageError("helper snapshot SHA256 mismatch")
    if sha256(file_bytes(source / "OpenRouterCredentialHelper.swift")) != expected_source_hash:
        raise StageError("helper source hash mismatch; supply a matching qualified snapshot")
    inventory = json.loads(file_bytes(Path(__file__).with_name("inventory.json")))
    validate_vendor(inputs.vendor_root, source, inventory["vendor"]["required_files"])
    return inventory


def command_environment(state: Path) -> dict[str, str]:
    # No inherited provider credentials, signing identity or Python import path.
    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("LANG", "LC_ALL", "LC_CTYPE"):
        if name in os.environ:
            environment[name] = os.environ[name]
    cache = state / "cache"
    cache.mkdir()
    temporary = state / "tmp"
    temporary.mkdir()
    environment["TMPDIR"] = str(temporary)
    environment["CLANG_MODULE_CACHE_PATH"] = str(cache / "clang")
    environment["SWIFTPM_MODULECACHE_OVERRIDE"] = str(cache / "swift")
    return environment


def copy_swift_bundles(build_root: Path, binary_directory: Path, app: Path, bundles: dict) -> None:
    for target, bundle_name in bundles.items():
        candidates = [
            path for path in build_root.rglob("resource_bundle_accessor.swift")
            if "release" in path.parts and path.parent.parent.name == f"{target}.build"
        ]
        if len(candidates) != 1:
            raise StageError("unsupported Swift release accessor inventory")
        accessor = file_bytes(candidates[0]).decode("utf-8")
        expected = f'let mainPath = Bundle.main.bundleURL.appendingPathComponent("{bundle_name}").path'
        if expected not in accessor or "let preferredBundle = Bundle(path: mainPath)" not in accessor:
            raise StageError("unsupported Swift resource lookup; review release accessor layout")
        copy_regular_tree(binary_directory / bundle_name, app / bundle_name)


def artifact_inventory(root: Path) -> list[dict]:
    records = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            if relative != "Model Deck.app/Contents/MacOS/OpenRouterSettings" or os.readlink(path) != "ModelDeck":
                raise StageError("unexpected artifact symlink")
            records.append({"path": relative, "symlink": "ModelDeck"})
        elif path.is_file():
            if path.suffix in (".pyc", ".pyo") or "__pycache__" in path.parts:
                raise StageError("bytecode is forbidden in staged resources")
            records.append({"path": relative, "sha256": sha256(file_bytes(path)), "size": path.stat().st_size})
        elif not path.is_dir():
            raise StageError("unexpected special file in artifact")
    return records


@contextmanager
def owned_temporary_directory(parent: Path, prefix: str):
    """Clean only our original child, addressed through its retained parent fd."""
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    child = None
    original = None
    try:
        child = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
        original = child.stat()
        yield child
    finally:
        try:
            if child is not None and original is not None:
                try:
                    current = os.stat(child.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                if current is not None and (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                    shutil.rmtree(child.name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)


def stage_app(
    inputs: StageInputs, *, command: CommandRunner = run_command,
    publisher: DirectoryPublisher = publish_exclusive,
) -> Path:
    inventory = validate_inputs(inputs)
    # Only these uniquely created children are eligible for cleanup.
    with ExitStack() as cleanup:
        scratch = cleanup.enter_context(owned_temporary_directory(inputs.scratch_root, "stage-build-"))
        state = cleanup.enter_context(owned_temporary_directory(inputs.state_root, "stage-state-"))
        output = cleanup.enter_context(owned_temporary_directory(inputs.artifact_root, ".stage-output-"))
        artifact_identity = inputs.artifact_root.stat()
        environment = command_environment(state)
        python = str(inputs.python_executable)
        command([python, "-I", "-B", "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"], scratch, environment)
        build_root = scratch / "swift"
        swift = ["/usr/bin/swift", "build", "--package-path", str(inputs.source_root / "macos"),
                 "--scratch-path", str(build_root), "--cache-path", str(state / "swift-cache"),
                 "--config-path", str(state / "swift-config"), "--security-path", str(state / "swift-security"),
                 "--disable-automatic-resolution", "-c", "release"]
        command([*swift, "--product", "ModelDeck"], scratch, environment)
        binary_directory = Path(command([*swift, "--show-bin-path"], scratch, environment))
        if not binary_directory.is_absolute() or not binary_directory.resolve().is_relative_to(build_root.resolve()):
            raise StageError("Swift binary output escaped owned scratch")
        if "release" not in binary_directory.parts:
            raise StageError("Swift output is not a release layout")
        app = output / inventory["app_name"]
        resources = app / "Contents/Resources"
        resources.mkdir(parents=True)
        write_new(app / "Contents/MacOS/ModelDeck", file_bytes(binary_directory / "ModelDeck"), 0o755)
        (app / "Contents/MacOS/OpenRouterSettings").symlink_to("ModelDeck")
        copy_swift_bundles(build_root, binary_directory, app, inventory["swift_bundles"])
        for relative in [*inventory["resources"], *inventory["icons"]]:
            source = inputs.source_root / relative
            write_new(resources / source.name, file_bytes(source), 0o755 if source.name == "CodexProviderBridge" else 0o644)
        copy_regular_tree(inputs.vendor_root, resources / "vendor")
        vendor_manifest = validate_vendor(resources / "vendor", inputs.source_root, inventory["vendor"]["required_files"])
        helper = app / "Contents/Helpers/OpenRouterCredentialHelper"
        write_new(helper, file_bytes(inputs.helper_snapshot), 0o755)
        write_new(resources / "OpenRouterCredentialHelper.source-sha256", file_bytes(inputs.helper_source_hash_file))
        if sha256(file_bytes(helper)) != inputs.helper_sha256:
            raise StageError("helper changed while staging")
        command(["/usr/bin/codesign", "--verify", "--strict", str(helper)], scratch, environment)
        plist = {**inventory["info_plist"], "PythonExecutable": python}
        write_new(app / "Contents/Info.plist", plistlib.dumps(plist))
        if inputs.signing_identity is not None:
            command(["/usr/bin/codesign", "--force", "--sign", inputs.signing_identity,
                     "--timestamp=none", str(app)], scratch, environment)
            command(["/usr/bin/codesign", "--verify", "--strict", str(app)], scratch, environment)
        if sha256(file_bytes(helper)) != inputs.helper_sha256:
            raise StageError("signing changed helper bytes")
        records = artifact_inventory(output)
        report = {
            "format_version": 1, "scope": "legacy-compatible staged app; not B26 acceptance",
            "source_root": str(inputs.source_root), "python_executable": python,
            "helper_sha256": inputs.helper_sha256, "helper_signature": "verified",
            "signing_identity": inputs.signing_identity, "app_signed": inputs.signing_identity is not None,
            "vendor_input": str(inputs.vendor_root), "vendor_manifest": vendor_manifest,
            "files": records,
            "limitations": ["not launched", "SDK not packaged", "dependency closure not runtime tested", "no installed-app or provider qualification"],
        }
        write_new(output / "inventory.json", (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
        parent_fd = os.open(inputs.artifact_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            current = os.fstat(parent_fd)
            if (current.st_dev, current.st_ino) != (artifact_identity.st_dev, artifact_identity.st_ino):
                raise StageError("artifact root changed before publication")
            publisher(parent_fd, output.name, inputs.name)
        finally:
            os.close(parent_fd)
        return inputs.artifact_root / inputs.name


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-root", "scratch-root", "state-root", "artifact-root", "helper-snapshot",
                 "helper-source-hash-file", "vendor-root", "python-executable"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--helper-sha256", required=True)
    parser.add_argument("--signing-identity", help="explicit certificate identity or '-' for ad hoc; omitted means unsigned")
    arguments = parser.parse_args(argv)
    try:
        destination = stage_app(StageInputs(**vars(arguments)))
    except (StageError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"stage: {error}", file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
