"""Pooled HTTP client backed by curl_cffi ``AsyncSession`` instances.

Each pooled session drives its transfers through a kit ``AsyncCurl`` whose transfers start
from an empty context. libcurl arms its timer and socket callbacks while a transfer is added,
and each callback re-arms the next in its own context, so a keep-alive connection a caller's
request opened would otherwise keep that request's context for the connection's life.
"""

import asyncio
import contextvars

import anyio
from curl_cffi import AsyncCurl, Curl, requests
from curl_cffi.requests.exceptions import SessionClosed

from tai42_kit.clients.base import PooledClient, is_loop_bound_runtime_error, reject_unknown_connection_kwargs

_ALLOWED_KWARGS = frozenset({"session_params", "share_key"})


class _EmptyContextAsyncCurl(AsyncCurl):
    """An ``AsyncCurl`` that adds every transfer from an empty context.

    The loop callbacks libcurl installs while the transfer is added (the timer, then the socket
    reader and writer it registers) copy the context they are created in, so they carry no
    caller's context variables.
    """

    def add_handle(self, curl: Curl) -> asyncio.Future:
        return contextvars.Context().run(super().add_handle, curl)


class CurlClient(PooledClient[requests.AsyncSession]):
    """Manages reusable curl_cffi sessions.

    Every primitive in ``session_params`` maps onto the ``AsyncSession``, giving
    full control over the network layer, except ``async_curl``: the client supplies
    the session's ``AsyncCurl`` itself and closes it with the session. ``share_key:
    str | None`` is part of the pool identity only: callers passing the same
    ``share_key`` and equal ``session_params`` share one pooled session.
    """

    async def _create(self, **kwargs) -> requests.AsyncSession:
        reject_unknown_connection_kwargs("Curl client", kwargs, _ALLOWED_KWARGS)
        session_params = dict(kwargs.get("session_params", {}))
        if "async_curl" in session_params:
            raise TypeError("Curl client: session_params cannot set async_curl; the client supplies its own")
        acurl = _EmptyContextAsyncCurl(loop=session_params.get("loop") or asyncio.get_running_loop())
        return requests.AsyncSession(async_curl=acurl, **session_params)

    async def _close(self, client: requests.AsyncSession):
        if client:
            # The session does not own the ``AsyncCurl`` it was given, so the client closes it.
            try:
                await client.close()
            finally:
                await client.acurl.close()

    def _disconnection_exceptions(self) -> tuple[type[Exception], ...]:
        return (
            # The async resource is dead (socket / loop closed)
            anyio.ClosedResourceError,
            anyio.BrokenResourceError,
            # The curl_cffi session object is explicitly closed / invalid
            SessionClosed,
        )

    def _is_disconnection_error(self, exc: BaseException) -> bool:
        # curl_cffi/anyio surface a dead loop-bound session as a plain
        # RuntimeError ("Event loop is closed" / "... attached to a different
        # loop"); match those by message so an unrelated RuntimeError raised by
        # caller code never tears down the pool.
        return super()._is_disconnection_error(exc) or is_loop_bound_runtime_error(exc)
