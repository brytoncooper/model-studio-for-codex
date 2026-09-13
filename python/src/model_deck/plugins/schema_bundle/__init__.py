"""Plugin-local JSON Schema bundle validation."""

from .bundle import (
    MAX_SCHEMA_RESOURCE_BYTES,
    MAX_SCHEMA_RESOURCES,
    MAX_SCHEMA_TOTAL_BYTES,
    PluginSchemaBundle,
    PluginSchemaBundleError,
    PluginSchemaDataError,
)

__all__ = [
    "MAX_SCHEMA_RESOURCE_BYTES",
    "MAX_SCHEMA_RESOURCES",
    "MAX_SCHEMA_TOTAL_BYTES",
    "PluginSchemaBundle",
    "PluginSchemaBundleError",
    "PluginSchemaDataError",
]
