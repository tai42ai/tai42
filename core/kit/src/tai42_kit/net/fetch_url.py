"""SSRF-pinning httpx transport, the shared guarded fetch client, and the ``fetch_url`` download helper.

Wrapping httpx's connection pool with a backend that validates every TCP target
through the SSRF guard and connects to the exact validated address closes
DNS-rebinding for the download path: the host is resolved once and the connection
is made to that address rather than re-resolving the hostname (which an attacker
could answer differently at connect time). While the guard is enabled it is
applied on the initial request and every redirect hop, and the response body is
size-capped while streaming; turning the guard off opts out of both.

:func:`guarded_fetch_client` leases the one ``httpx.AsyncClient`` every guarded fetch
of one configuration shares on its event loop — :func:`fetch_url` here,
:func:`~tai42_kit.net.fetch_media.fetch_media` and
:func:`~tai42_kit.net.guarded_request.guarded_request` — so only a configuration's first
fetch on a loop pays client setup. The client follows no redirect and keeps no cookie: each fetch walks its own
redirect hops, carrying its own cookies across them. The client sets no timeout of its
own, so a request runs under httpx's default timeout unless its fetch passes one:
:func:`~tai42_kit.net.guarded_request.guarded_request` passes its caller's per request,
:func:`fetch_url` and :func:`~tai42_kit.net.fetch_media.fetch_media` pass none.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, aclosing, contextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, Final, cast

import httpcore
import httpx

from tai42_kit.clients.base import PooledClient
from tai42_kit.clients.facade import client_ctx
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


# ``httpx.AsyncHTTPTransport``'s own default connection limits.
_HTTPX_DEFAULT_LIMITS: Final = httpx.Limits(max_connections=100, max_keepalive_connections=20)


class PinningTransport(httpx.AsyncBaseTransport):
    """httpx transport over a connection pool whose network backend validates and pins every TCP target.

    Built through the libraries' public constructors: httpx's ``AsyncHTTPTransport`` takes no
    network backend, while httpcore's pool does. The pool's connection limits are ``limits``
    (``httpx.AsyncHTTPTransport``'s default unless given); its TLS context and backend are what
    ``httpx.AsyncHTTPTransport()`` builds by default, with :class:`_PinningBackend` wrapping the
    asyncio backend. The TLS context reads the environment's CA settings (``SSL_CERT_FILE``,
    ``SSL_CERT_DIR``) as httpx's default one does; environment proxies stay unused, since an
    ``httpx.AsyncClient`` given a transport reads none.
    """

    def __init__(self, *, limits: httpx.Limits = _HTTPX_DEFAULT_LIMITS) -> None:
        """Build the pinned pool with ``limits`` and ``httpx.AsyncHTTPTransport``'s default TLS context."""
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


# The shared client's pool caps no total connection count, so no fetch waits for a connection
# another fetch holds (a held media stream, a gather of many fetches); idle keep-alive
# connections stay bounded at httpx's default.
_SHARED_CLIENT_LIMITS: Final = httpx.Limits(
    max_connections=None, max_keepalive_connections=_HTTPX_DEFAULT_LIMITS.max_keepalive_connections
)


class _GuardedFetchClient(PooledClient[httpx.AsyncClient]):
    """The ``httpx.AsyncClient`` every guarded fetch of one configuration shares on its event loop.

    ``pinned`` selects the transport: the SSRF :class:`PinningTransport` while the guard is
    enabled, httpx's default one when it is not; both pools take :data:`_SHARED_CLIENT_LIMITS`.
    ``trust_env`` is httpx's own switch for the environment's proxy and CA settings, which
    httpx reads only for its default transport. The client follows no redirect, and its cookie
    jar admits no cookie, so nothing one fetch receives reaches another fetch's request.
    """

    async def _create(self, *, pinned: bool, trust_env: bool) -> httpx.AsyncClient:
        no_cookies = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
        if pinned:
            return httpx.AsyncClient(
                transport=PinningTransport(limits=_SHARED_CLIENT_LIMITS),
                follow_redirects=False,
                cookies=no_cookies,
                trust_env=trust_env,
            )
        return httpx.AsyncClient(
            limits=_SHARED_CLIENT_LIMITS, follow_redirects=False, cookies=no_cookies, trust_env=trust_env
        )

    async def _close(self, client: httpx.AsyncClient) -> None:
        await client.aclose()

    def _disconnection_exceptions(self) -> tuple[type[BaseException], ...]:
        # No fault leaves an httpx client unusable (its pool drops a broken connection itself), so
        # nothing raised inside a lease, the caller's own body included, evicts or rewraps it.
        return ()


def guarded_fetch_client(*, pinned: bool, trust_env: bool = True) -> AbstractAsyncContextManager[httpx.AsyncClient]:
    """Lease the shared guarded fetch client for ``pinned`` and ``trust_env`` on the running loop.

    The kit's client pool closes it when the loop's kit resources are released or its client
    epoch retires. Under the pin ``trust_env`` changes nothing: the client given the pinning
    transport reads no environment proxy, and the transport's TLS context reads the
    environment's CA settings either way, so every pinned lease shares one client.
    """
    return client_ctx(_GuardedFetchClient, pinned=pinned, trust_env=trust_env and not pinned)


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
    that opens a new connection), the body is size-capped while streaming, and at
    most the guard's ``max_redirects`` hops are followed. Raises rather than
    truncating an over-cap body or reaching a non-public host. When the guard is
    disabled the transport caps nothing and pins nothing, and the redirect bound is
    httpx's own (``httpx.TooManyRedirects`` past it).

    Redirects are followed one hop at a time on the shared client rather than by
    httpx, which reads each intermediate body into memory in full before chasing
    ``Location``. A hop carrying a body large enough to exhaust memory would slip
    past the cap that way, so each redirect response is closed unread — forgoing
    connection reuse for that hop — and only the terminal body is streamed and
    counted. The cookies a hop sets ride this fetch's later hops only.
    """
    pinned = url_guard.guard_enabled()
    try:
        async with guarded_fetch_client(pinned=pinned) as client:
            max_redirects = url_guard.url_guard_settings().max_redirects if pinned else client.max_redirects
            cookies = httpx.Cookies()
            request = client.build_request("GET", url)
            for _ in range(max_redirects + 1):
                async with aclosing(await client.send(request, stream=True)) as response:
                    cookies.extract_cookies(response)
                    # ``next_request`` is httpx's fully-built redirect request (relative
                    # ``Location`` resolved, method and headers adjusted per status). It is
                    # set only when the response is a redirect the client did not follow.
                    if response.next_request is not None:
                        request = response.next_request
                        cookies.set_cookie_header(request)
                        continue
                    response.raise_for_status()
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        buffer.extend(chunk)
                        if pinned:
                            url_guard.enforce_size(len(buffer))
                    return bytes(buffer), response.headers.get("Content-Type")
            if not pinned:
                raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=request)  # noqa: TRY301 -- httpx's own error for its own bound
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


__all__ = ["PinningTransport", "fetch_url", "guarded_fetch_client", "unwrap_guard_error"]
