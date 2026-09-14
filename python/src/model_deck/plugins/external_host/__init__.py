"""Small composition root for installed process extensions."""

from .host import ExternalExtensionHost, HostConflictError, HostNotServingError, HostDependencies

__all__ = ["ExternalExtensionHost", "HostConflictError", "HostNotServingError", "HostDependencies"]
