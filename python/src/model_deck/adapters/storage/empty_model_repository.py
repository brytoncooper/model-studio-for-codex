"""A model repository that holds no registrations.

The minimal engine composition (no application database, no host integration)
still has to answer ``engine.v1.models.list``. This is the vendor-free default
for that path: a composition root that wants real rows injects a repository of
its own instead of the engine reaching for one vendor's on-disk format.
"""
from __future__ import annotations

from model_deck.engine.model_library.ports import ModelRepository, RegisteredModelRecord


class EmptyModelRepository(ModelRepository):
    """Answers every registered-model query with an empty list."""

    def list_registered(self, *, connection_id: str | None = None) -> list[RegisteredModelRecord]:
        if connection_id is not None and not isinstance(connection_id, str):
            raise ValueError("connection_id must be a string")
        return []
