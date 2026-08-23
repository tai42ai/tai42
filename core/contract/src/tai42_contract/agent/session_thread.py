"""The ambient in-process agent session-thread deposit seam.

A driver running an agent through the synthesized ``run`` tool can deposit a thread id for
the wrapped block; the skeleton's run-tool binding reads it and, when the caller pinned no
``thread_id`` of its own, threads it onto the run so successive runs address the SAME agent
thread (memory carries across them). This module owns that ambient deposit — a ContextVar,
mirroring the run-attribution discipline — and the context manager that sets/resets it.

The channel lives in the CONTRACT (not the skeleton) on purpose: the reader (the skeleton
run-tool binding) and the writer (a contract-facing plugin such as the flows iterate engine)
sit in different packages that share only :mod:`tai42_contract`, so the ambient channel both
address must live in the layer both may import. The contract interprets NOTHING about the
deposited id — it is a logic-free channel, exactly like the neutral ``Agent`` vocabulary
this package already owns.

A task created inside :func:`agent_session_thread` runs on a COPY and keeps the deposit for
its lifetime, so a detached run stays threaded.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_current_session_thread: ContextVar[str | None] = ContextVar("tai42_current_session_thread", default=None)


def get_agent_session_thread() -> str | None:
    """The session thread deposited for the current run, or ``None`` when none is set."""
    return _current_session_thread.get()


def set_agent_session_thread(thread_id: str | None) -> Token[str | None]:
    """Bind ``thread_id`` as the current run's session thread; pass the returned token to
    :func:`reset_agent_session_thread` to restore the previous value."""
    return _current_session_thread.set(thread_id)


def reset_agent_session_thread(token: Token[str | None]) -> None:
    """Restore the session thread to the value captured in ``token`` by the matching
    :func:`set_agent_session_thread` call."""
    _current_session_thread.reset(token)


@contextmanager
def agent_session_thread(thread_id: str) -> Generator[None]:
    """Deposit ``thread_id`` as the ambient session thread for the wrapped block, resetting
    it in a ``finally``. A task created inside the block inherits it on a copy. Absent this
    wrap the deposit stays ``None`` and a run mints its own fresh thread — byte-identical to
    the pre-deposit behavior."""
    token = set_agent_session_thread(thread_id)
    try:
        yield
    finally:
        reset_agent_session_thread(token)


__all__ = [
    "agent_session_thread",
    "get_agent_session_thread",
    "reset_agent_session_thread",
    "set_agent_session_thread",
]
