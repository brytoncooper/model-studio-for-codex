import unittest
from contextlib import nullcontext
from dataclasses import FrozenInstanceError

from model_deck.engine.plugin_data.versioning import (
    PluginDataBinding,
    PluginDataVersioningContractError,
    VersionedPluginDataStore,
)


class CompleteVersionedStore:
    def selected_revision(self, selected):
        raise NotImplementedError

    def repository_for(self, binding):
        raise NotImplementedError

    def freeze(self, operation_id, selected):
        raise NotImplementedError

    def stage(self, operation_id, candidate, frozen):
        raise NotImplementedError

    def thaw(self, operation_id, frozen):
        raise NotImplementedError

    def activate_selected(
        self,
        operation_id,
        selected,
        *,
        expected_data_revision,
        enabled,
    ):
        raise NotImplementedError

    def mutation_barrier(self):
        return nullcontext()


class StoreWithoutActivation:
    def selected_revision(self, selected):
        raise NotImplementedError

    def repository_for(self, binding):
        raise NotImplementedError

    def freeze(self, operation_id, selected):
        raise NotImplementedError

    def stage(self, operation_id, candidate, frozen):
        raise NotImplementedError

    def thaw(self, operation_id, frozen):
        raise NotImplementedError

    def mutation_barrier(self):
        return nullcontext()


class PluginDataVersioningContractTests(unittest.TestCase):
    def test_binding_accepts_shared_contract_values_and_is_immutable(self) -> None:
        binding = PluginDataBinding(
            namespace="com.example.notes",
            data_ref="ref:plugin-data.notes-7",
            activation_generation=4,
        )

        self.assertEqual(binding.namespace, "com.example.notes")
        self.assertEqual(binding.data_ref, "ref:plugin-data.notes-7")
        self.assertEqual(binding.activation_generation, 4)
        with self.assertRaises(FrozenInstanceError):
            binding.activation_generation = 5  # type: ignore[misc]

    def test_binding_rejects_malformed_namespace_data_ref_and_generation(self) -> None:
        invalid_bindings = (
            {"namespace": "notes", "data_ref": "ref:plugin-data.notes", "activation_generation": 0},
            {"namespace": "com.example.notes", "data_ref": "plugin-data.notes", "activation_generation": 0},
            {"namespace": "com.example.notes", "data_ref": "ref:PluginData", "activation_generation": 0},
            {"namespace": "com.example.notes", "data_ref": "ref:plugin-data.notes", "activation_generation": -1},
            {"namespace": "com.example.notes", "data_ref": "ref:plugin-data.notes", "activation_generation": True},
        )

        for values in invalid_bindings:
            with self.subTest(values=values):
                with self.assertRaises(PluginDataVersioningContractError):
                    PluginDataBinding(**values)

    def test_runtime_protocol_requires_data_lifecycle_and_activation_methods(self) -> None:
        self.assertIsInstance(CompleteVersionedStore(), VersionedPluginDataStore)
        self.assertNotIsInstance(StoreWithoutActivation(), VersionedPluginDataStore)


if __name__ == "__main__":
    unittest.main()
