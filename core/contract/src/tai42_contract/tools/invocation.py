"""The ambient in-process invoked-tool seam.

For the duration of a tool's execution the platform deposits the invoked tool's
name here; in-process code reads it to learn WHICH tool is running without the
name being threaded through every call. The deposit is the invoked tool's own
name; outside any tool execution the reader returns ``None``.

The channel lives in the CONTRACT (not the skeleton) on purpose: a reader and
the execution door that arms it can sit in different packages that share only
:mod:`tai42_contract`, so the ambient channel must live in the layer both may
import. The contract interprets NOTHING about the deposited name — it is a
logic-free channel, mirroring the run-attribution ContextVar discipline.

A task created inside a deposited block runs on a COPY and keeps the deposit for
its lifetime, so a detached continuation stays attributed to the invoked tool.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from pydantic import BaseModel, ConfigDict


class ToolInvocation(BaseModel):
    """The tool execution currently in flight: ``tool_name`` is the invoked
    tool's registered name. Frozen — a deposited invocation is a fact of the
    active execution, never mutated in place."""

    model_config = ConfigDict(frozen=True)

    tool_name: str


_current_tool_invocation: ContextVar[ToolInvocation | None] = ContextVar("tai42_current_tool_invocation", default=None)


def current_tool_invocation() -> ToolInvocation | None:
    """The tool execution in flight for the current context, or ``None`` when no
    tool is executing."""
    return _current_tool_invocation.get()


def set_current_tool_invocation(invocation: ToolInvocation) -> Token[ToolInvocation | None]:
    """Deposit ``invocation`` as the in-flight tool for the current context; pass
    the returned token to :func:`reset_current_tool_invocation` to restore the
    previous value. Nested deposits (a tool invoking another) re-set for the inner
    call and restore the outer value on reset — ContextVar token discipline."""
    return _current_tool_invocation.set(invocation)


def reset_current_tool_invocation(token: Token[ToolInvocation | None]) -> None:
    """Restore the in-flight tool to the value captured in ``token`` by the
    matching :func:`set_current_tool_invocation` call."""
    _current_tool_invocation.reset(token)


__all__ = [
    "ToolInvocation",
    "current_tool_invocation",
    "reset_current_tool_invocation",
    "set_current_tool_invocation",
]
