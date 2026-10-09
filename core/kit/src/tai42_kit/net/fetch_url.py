"""SSRF-pinning httpx transport and the ``fetch_url`` download helper.

Wrapping httpx's connection pool with a backend that validates every TCP target
through the SSRF guard and connects to the exact validated address closes
DNS-rebinding for the download path: the host is resolved once and the connection
is made to that address rather than re-resolving the hostname (which an attacker
could answer differently at connect time). While the guard is enabled it is
applied on the initial request and every redirect hop, and the response body is
size-capped while streaming; turning the guard off opts out of both.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Iterator
from contextlib import aclosing, contextmanager
from typing import Any, Final, cast

import httpcore
import httpx

from tai42_kit.net import url_guard
from tai42_kit.net.url_guard import UrlGuardError


class _PinningBackend(httpcore.AsyncNetworkBackend):
    """Validate every TCP target against the SSRF guard and connect to the validated address.

    Wrapping the transport's backend closes DNS-rebinding: the host is resolved
    once inside :func:`url_guard.resolve_and_validate`, and the connection is
    made to that validated address rather than re-resolving the hostname (which
    an attacker could answer differently at connect time). TLS is unaffected —
    httpcore drives SNI and certificate validation from the request's origin
    hostname, not from this address, so pinning stays transparent to TLS.

    This runs on every connection the pool opens, so each redirect hop that
    needs a new connection is validated and pinned in turn.
    """

    def __init__(self, wrapped: httpcore.AsyncNetworkBackend) -> None:
        self._wrapped = wrapped

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        validated_ip = await url_guard.resolve_and_validate(host)
        return await self._wrapped.connect_tcp(
            validated_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> httpcore.AsyncNetworkStream:
        return await self._wrapped.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._wrapped.sleep(seconds)


# httpcore's exported errors and httpx's class of the same name, for a transport built over the
# pool's public constructor. ``ConnectionNotAvailable`` is absent: the pool catches it and
# retries, so it never reaches the transport.
_HTTPCORE_TO_HTTPX: Final[dict[type[Exception], type[httpx.TransportError]]] = {
    httpcore.TimeoutException: httpx.TimeoutException,
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.WriteTimeout: httpx.WriteTimeout,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.NetworkError: httpx.NetworkError,
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.WriteError: httpx.WriteError,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
    httpcore.ProtocolError: httpx.ProtocolError,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
}


@contextmanager
def _as_httpx_errors() -> Iterator[None]:
    """Re-raise an httpcore exception as httpx's class of the same name; any other exception passes unchanged."""
    try:
        yield
    except Exception as exc:
        mapped = next((_HTTPCORE_TO_HTTPX[cls] for cls in type(exc).__mro__ if cls in _HTTPCORE_TO_HTTPX), None)
        if mapped is None:
            raise
        raise mapped(str(exc)) from exc


