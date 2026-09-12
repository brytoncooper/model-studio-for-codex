"""Public external provider execution adapter; process ownership stays in runtime."""
from .proxy import ExternalProviderExecution, ProviderProxyError

__all__ = ["ExternalProviderExecution", "ProviderProxyError"]
