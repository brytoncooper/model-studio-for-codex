"""Supervisor-injected event descriptor contracts for the plugin event broker."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

ContentClass = Literal["metadata", "content"]

PayloadValidator = Callable[[object], bool]


@dataclass(frozen=True, slots=True)
class EventDescriptor:
    """Exact registered shape of one broker topic (event ID).

    Supplied by the supervisor-owned resolver; the broker invents no grants.
    """
    topic: str
    owner_plugin: str
    validate_payload: PayloadValidator | None
    publish_effect: str
    publish_resource_scope: str
    publish_grant: str
    subscribe_effect: str
    subscribe_resource_scope: str
    subscribe_grant: str
    classification: ContentClass


class EventDescriptorResolver(Protocol):
    """Resolve an exact topic to its registered descriptor.

    Returns None for unknown topics. No wildcard matching: the topic must
    equal a registered descriptor event ID exactly.
    """

    def resolve(self, topic: str) -> EventDescriptor | None: ...
