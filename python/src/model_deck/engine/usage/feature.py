"""``usage.summary`` as a kernel feature descriptor and handler.

Composed alongside the built-in descriptors and routed through the kernel, so
the handler returns a contract-valid result and raises for everything else.
The feature is deliberately separate from the built-in ``usage`` group: it is
composed exactly when a usage query is, and it adds totals on top of the same
reader rather than a second source of records.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from model_deck.kernel import FeatureDescriptor, KernelApiVersion, OperationDescriptor
from model_deck.kernel.registry import Handler

from model_deck.engine.builtins.capabilities import BuiltinWiringError

__all__ = [
    "CAPABILITY_USAGE_SUMMARY",
    "COLLABORATOR_SUMMARIZE_USAGE",
    "FEATURE_USAGE_SUMMARY",
    "USAGE_SUMMARY_OPERATION_ID",
    "usage_summary_feature_descriptors",
    "usage_summary_handler_adapters",
    "usage_summary_handlers",
]

FEATURE_USAGE_SUMMARY = "modeldeck.engine.usage-summary"
CAPABILITY_USAGE_SUMMARY = "usage.summary"
COLLABORATOR_SUMMARIZE_USAGE = "summarize_usage"

USAGE_SUMMARY_OPERATION_ID = "engine.v1.usage.summary"

_USAGE_SUMMARY = FeatureDescriptor(
    feature_id=FEATURE_USAGE_SUMMARY,
    version="1.0.0",
    api=KernelApiVersion(major=1, minimum_minor=0),
    required_capabilities=(CAPABILITY_USAGE_SUMMARY,),
    operations=(
        OperationDescriptor(
            operation_id=USAGE_SUMMARY_OPERATION_ID,
            input_schema_id="contracts/engine.v1/methods/usage.summary.params.schema.json",
            output_schema_id="contracts/engine.v1/methods/usage.summary.result.schema.json",
            effect="read",
        ),
    ),
)


def usage_summary_feature_descriptors() -> tuple[FeatureDescriptor, ...]:
    return (_USAGE_SUMMARY,)


def usage_summary_handlers(
    descriptor: FeatureDescriptor,
    collaborators: Mapping[str, Any],
) -> dict[str, Handler]:
    summarize = collaborators.get(COLLABORATOR_SUMMARIZE_USAGE)
    if summarize is None:
        raise BuiltinWiringError(
            "usage summary is composed but summarize_usage was not injected"
        )

    def usage_summary(params: Any, grants: frozenset[str]) -> dict[str, Any]:
        params = dict(params or {})
        return summarize.summarize(
            since=params.get("since"), until=params.get("until")
        ).to_wire()

    return {USAGE_SUMMARY_OPERATION_ID: usage_summary}


def usage_summary_handler_adapters() -> dict[str, Any]:
    return {FEATURE_USAGE_SUMMARY: usage_summary_handlers}
