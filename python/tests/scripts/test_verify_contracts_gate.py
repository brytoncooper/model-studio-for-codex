"""Focused test for the `contracts` gate in scripts/verify.py.

Runs `scripts.verify` as a subprocess with isolated state/artifact
roots and asserts (a) rc=0 on a clean tree, and (b) rc!=0 when the
canonical inventory disagrees with the rendered bundle manifest.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DRIFT_MESSAGE = "_generated_inventory.json differs from rendered canonical inventory"


class VerifyContractsGateTests(unittest.TestCase):
    def _run_gate(self, state_root: Path, artifact_root: Path) -> tuple[int, str]:
        """Run the `contracts` gate and return (exit code, merged output).

        The child's output is captured instead of inherited. One of these
        tests provokes a deliberate bundle-parity failure; inherited output
        would land in the surrounding `scripts/verify.py` run, which echoes
        every step's output, and read there as if the real contracts gate
        had failed.

        The capture goes to a temporary file rather than a pipe, matching
        `scripts/verify.py`: a leaked grandchild holding a pipe open would
        otherwise block this test long after the direct child exited.
        """
        command = [
            sys.executable,
            "-B",
            "scripts/verify.py",
            "contracts",
            "--state-root",
            str(state_root),
            "--artifact-root",
            str(artifact_root),
        ]
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as sink:
            completed = subprocess.run(
                command,
                cwd=str(REPO_ROOT),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
            )
            sink.seek(0)
            output = sink.read()
        return completed.returncode, output

    def test_clean_tree_passes(self) -> None:
        with tempfile.TemporaryDirectory() as state_tmp, tempfile.TemporaryDirectory() as artifact_tmp:
            rc, output = self._run_gate(Path(state_tmp), Path(artifact_tmp))
            self.assertEqual(
                rc,
                0,
                f"contracts gate must pass on a clean tree; gate output:\n{output}",
            )

    def test_drift_in_generated_python_manifest_fails(self) -> None:
        with tempfile.TemporaryDirectory() as state_tmp, tempfile.TemporaryDirectory() as artifact_tmp:
            manifest = REPO_ROOT / "python/src/model_deck_contracts/_generated_inventory.json"
            backup = manifest.read_bytes()
            self.addCleanup(manifest.write_bytes, backup)
            # Replace the generated manifest with bytes that cannot match
            # the rendered canonical inventory (sorted keys, indent=2).
            manifest.write_text(
                json.dumps({"_stub": True}, indent=4, sort_keys=False) + "\n",
                encoding="utf-8",
            )
            rc, output = self._run_gate(Path(state_tmp), Path(artifact_tmp))
            self.assertNotEqual(rc, 0, "contracts gate must fail when generated manifest drifts")
            self.assertIn(
                DRIFT_MESSAGE,
                output,
                f"contracts gate must name the drifted python manifest; gate output:\n{output}",
            )
