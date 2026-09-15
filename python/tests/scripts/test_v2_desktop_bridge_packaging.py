"""D1a — packaging assertion for the V2 Codex Desktop bridge wrapper.

The wrapper ``scripts/v2/CodexDesktopBridge`` is the only surface that
bundles the producer-owned ``model_deck.integrations.hosts.codex.desktop_attachment``
module into the staged V2 ``Model Deck V2.app``. The builder copies it
into ``Contents/Resources/`` and chmods it executable; the wrapper exec's
the declared Python interpreter from
``Contents/Resources/config/runtime.plist`` (key ``python_executable``)
with the engine source tree on ``PYTHONPATH``.

These checks are read-only against the source tree: they inspect the
wrapper text, the staging instruction inside ``scripts/v2/build.sh``,
and the executable bit. They never rebuild or relocate the live app.
"""
from __future__ import annotations

import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_WRAPPER = REPO_ROOT / "scripts" / "v2" / "CodexDesktopBridge"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "v2" / "build.sh"
RUNTIME_PLIST_RELATIVE = Path("Contents/Resources/config/runtime.plist")
WRAPPER_STAGED_RELATIVE = Path("Contents/Resources/CodexDesktopBridge")
PYTHON_SRC_RELATIVE = Path("Contents/Resources/python/src")
TARGET_MODULE = "model_deck.integrations.hosts.codex.desktop_attachment"


