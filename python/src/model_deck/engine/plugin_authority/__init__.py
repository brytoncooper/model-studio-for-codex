"""Public plugin invocation authority seam."""
from .authorization import AuthorityDeniedError, PluginAuthority
from .ports import (
    ActivationIdentity, ActivationState, AuthorityContext, OperationAuthority,
    OriginState, TrustedAuthorityState, TrustedContextStore,
)

__all__ = [
    "ActivationIdentity", "ActivationState", "AuthorityContext", "AuthorityDeniedError",
    "OperationAuthority", "OriginState", "PluginAuthority", "TrustedAuthorityState",
    "TrustedContextStore",
]
