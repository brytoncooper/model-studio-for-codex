"""Pure in-memory plugin manifest inspection.

Public surface is intentionally small:

- :class:`InspectionError` and :class:`InspectionErrorCode` for stable error
  reporting.
- :class:`InspectionResult` and its detached value types
  (:class:`Identity`, :class:`Api`, :class:`Entrypoint`,
  :class:`Contributions`, :class:`OperationContribution`,
  :class:`PanelContribution`, :class:`ProviderContribution`,
  :class:`InspectionFailure`).
- :func:`inspect_manifest` and :func:`inspect_entrypoint_path` as the only
  entry points.

The inspector relies only on the frozen ``contracts/plugin.v1`` manifest
schema and the bundled :mod:`model_deck_contracts` validator. It performs no
filesystem access, process execution, network call, or plugin discovery.
"""
from .errors import InspectionError, InspectionErrorCode
from .inspection import inspect_entrypoint_path, inspect_manifest
from .models import (
    Api,
    Contributions,
    Entrypoint,
    Identity,
    InspectionFailure,
    InspectionResult,
    OperationContribution,
    PanelContribution,
    ProviderContribution,
)

__all__ = [
    "Api",
    "Contributions",
    "Entrypoint",
    "Identity",
    "InspectionError",
    "InspectionErrorCode",
    "InspectionFailure",
    "InspectionResult",
    "OperationContribution",
    "PanelContribution",
    "ProviderContribution",
    "inspect_entrypoint_path",
    "inspect_manifest",
]
