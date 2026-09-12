import json
import unittest

from model_deck_contracts.paths import fixtures_root
from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref


def _load(name: str, *, valid: bool) -> dict:
    folder = "valid" if valid else "invalid"
    return json.loads((fixtures_root() / folder / name).read_text(encoding="utf-8"))


class ModelRevisionCasTests(unittest.TestCase):
    def test_registered_list_item_includes_revision(self) -> None:
        item = _load("model_list_item_registered.json", valid=True)
        self.assertIn("revision", item)
        validate_schema_ref(
            "contracts/engine.v1/vocabulary.schema.json#/definitions/model_list_item_registered",
            item,
        )

    def test_registered_model_requires_revision(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/engine.v1/vocabulary.schema.json#/definitions/model_list_item_registered",
                _load("model_list_item_registered_missing_revision.json", valid=False),
            )

    def test_rename_requires_expected_revision(self) -> None:
        validate_schema_ref(
            "contracts/engine.v1/methods/models.rename.params.schema.json",
            _load("models_rename_params.json", valid=True),
        )
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/engine.v1/methods/models.rename.params.schema.json",
                _load("models_rename_missing_expected_revision.json", valid=False),
            )

    def test_remove_requires_expected_revision(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/engine.v1/methods/models.remove.params.schema.json",
                _load("models_remove_missing_expected_revision.json", valid=False),
            )

    def test_register_requires_expected_revision(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/engine.v1/methods/models.register.params.schema.json",
                _load("models_register_missing_expected_revision.json", valid=False),
            )

    def test_connections_save_rejects_revision_inside_connection(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema_ref(
                "contracts/engine.v1/methods/connections.save.params.schema.json",
                _load("connections_save_ambiguous_connection_revision.json", valid=False),
            )


if __name__ == "__main__":
    unittest.main()