class _PoolResponseStream(httpx.AsyncByteStream):
    """httpx body stream over the pool's response stream, mapping httpcore errors raised mid-body."""

    def __init__(self, stream: AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with _as_httpx_errors():
            async for part in self._stream:
                yield part

    async def aclose(self) -> None:
        aclose = getattr(self._stream, "aclose", None)
        if aclose is not None:
            await aclose()


class PinningTransport(httpx.AsyncBaseTransport):
    """httpx transport over a connection pool whose network backend validates and pins every TCP target.

    Built through the libraries' public constructors: httpx's ``AsyncHTTPTransport`` takes no
    network backend, while httpcore's pool does. The pool settings, TLS context and backend are
    what ``httpx.AsyncHTTPTransport()`` builds by default, with :class:`_PinningBackend` wrapping
    the asyncio backend. Environment proxies stay unused: an ``httpx.AsyncClient`` given a
    transport reads none.
    """

    def __init__(self) -> None:
        """Build the pinned pool with ``httpx.AsyncHTTPTransport``'s default limits and TLS context."""
        limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            # ``httpcore`` declares a stand-in ``AnyIOBackend`` for installs without anyio; httpx
            # requires anyio, so the class here is the real asyncio network backend.
            network_backend=_PinningBackend(cast("httpcore.AsyncNetworkBackend", httpcore.AnyIOBackend())),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Send ``request`` through the pinned pool, mapping httpcore's errors to httpx's."""
        pool_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _as_httpx_errors():
            response = await self._pool.handle_async_request(pool_request)
        if not isinstance(response.stream, AsyncIterable):
            raise TypeError(f"the async connection pool returned a synchronous body stream: {type(response.stream)!r}")
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_PoolResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        """Close the pool and every connection it holds."""
        await self._pool.aclose()


def unwrap_guard_error(exc: BaseException) -> UrlGuardError | None:
    """Return the :class:`UrlGuardError` in ``exc``'s cause/context chain, if any.

    A guard rejection raised inside the network backend usually surfaces as itself.
    It can also end up buried in another exception's ``__cause__`` / ``__context__``
    — a failure while closing the response replaces the rejection that was in flight,
    and a transport could map it to an error of its own. Walking the chain lets the
    caller re-raise the SSRF failure loudly rather than whatever displaced it.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, UrlGuardError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


async def fetch_url(url: str) -> tuple[bytes, str | None]:
    """Fetch the URL's body and return ``(bytes, mime)``.

    ``mime`` is the response ``Content-Type`` header (``None`` when absent). When
    the guard is enabled the request runs over :class:`PinningTransport`, which
    validates and pins every connection (initial request and each redirect hop
    that opens a new connection), and the body is size-capped while streaming.
    Raises rather than truncating an over-cap body or reaching a non-public host.
    When the guard is disabled a plain client is used, which caps nothing, pins
    nothing, and lets httpx follow redirects.

    Under the guard, redirects are followed one hop at a time rather than by
    httpx, which reads each intermediate body into memory in full before chasing
    ``Location``. A hop carrying a body large enough to exhaust memory would slip
    past the cap that way, so each redirect response is closed unread — forgoing
    connection reuse for that hop — and only the terminal body is streamed and
    counted.
    """
    if not url_guard.guard_enabled():
        async with (
            httpx.AsyncClient(follow_redirects=True) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
            return bytes(buffer), response.headers.get("Content-Type")

    max_redirects = url_guard.url_guard_settings().max_redirects
    try:
        async with httpx.AsyncClient(transport=PinningTransport(), follow_redirects=False) as client:
            request = client.build_request("GET", url)
            for _ in range(max_redirects + 1):
                async with aclosing(await client.send(request, stream=True)) as response:
                    # ``next_request`` is httpx's fully-built redirect request (relative
                    # ``Location`` resolved, method and headers adjusted per status). It is
                    # set only when the response is a redirect the client did not follow.
                    if response.next_request is not None:
                        request = response.next_request
                        continue
                    response.raise_for_status()
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        buffer.extend(chunk)
                        url_guard.enforce_size(len(buffer))
                    return bytes(buffer), response.headers.get("Content-Type")
            raise UrlGuardError(  # noqa: TRY301 -- must pass through the except unwrap so it surfaces as the guard's rejection
                f"SSRF guard: exceeded max_redirects={max_redirects} fetching {url!r}."
            )
    except Exception as exc:
        guard_error = unwrap_guard_error(exc)
        if guard_error is not None:
            # Re-raise the guard's own rejection rather than whatever exception is carrying
            # it, so the caller sees the policy that refused the fetch instead of a generic
            # failure. Chaining onto the rejection's own context keeps whatever provoked it
            # (a resolver failure, say) as the reported cause, and is ``None`` when it was
            # raised on its own.
            raise guard_error from guard_error.__context__
        raise


__all__ = ["PinningTransport", "fetch_url", "unwrap_guard_error"]
