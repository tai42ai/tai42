"""The reserved ``bridge:`` thread namespace.

A thread under this prefix carries the messaging bridge's per-conversation memory, so only
the bridge may address one. Every door that maps caller-supplied tool input to agent run
kwargs maps through :func:`run_kwargs_from_tool_input`, which is where the reservation is
enforced — an agent run reached by any other spelling would bypass it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel
from tai42_contract.agent import Agent

# The reserved thread namespace the messaging bridge alone writes.
BRIDGE_THREAD_PREFIX = "bridge:"

# The sub-namespace a LINKED person's aggregated thread is keyed under:
# ``bridge:@person:{person_id}``. ``@person`` can never be a route name
# (``ROUTE_NAME_RE = ^[a-z0-9-]+$``), so this form can never collide with a
# route-keyed ``bridge:{route_name}:{address}`` thread.
PERSON_THREAD_PREFIX = f"{BRIDGE_THREAD_PREFIX}@person:"


class ReservedThreadNamespaceError(ValueError):
    """A caller steered an agent run at the reserved ``bridge:`` thread namespace."""


def reserved_thread_namespace_error(agent: type[Agent], run_kwargs: Mapping[str, Any]) -> str | None:
    """The message for a caller-supplied ``bridge:``-prefixed thread or checkpoint id, or ``None``.

    The contract's own ``thread_id`` kwarg is checked here; every other thread and checkpoint id is
    the one ``agent`` reports its run kwargs address (:meth:`~tai42_contract.agent.Agent.addressed_memory`),
    however nested in the agent's own input shape.
    """
    thread_id = run_kwargs.get("thread_id")
    if isinstance(thread_id, str) and thread_id.startswith(BRIDGE_THREAD_PREFIX):
        return f"thread_id may not use the reserved {BRIDGE_THREAD_PREFIX!r} namespace"
    memory = agent.addressed_memory(run_kwargs)
    if any(thread.startswith(BRIDGE_THREAD_PREFIX) for thread in memory.thread_ids):
        return f"thread_id may not use the reserved {BRIDGE_THREAD_PREFIX!r} namespace"
    if any(checkpoint.startswith(BRIDGE_THREAD_PREFIX) for checkpoint in memory.checkpoint_ids):
        return f"checkpoint_id may not use the reserved {BRIDGE_THREAD_PREFIX!r} namespace"
    return None


def run_kwargs_from_tool_input(agent: Agent, validated: BaseModel) -> dict[str, Any]:
    """Map ``validated`` to ``agent``'s run kwargs and refuse a reserved thread id.

    The one seam every caller-driven agent run passes, so no door dispatches around the
    reservation. Raises :class:`ReservedThreadNamespaceError` on a reserved id, ``ValueError``
    on an input the agent's own mapping rejects.
    """
    run_kwargs = agent.from_tool_input(validated)
    message = reserved_thread_namespace_error(type(agent), run_kwargs)
    if message is not None:
        raise ReservedThreadNamespaceError(message)
    return run_kwargs


__all__ = [
    "BRIDGE_THREAD_PREFIX",
    "PERSON_THREAD_PREFIX",
    "ReservedThreadNamespaceError",
    "reserved_thread_namespace_error",
    "run_kwargs_from_tool_input",
]
