"""MCP transport contract: open a client session to an MCP server.

Implementations subclass fastmcp ``ClientTransport`` over a UDS/HTTP socket."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mcp import ClientSession


@runtime_checkable
class Transport(Protocol):
    """An MCP client transport."""

    def connect_session(self, **session_kwargs: Any) -> AbstractAsyncContextManager[ClientSession]:
        """Open and yield a connected MCP ``ClientSession`` for the lifetime of
        the context."""
        ...


__all__ = ["Transport"]
