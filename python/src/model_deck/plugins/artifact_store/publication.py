"""Replaceable atomic directory publication boundary."""
from __future__ import annotations

import os
import sys
from typing import Protocol


class DirectoryPublisher(Protocol):
    """Move source to target under one directory fd, atomically and exclusively.

    Implementations must never replace an existing target of any type and must
    raise FileExistsError on collision. A return means the rename succeeded.
    """

    def __call__(self, directory_fd: int, source: str, target: str) -> None: ...


def publish_directory_exclusive(directory_fd: int, source: str, target: str) -> None:
    """macOS adapter; no unsafe rename fallback on unsupported platforms."""
    if sys.platform != "darwin":
        raise NotImplementedError("atomic directory publication unavailable")
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameatx_np
    except AttributeError:
        raise NotImplementedError("atomic directory publication unavailable") from None
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # <sys/stdio.h>: RENAME_EXCL fails if the destination already exists.
    if rename(directory_fd, os.fsencode(source), directory_fd, os.fsencode(target), 0x00000004):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
