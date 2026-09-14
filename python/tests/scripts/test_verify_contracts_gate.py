"""Focused test for the `contracts` gate in scripts/verify.py.

Runs `scripts.verify` as a subprocess with isolated state/artifact
roots and asserts (a) rc=0 on a clean tree, and (b) rc!=0 when the
canonical inventory disagrees with the rendered bundle manifest.
"""
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class VerifyContractsGateTests(unittest.TestCase):
    def _run_gate(self, state_root: Path, artifact_root: Path) -> int:
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
        completed = subprocess.run(command, cwd=str(REPO_ROOT), check=False)
        return completed.returncode

    def test_clean_tree_passes(self) -> None:
        with tempfile.TemporaryDirectory() as state_tmp, tempfile.TemporaryDirectory() as artifact_tmp:
            rc = self._run_gate(Path(state_tmp), Path(artifact_tmp))
            self.assertEqual(rc, 0, "contracts gate must pass on a clean tree")

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
            rc = self._run_gate(Path(state_tmp), Path(artifact_tmp))
            self.assertNotEqual(rc, 0, "contracts gate must fail when generated manifest drifts")
