"""The HTTP answer an outbound MCP failure carries, read across its exception chain.

A failure of an MCP request is often a wrapped error (a client connect error around an ``httpx``
response error, a task-group error around it), so the status and the response are read across the
whole ``__cause__`` / ``__context__`` chain, not just the outermost exception.
"""

from __future__ import annotations

from typing import Any


def exception_chain(exc: BaseException) -> list[BaseException]:
    """The exception and every ``__cause__`` / ``__context__`` behind it, cycle-guarded."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def mcp_http_response(exc: BaseException) -> Any | None:
    """The first response in the chain that carries an integer ``status_code``, or ``None``."""
    for err in exception_chain(exc):
        response = getattr(err, "response", None)
        if isinstance(getattr(response, "status_code", None), int):
            return response
    return None


def mcp_http_status(exc: BaseException) -> int | None:
    """The HTTP status a failure carries — from a response error or a ``status_code`` — or ``None``.

    ``None`` when no exception in the chain carries an integer status (a pure transport failure).
    """
    for err in exception_chain(exc):
        response = getattr(err, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
        status = getattr(err, "status_code", None)
        if isinstance(status, int):
            return status
    return None
