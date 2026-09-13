"""Generation-bound plugin data contracts for extension lifecycle storage."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from model_deck.engine.extensions.ports import (
    ExtensionDataLifecycle,
    SelectedInstallation,
)
from model_deck.engine.plugin_data.ports import PluginDataRepository
from model_deck_contracts.validator import SchemaValidationError, validate_schema_ref

_COMMON_TYPES = "contracts/common/types.schema.json#/definitions/"


class PluginDataVersioningContractError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid plugin data versioning contract")


def _validate_shared_type(type_name: str, value: object) -> None:
    try:
        validate_schema_ref(_COMMON_TYPES + type_name, value)
    except SchemaValidationError:
        raise PluginDataVersioningContractError() from None


@dataclass(frozen=True, slots=True)
class PluginDataBinding:
    """Trusted serving target captured for one activation generation.

    Workers never construct this value from request fields. A repository returned
    for this binding must check all three fields on every operation rather than
    trusting a successful check performed when the repository was created.
    """

    namespace: str
    data_ref: str
    activation_generation: int

    def __post_init__(self) -> None:
        _validate_shared_type("reverse_domain_id", self.namespace)
        _validate_shared_type("opaque_ref", self.data_ref)
        if type(self.activation_generation) is not int or self.activation_generation < 0:
            raise PluginDataVersioningContractError()


# The callable receives a repository already bound to one non-serving staged
# generation. It cannot choose a serving data reference or another namespace.
PluginDataMigration = Callable[[PluginDataRepository], None]


@runtime_checkable
class VersionedPluginDataStore(ExtensionDataLifecycle, Protocol):
    """Storage-owned bridge between broker CRUD and lifecycle data generations."""

    def repository_for(self, binding: PluginDataBinding) -> PluginDataRepository:
        """Return CRUD bound to one trusted activation and data generation.

        Implementations revalidate the binding on every call. Frozen generations
        may remain readable, but ``put`` and ``delete`` must reject them.
        """
        ...

    def activate_selected(
        self,
        operation_id: str,
        selected: SelectedInstallation,
        *,
        expected_data_revision: int,
        enabled: bool,
    ) -> None:
        """Apply the selected generation's serving state under the shared barrier.

        ``enabled=True`` may make only the exact selected activation generation
        writable. ``enabled=False`` must leave its data sealed, frozen, or retained.
        """
        ...

    def mutation_barrier(self) -> AbstractContextManager[None]:
        """Return the barrier shared by broker authorization and lifecycle writes."""
        ...


__all__ = [
    "PluginDataBinding",
    "PluginDataMigration",
    "PluginDataVersioningContractError",
    "VersionedPluginDataStore",
]
