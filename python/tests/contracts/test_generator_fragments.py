import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.generate_contracts as generator


class GeneratorFragmentTests(unittest.TestCase):
    def test_missing_local_fragment_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "probe.schema.json"
            schema_path.write_text(
                json.dumps(
                    {
                        "definitions": {"present": {"type": "string"}},
                        "$ref": "#/definitions/FINALIZER_MISSING",
                    }
                ),
                encoding="utf-8",
            )
            doc = json.loads(schema_path.read_text(encoding="utf-8"))
            with self.assertRaises(FileNotFoundError) as ctx:
                generator.resolve_ref_string(
                    "#/definitions/FINALIZER_MISSING",
                    schema_path,
                    root_doc=doc,
                )
            self.assertIn("FINALIZER_MISSING", str(ctx.exception))

    def test_external_fragment_with_escaped_pointer(self) -> None:
        doc = {
            "definitions": {
                "a~b/c": {"type": "string"},
            }
        }
        node = generator.resolve_json_pointer(doc, "#/definitions/a~0b~1c")
        self.assertEqual(node, {"type": "string"})