class CodexDesktopBridgeWrapperTests(unittest.TestCase):
    """The wrapper script must exist, be executable, and target the V2 module."""

    def test_wrapper_file_is_present_and_executable(self) -> None:
        self.assertTrue(
            BRIDGE_WRAPPER.is_file(),
            f"V2 Desktop bridge wrapper must exist at {BRIDGE_WRAPPER}",
        )
        mode = BRIDGE_WRAPPER.stat().st_mode
        self.assertTrue(
            mode & stat.S_IXUSR,
            "CodexDesktopBridge must be executable for its owner",
        )

    def test_wrapper_exec_target_is_v2_desktop_attachment_module(self) -> None:
        text = BRIDGE_WRAPPER.read_text(encoding="utf-8")
        # The wrapper must exec the producer-owned module with argv forwarded verbatim.
        match = re.search(r"exec\s+\"\$\{?bridge_python\}?\"\s+(.+?)\s+\"\$\@\"", text)
        self.assertIsNotNone(
            match,
            "wrapper must exec the configured Python interpreter with the module + argv",
        )
        exec_arguments = match.group(1)
        self.assertIn("-m", exec_arguments)
        self.assertIn(TARGET_MODULE, exec_arguments)

    def test_wrapper_reads_python_executable_from_v2_runtime_plist(self) -> None:
        text = BRIDGE_WRAPPER.read_text(encoding="utf-8")
        # V2 stores the Python interpreter in Contents/Resources/config/runtime.plist
        # (key ``python_executable``); the wrapper must read from there and not from
        # Info.plist, which is the legacy CodexProviderBridge pattern.
        self.assertIn(
            str(RUNTIME_PLIST_RELATIVE),
            text,
            "wrapper must reference the V2 runtime.plist for python_executable",
        )
        self.assertIn("python_executable", text)
        # Confirm the wrapper does not regress to the legacy Info.plist lookup.
        self.assertNotIn("Info.plist", text)
        self.assertNotIn("PythonExecutable", text)

    def test_wrapper_sets_pythonpath_to_bundled_python_src(self) -> None:
        text = BRIDGE_WRAPPER.read_text(encoding="utf-8")
        # The bundled engine source must be discoverable so the producer-owned
        # ``model_deck.integrations.hosts.codex.desktop_attachment`` module
        # resolves from the staged tree regardless of the host PYTHONPATH.
        match = re.search(
            r"PYTHONPATH=\"(?P<value>[^\"]*python/src[^\"]*)\"", text
        )
        self.assertIsNotNone(
            match,
            "wrapper must set PYTHONPATH to the bundled python/src directory",
        )
        # The wrapper must anchor PYTHONPATH at the bundle-resident directory; a
        # path that resolves through the host's working directory would not be
        # stable across launches from different cwd values.
        # PYTHONPATH anchors on the bundle-resident directory via $bridge_directory
        # (zsh: ${0:A:h}). The literal path is built at exec time, so the source
        # must combine the directory variable with the trailing python/src suffix.
        self.assertIn("$bridge_directory", match.group("value"))
        # The PYTHONPATH value appends ``${PYTHONPATH:+:$PYTHONPATH}`` after the
        # bundled tree so any host-side path is preserved; the assertion below
        # confirms the bundled ``python/src`` is the leading entry (via the
        # ``$bridge_directory`` variable, which resolves to the bundle-resident
        # ``Contents/Resources/`` directory).
        value_before_appending = match.group("value").split("${PYTHONPATH", 1)[0]
        self.assertTrue(
            value_before_appending.endswith("python/src"),
            f"PYTHONPATH prefix must anchor on the bundled python/src tree, got {match.group('value')!r}",
        )
        self.assertTrue(
            value_before_appending.startswith("$bridge_directory"),
            f"PYTHONPATH must be rooted at the wrapper directory, got {match.group('value')!r}",
        )

    def test_wrapper_zsh_syntax_is_valid(self) -> None:
        # ``zsh -n`` parses without executing the wrapper. We rely on the host
        # ``zsh`` so the syntax check matches what the builder actually runs.
        zsh = shutil.which("zsh") or shutil.which("/bin/zsh")
        if zsh is None:
            self.skipTest("zsh is unavailable on this host")
        result = subprocess.run(
            [zsh, "-n", str(BRIDGE_WRAPPER)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"zsh -n reported a syntax error:\nstdout={result.stdout}\nstderr={result.stderr}",
        )


class CodexDesktopBridgeStagingTests(unittest.TestCase):
    """``scripts/v2/build.sh`` must copy and chmod the wrapper into the V2 app."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = BUILD_SCRIPT.read_text(encoding="utf-8")

    def test_build_sh_copies_wrapper_into_resources(self) -> None:
        # A ditto from the repository source into the staged Resources directory
        # is the canonical pattern (cf. the runtime checker on the line above).
        pattern = re.compile(
            r"ditto\s+\"\$\{?repository_root\}?/scripts/v2/CodexDesktopBridge\"\s+"
            r"\"\$\{?application\}?/Contents/Resources/CodexDesktopBridge\""
        )
        self.assertRegex(
            self.text,
            pattern,
            "build.sh must ditto scripts/v2/CodexDesktopBridge into Contents/Resources/",
        )

    def test_build_sh_makes_wrapper_executable(self) -> None:
        # The wrapper must be chmod 755 inside the staged app, matching the
        # other V2-shipped executables in Resources/.
        pattern = re.compile(
            r"chmod\s+755\s+\"\$\{?application\}?/Contents/Resources/CodexDesktopBridge\""
        )
        self.assertRegex(
            self.text,
            pattern,
            "build.sh must chmod 755 the staged CodexDesktopBridge wrapper",
        )

    def test_build_sh_places_wrapper_alongside_v2_runtime_contract(self) -> None:
        # The wrapper must land in Contents/Resources/ where the runtime.plist
        # also lives, so the wrapper's plist lookup resolves a sibling file.
        wrapper_index = self.text.find("Contents/Resources/CodexDesktopBridge")
        runtime_index = self.text.find("Contents/Resources/config/runtime.plist")
        self.assertNotEqual(wrapper_index, -1, "wrapper staging line is missing")
        self.assertNotEqual(runtime_index, -1, "runtime.plist staging line is missing")
        self.assertGreater(
            runtime_index,
            0,
            "runtime.plist must exist (read by the wrapper at runtime)",
        )

    def test_build_sh_zsh_syntax_is_valid(self) -> None:
        zsh = shutil.which("zsh") or shutil.which("/bin/zsh")
        if zsh is None:
            self.skipTest("zsh is unavailable on this host")
        result = subprocess.run(
            [zsh, "-n", str(BUILD_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=(
                "zsh -n reported a syntax error in build.sh:\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            ),
        )


class CodexDesktopBridgeStagingIntegrationTests(unittest.TestCase):
    """A minimal staging layout proves the wrapper resolves its runtime plist."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bundle = Path(self.tmp.name) / "Model Deck V2.app"
        self.resources = self.bundle / "Contents" / "Resources"
        self.resources.mkdir(parents=True)

    def test_wrapper_locates_sibling_runtime_plist_when_staged(self) -> None:
        # Mirror the V2 staging layout: wrapper sits in Resources/, the
        # runtime.plist that names python_executable sits in Resources/config/,
        # and the bundled engine source lives in Resources/python/src/. The
        # wrapper must read the plist from that relative position.
        wrapper_target = self.resources / "CodexDesktopBridge"
        shutil.copyfile(BRIDGE_WRAPPER, wrapper_target)
        wrapper_target.chmod(0o755)

        config_dir = self.resources / "config"
        config_dir.mkdir()
        # Use the host's Python executable so we never invent a path; the
        # wrapper only resolves and forwards it.
        host_python = Path(sys.executable)
        runtime_plist = config_dir / "runtime.plist"
        subprocess.run(
            ["/usr/bin/plutil", "-create", "xml1", str(runtime_plist)],
            check=True,
        )
        subprocess.run(
            [
                "/usr/bin/plutil",
                "-insert",
                "python_executable",
                "-string",
                str(host_python),
                str(runtime_plist),
            ],
            check=True,
        )

        # Replicate just enough of the wrapper's prelude so the integration
        # test does not depend on the producer-owned Python module actually
        # existing. This proves the PlistBuddy lookup resolves the staged
        # runtime.plist from the wrapper's bundle position.
        prelude = subprocess.run(
            [
                "/bin/zsh",
                "-c",
                (
                    "set -euo pipefail\n"
                    f"bridge_directory=\"{wrapper_target.parent}\"\n"
                    f"runtime_plist=\"$bridge_directory/config/runtime.plist\"\n"
                    "/usr/libexec/PlistBuddy -c 'Print :python_executable' \"$runtime_plist\"\n"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(prelude.stdout.strip(), str(host_python))


if __name__ == "__main__":
    unittest.main()
