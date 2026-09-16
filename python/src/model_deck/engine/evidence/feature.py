"""The evidence operations as kernel feature descriptors and handlers.

These are engine features composed alongside the built-in descriptors, not part
of the built-in set: their operations are routed *through* the kernel rather
than through the dispatch method chain, so the handlers here return
contract-valid results and raise for everything else.

Reads and refreshes are separate features on purpose. A cache with no source
composed still answers ``prices.query`` and ``benchmarks.query`` — with an
empty, stale, self-described snapshot — while ``prices.refresh`` and
``benchmarks.refresh`` simply do not appear in discovery, because there is
nothing for them to fetch from.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from model_deck.kernel import FeatureDescriptor, KernelApiVersion, OperationDescriptor
from model_deck.kernel.registry import Handler

from model_deck.engine.builtins.capabilities import BuiltinWiringError
from model_deck.engine.evidence.ports import (
    BENCHMARKS_KIND,
    PRICES_KIND,
    EvidenceResourceExhaustedError,
)
from model_deck.engine.kernel_composition import KernelDomainError
from model_deck.engine.jobs.first_party import (
    JOB_KIND_BENCHMARKS_REFRESH,
    JOB_KIND_PRICES_REFRESH,
)

__all__ = [
    "CAPABILITY_EVIDENCE_CACHE",
    "CAPABILITY_EVIDENCE_REFRESH",
    "COLLABORATOR_QUERY_BENCHMARKS",
    "COLLABORATOR_QUERY_PRICES",
    "COLLABORATOR_REFRESH_JOBS",
    "EVIDENCE_FEATURE_IDS",
    "EVIDENCE_OPERATION_IDS",
    "FEATURE_EVIDENCE_READ",
    "FEATURE_EVIDENCE_REFRESH",
    "evidence_feature_descriptors",
    "evidence_handler_adapters",
    "evidence_read_handlers",
    "evidence_refresh_handlers",
]

FEATURE_API = KernelApiVersion(major=1, minimum_minor=0)
FEATURE_VERSION = "1.0.0"

FEATURE_EVIDENCE_READ = "modeldeck.engine.evidence-read"
FEATURE_EVIDENCE_REFRESH = "modeldeck.engine.evidence-refresh"

EVIDENCE_FEATURE_IDS: tuple[str, ...] = (
    FEATURE_EVIDENCE_READ,
    FEATURE_EVIDENCE_REFRESH,
)

CAPABILITY_EVIDENCE_CACHE = "evidence.cache"
"""An evidence cache repository reached the engine, so cached reads can answer."""

CAPABILITY_EVIDENCE_REFRESH = "evidence.refresh"
"""An evidence source and a job runner reached the engine, so refreshes can run."""

COLLABORATOR_QUERY_PRICES = "query_prices"
COLLABORATOR_QUERY_BENCHMARKS = "query_benchmarks"
COLLABORATOR_REFRESH_JOBS = "evidence_refresh_jobs"

_OPERATION_PREFIX = "engine.v1."
_SCHEMA_PREFIX = "contracts/engine.v1/methods/"


def _operation(short_name: str, effect: str) -> OperationDescriptor:
    return OperationDescriptor(
        operation_id=f"{_OPERATION_PREFIX}{short_name}",
        input_schema_id=f"{_SCHEMA_PREFIX}{short_name}.params.schema.json",
        output_schema_id=f"{_SCHEMA_PREFIX}{short_name}.result.schema.json",
        effect=effect,
    )


def _feature(
    feature_id: str,
    *,
    operations: Sequence[tuple[str, str]],
    dependencies: Sequence[str] = (),
    required_capabilities: Sequence[str] = (),
) -> FeatureDescriptor:
    return FeatureDescriptor(
        feature_id=feature_id,
        version=FEATURE_VERSION,
        api=FEATURE_API,
        dependencies=tuple(dependencies),
        required_capabilities=tuple(required_capabilities),
        operations=tuple(_operation(name, effect) for name, effect in operations),
    )


_EVIDENCE_READ = _feature(
    FEATURE_EVIDENCE_READ,
    operations=(("prices.query", "read"), ("benchmarks.query", "read")),
    required_capabilities=(CAPABILITY_EVIDENCE_CACHE,),
)

_EVIDENCE_REFRESH = _feature(
    FEATURE_EVIDENCE_REFRESH,
    operations=(("prices.refresh", "write"), ("benchmarks.refresh", "write")),
    dependencies=(FEATURE_EVIDENCE_READ,),
    required_capabilities=(CAPABILITY_EVIDENCE_REFRESH,),
)

EVIDENCE_OPERATION_IDS: tuple[str, ...] = tuple(
    operation.operation_id
    for descriptor in (_EVIDENCE_READ, _EVIDENCE_REFRESH)
    for operation in descriptor.operations
)


def evidence_feature_descriptors(
    *, include_refresh: bool = True
) -> tuple[FeatureDescriptor, ...]:
    """The evidence features, in dependency order.

    Leave ``include_refresh`` false when no source is composed: the refresh
    feature is then absent rather than composed-and-unsupported, so discovery
    advertises only what the engine can actually do.
    """
    if include_refresh:
        return (_EVIDENCE_READ, _EVIDENCE_REFRESH)
    return (_EVIDENCE_READ,)


def _required(collaborators: Mapping[str, Any], name: str) -> Any:
    collaborator = collaborators.get(name)
    if collaborator is None:
        raise BuiltinWiringError(
            f"evidence capability is composed but {name} was not injected"
        )
    return collaborator


def evidence_read_handlers(
    descriptor: FeatureDescriptor,
    collaborators: Mapping[str, Any],
) -> dict[str, Handler]:
    """Bind the cached read operations. Neither of them ever fetches."""
    query_prices = _required(collaborators, COLLABORATOR_QUERY_PRICES)
    query_benchmarks = _required(collaborators, COLLABORATOR_QUERY_BENCHMARKS)

    def prices_query(params: Any, grants: frozenset[str]) -> dict[str, Any]:
        params = dict(params or {})
        # An answer too large to serve is the caller's to narrow, so it is
        # classified in the public vocabulary instead of being redacted to an
        # internal error the caller can do nothing about. Only the code travels;
        # dispatch writes the sentence, so nothing from here is published.
        try:
            return query_prices.query(
                registration_id=params.get("registration_id"),
                provider_model_id=params.get("provider_model_id"),
                include_stale=params.get("include_stale", True),
            ).to_wire()
        except EvidenceResourceExhaustedError:
            raise KernelDomainError("resource_exhausted") from None

    def benchmarks_query(params: Any, grants: frozenset[str]) -> dict[str, Any]:
        params = dict(params or {})
        try:
            return query_benchmarks.query(model_id=params.get("model_id")).to_wire()
        except EvidenceResourceExhaustedError:
            raise KernelDomainError("resource_exhausted") from None

    return {
        f"{_OPERATION_PREFIX}prices.query": prices_query,
        f"{_OPERATION_PREFIX}benchmarks.query": benchmarks_query,
    }


def evidence_refresh_handlers(
    descriptor: FeatureDescriptor,
    collaborators: Mapping[str, Any],
) -> dict[str, Handler]:
    """Bind the refresh operations. Each starts a job and returns its id."""
    refresh_jobs = _required(collaborators, COLLABORATOR_REFRESH_JOBS)

    def start(kind: str, job_kind: str) -> Handler:
        def handler(params: Any, grants: frozenset[str]) -> dict[str, Any]:
            params = dict(params or {})
            job_id = refresh_jobs.start(
                kind, idempotency_key=params["idempotency_key"]
            )
            return {
                "job_id": job_id,
                "job_kind": job_kind,
                "explicit_network": True,
            }

        return handler

    return {
        f"{_OPERATION_PREFIX}prices.refresh": start(
            PRICES_KIND, JOB_KIND_PRICES_REFRESH
        ),
        f"{_OPERATION_PREFIX}benchmarks.refresh": start(
            BENCHMARKS_KIND, JOB_KIND_BENCHMARKS_REFRESH
        ),
    }


def evidence_handler_adapters(*, include_refresh: bool = True) -> dict[str, Any]:
    """Feature ID to handler adapter, ready to merge into the engine registry."""
    adapters: dict[str, Any] = {FEATURE_EVIDENCE_READ: evidence_read_handlers}
    if include_refresh:
        adapters[FEATURE_EVIDENCE_REFRESH] = evidence_refresh_handlers
    return adapters
