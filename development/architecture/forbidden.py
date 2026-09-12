from __future__ import annotations

from development.architecture.graph import Layer

KERNEL_ENGINE_STDLIB_ALLOWLIST = frozenset(
    {
        "__future__",
        "abc",
        "ast",
        "asyncio",
        "base64",
        "binascii",
        "bisect",
        "collections",
        "contextlib",
        "copy",
        "csv",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "functools",
        "hashlib",
        "hmac",
        "html",
        "importlib",
        "inspect",
        "io",
        "itertools",
        "json",
        "logging",
        "math",
        "operator",
        "pathlib",
        "queue",
        "re",
        "secrets",
        "string",
        "struct",
        "textwrap",
        "threading",
        "time",
        "types",
        "typing",
        "unicodedata",
        "uuid",
        "weakref",
        "zoneinfo",
    }
)

KERNEL_ENGINE_FORBIDDEN_ROOTS = frozenset(
    {
        "fcntl",
        "sqlite3",
        "AppKit",
        "Foundation",
        "Security",
        "keyring",
        "subprocess",
        "socket",
        "multiprocessing",
        "ctypes",
        "darwin",
        "posix",
        "cursor_agent",
        "cursor_sdk_runtime",
        "codex_runtime",
        "provider_bridge",
        "local_router",
        "anthropic",
        "openai",
        "cursor",
        "httpx",
        "requests",
        "aiohttp",
    }
)


def forbidden_root_for_layer(layer: Layer, root: str) -> bool:
    if layer not in {Layer.KERNEL, Layer.ENGINE}:
        return False
    if root in KERNEL_ENGINE_FORBIDDEN_ROOTS:
        return True
    return False


def stdlib_root_allowed_for_core(layer: Layer, root: str) -> bool:
    if layer not in {Layer.KERNEL, Layer.ENGINE}:
        return True
    return root in KERNEL_ENGINE_STDLIB_ALLOWLIST
