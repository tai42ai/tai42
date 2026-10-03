"""The reserved ``bridge:`` thread namespace.

A thread under this prefix carries the messaging bridge's per-conversation memory, so only
the bridge may address one. Every door that maps caller-supplied tool input to agent run
kwargs maps through :func:`run_kwargs_from_tool_input`, which is where the reservation is
enforced — an agent run reached by any other spelling would bypass it.
"""

from __future__ import annotations

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

# The contract's own uniform thread kwargs — the top-level keys every agent run addresses a
# thread through (``tai42_contract.agent.Agent.run``). A caller that exposes one of these on an
# agent's ``ToolInput`` could steer the run into the reserved namespace through it.
_RESERVED_THREAD_KEYS = ("thread_id", "resume_checkpoint_id")

# Thread-scoping keys a caller could steer into the reserved namespace through an engine's own
# config blob (an agent that pins its thread inside a ``configurable`` mapping on a run kwarg).
_RESERVED_CONFIGURABLE_KEYS = ("thread_id", "checkpoint_id")


class ReservedThreadNamespaceError(ValueError):
    """A caller steered an agent run at the reserved ``bridge:`` thread namespace."""


def reserved_thread_namespace_error(run_kwargs: dict[str, Any]) -> str | None:
    """The message for a caller-supplied ``bridge:``-prefixed thread id, or ``None``.

    Returns the message when such an id appears in ``run_kwargs``, else ``None``. Two forms
    are scanned: the contract's own top-level thread kwargs (``thread_id`` /
    ``resume_checkpoint_id``), and a config-shaped spelling riding inside a ``configurable``
    mapping on any run-kwarg value (a run can carry several config-bearing kwargs, each an
    equal steering vector, so EVERY config-bearing value is scanned).
    """
    for key in _RESERVED_THREAD_KEYS:
        candidate = run_kwargs.get(key)
        if isinstance(candidate, str) and candidate.startswith(BRIDGE_THREAD_PREFIX):
            return f"{key} may not use the reserved {BRIDGE_THREAD_PREFIX!r} namespace"
    for value in run_kwargs.values():
        if not isinstance(value, dict):
            continue
        configurable = value.get("configurable")
        if not isinstance(configurable, dict):
            continue
        for key in _RESERVED_CONFIGURABLE_KEYS:
            candidate = configurable.get(key)
            if isinstance(candidate, str) and candidate.startswith(BRIDGE_THREAD_PREFIX):
                return f"{key} may not use the reserved {BRIDGE_THREAD_PREFIX!r} namespace"
    return None


def run_kwargs_from_tool_input(agent: Agent, validated: BaseModel) -> dict[str, Any]:
    """Map ``validated`` to ``agent``'s run kwargs and refuse a reserved thread id.

    The one seam every caller-driven agent run passes, so no door dispatches around the
    reservation. Raises :class:`ReservedThreadNamespaceError` on a reserved id, ``ValueError``
    on an input the agent's own mapping rejects.
    """
    run_kwargs = agent.from_tool_input(validated)
    message = reserved_thread_namespace_error(run_kwargs)
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
