"""Public process-backed extension activation composition."""

from .adapter import (
    ActivationAuthorityController,
    ActivationBrokerFactory,
    ActivationLifecycleConfigurationError,
    ActivationLifecycleConflictError,
    ActivationLifecycleOperationError,
    ArtifactLaunchResolver,
    ProcessExtensionActivationLifecycle,
    ResolvedArtifactLaunch,
    ServingActivation,
)

__all__ = [
    "ActivationAuthorityController",
    "ActivationBrokerFactory",
    "ActivationLifecycleConfigurationError",
    "ActivationLifecycleConflictError",
    "ActivationLifecycleOperationError",
    "ArtifactLaunchResolver",
    "ProcessExtensionActivationLifecycle",
    "ResolvedArtifactLaunch",
    "ServingActivation",
]
