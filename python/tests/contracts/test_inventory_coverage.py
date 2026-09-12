
import unittest

from model_deck_contracts.inventory import iter_inventory_methods
from model_deck_contracts.paths import schema_path_for_method


class InventoryCoverageTests(unittest.TestCase):
    def test_every_inventory_method_has_param_and_result_schemas(self) -> None:
        for method in iter_inventory_methods():
            with self.subTest(method=method):
                self.assertTrue(schema_path_for_method(method, "params").is_file())
                self.assertTrue(schema_path_for_method(method, "result").is_file())
