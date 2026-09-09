"""The one bounded request-body reader every ingress door shares.

A public door must count the body's ACTUAL bytes as it streams, never trust a
client ``Content-Length``, and raise the moment the count crosses the cap — before
any parse, signature, or verification work runs over an unbounded read. Every
channel and callback door caps its own body with its own limit, so the READER is
shared and the ``cap`` stays each door's.

The reader is PURE: it consumes the stream and returns the joined bytes. A door that
must re-read (a size guard, then the handler) caches the returned bytes on its
request object itself — the reader keeps no framework coupling, so ``kit`` needs no
starlette runtime dependency. The request is typed by a minimal local protocol that
declares only the ``stream`` the reader uses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol


class PayloadTooLarge(Exception):
    """The request body streamed past the door's byte cap — a loud 413, never a
    truncated shorter body."""


class _StreamableRequest(Protocol):
    """The one thing the reader needs of a request: an async byte stream. Typing it
    here rather than as ``starlette.requests.Request`` keeps ``kit`` free of a
    starlette runtime dependency (every caller passes a starlette request, which
    satisfies this)."""

    def stream(self) -> AsyncIterator[bytes]: ...


async def read_bounded_body(request: _StreamableRequest, cap: int) -> bytes:
    """Read the body counting ACTUAL bytes, never a client ``Content-Length``.
    Raise :class:`PayloadTooLarge` the moment the stream crosses ``cap`` — the read
    stops there, so nothing is ever truncated into a shorter valid body."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise PayloadTooLarge(f"request body exceeds the {cap}-byte cap")
        chunks.append(chunk)
    return b"".join(chunks)
