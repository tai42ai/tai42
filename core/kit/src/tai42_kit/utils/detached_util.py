"""Marker for a detached agent run — one with no live caller holding a connection.

The run budget (``TAI_AGENT_RUN_TIMEOUT_SECONDS``) guards a live caller's held
connection; a detached run has no caller and runs unbounded. Three execution
classes are detached: a background submit, a hook/trigger fire, and a
backend-worker execution of a dequeued task. Each detached entry path flags the
context around the tool invocation and the run-tool binding reads the flag to
skip the budget. Homed in kit so both the skeleton binding that reads it and the
execution backends below the skeleton (which never import tai42-skeleton) can
set it.
"""

from contextvars import ContextVar, Token

_detached_run: ContextVar[bool] = ContextVar("tai42_detached_run", default=False)


def mark_detached_run() -> Token[bool]:
    """Flag the current context as a detached run; reset with the returned token."""
    return _detached_run.set(True)


def reset_detached_run(token: Token[bool]) -> None:
    """Restore the flag to its prior value, so it never leaks past the detached run."""
    _detached_run.reset(token)


def in_detached_run() -> bool:
    return _detached_run.get()
