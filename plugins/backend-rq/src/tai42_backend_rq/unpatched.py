"""The process's own threading primitives, as they were before ``--pool gevent`` patched it.

A gevent-patched process swaps its thread, lock and time primitives for cooperative ones; the
pieces that must block or run on a real OS thread (the refresher thread, the locks it waits on,
the guard of a job's stop) read the original ones here.
"""

from __future__ import annotations

import importlib
import sys
from _thread import LockType
from typing import Any


def original(module: str, name: str) -> Any:
    """``module.name`` as it was before ``--pool gevent`` patched the process (unchanged when it has not).

    Only gevent's ``monkey`` module patches, so while it is not loaded nothing is patched and the
    attribute is read as it stands: the pools that never load gevent do not load it here either.
    """
    monkey = sys.modules.get("gevent.monkey")
    if monkey is None:
        return getattr(importlib.import_module(module), name)
    return monkey.get_original(module, name)


def os_thread_lock() -> LockType:
    """A lock that blocks only the OS thread that waits on it, even in a process gevent has patched."""
    return original("_thread", "allocate_lock")()
