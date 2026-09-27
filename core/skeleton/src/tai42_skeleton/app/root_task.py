"""Spawn a fire-and-forget root of execution in a fresh context.

A root of execution — a turn, a delivery, a detached continuation drive, a background
run — is not a child of whatever happened to spawn it: it is a NEW outermost run that must
carry only the facts it was given, never the ambient run scope of its spawner. An ordinary
:func:`asyncio.create_task` starts the task on a COPY of the spawner's context, so every
contextvar of a run in flight (its call chain, its run-delivery identity, its bound
execution identity, any scope a caller set) would leak into the new root. This helper
starts the task in a fresh :class:`contextvars.Context` instead, so the root inherits
nothing and binds every ambient fact it needs from explicit arguments.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Coroutine
from typing import Any

__all__ = ["spawn_root_task"]


def spawn_root_task(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """Schedule ``coro`` as a new root of execution: a task that inherits no ambient scope of its spawner.

    The task runs in a fresh :class:`contextvars.Context`, so it reads the default of every
    contextvar rather than a copy of the spawner's run scope. A root binds what it needs
    itself, from explicit arguments passed into ``coro``. A fresh context is created per
    call — a context may be entered by only one task at a time, so one is never shared.
    """
    return asyncio.create_task(coro, name=name, context=contextvars.Context())
