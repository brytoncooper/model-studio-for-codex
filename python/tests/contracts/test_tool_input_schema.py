import unittest

from model_deck_contracts import validate_tool_input_schema


class ToolInputSchemaTests(unittest.TestCase):
    def test_reference_spelling_in_literal_data_is_preserved_and_detached(self):
        literal = {"$ref": "https://example.invalid/ordinary-data", "$id": "ordinary-id"}
        for keyword in ("default", "const", "enum", "examples"):
            value = [literal] if keyword in ("enum", "examples") else literal
            schema = {"type": "object", keyword: value}
            with self.subTest(keyword=keyword):
                result = validate_tool_input_schema(schema)
                self.assertEqual(result, schema)
                self.assertIsNot(result[keyword], value)
        nested = {"properties": {"name": {"default": literal}}}
        self.assertEqual(validate_tool_input_schema(nested), nested)

    def test_external_reference_in_schema_locations_is_rejected(self):
        external = {"$ref": "https://example.invalid/schema"}
        schemas = [external, {"$id": "https://example.invalid/base"},
                   {"properties": {"name": external}}, {"$defs": {"value": external}},
                   {"allOf": [external]}, {"items": external}, {"prefixItems": [external]},
                   {"dependentSchemas": {"name": external}}, {"contentSchema": external},
                   {"unevaluatedProperties": external}, {"$dynamicRef": "https://example.invalid/#anchor"}]
        for schema in schemas:
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                validate_tool_input_schema(schema)

    def test_local_reference_is_allowed_and_literal_target_becomes_schema(self):
        schema = {"$defs": {"value": {"type": "string"}}, "properties": {"name": {"$ref": "#/$defs/value"}}}
        self.assertEqual(validate_tool_input_schema(schema), schema)
        self.assertEqual(validate_tool_input_schema({"$ref": "#"}), {"$ref": "#"})
        with self.assertRaises(ValueError):
            validate_tool_input_schema({"$ref": "#/default", "default": {"$ref": "https://example.invalid/schema"}})

    def test_literal_data_still_requires_finite_json(self):
        with self.assertRaises(ValueError):
            validate_tool_input_schema({"default": {"$ref": float("nan")}})
