"""The ambient in-process session-thread door, surfaced in the skeleton namespace.

The door itself — the ContextVar deposit + the :func:`agent_session_thread` context manager
— is owned by :mod:`tai42_contract.agent.session_thread` because its reader (the run-tool
binding here) and its writer (a contract-facing driver plugin)
share only the contract package. The skeleton re-exports it so its own code and consumers
import the whole agent seam through one cohesive ``tai42_skeleton.agent`` namespace.

:meth:`AgentBinding._register_agent_tool` reads the deposit and, when the caller pinned no
``thread_id`` of its own, threads it onto the run so successive runs of the same node address
the SAME agent thread. Absent a deposit the run mints its own fresh thread — byte-identical
to the pre-deposit behavior.
"""

from __future__ import annotations

from tai42_contract.agent.session_thread import (
    agent_session_thread,
    get_agent_session_thread,
    reset_agent_session_thread,
    set_agent_session_thread,
)

__all__ = [
    "agent_session_thread",
    "get_agent_session_thread",
    "reset_agent_session_thread",
    "set_agent_session_thread",
]
