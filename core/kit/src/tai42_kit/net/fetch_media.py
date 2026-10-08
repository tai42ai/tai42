"""Bounded, authenticated, SSRF-pinned streaming fetch for vendor media.

:func:`open_media_stream` is the streaming primitive: it opens a credentialed,
SSRF-pinned GET, follows redirects hop-by-hop, and yields a :class:`MediaStream`
whose ``chunks`` deliver the body as it arrives — the caller decides how (and how
far) to read it. :func:`fetch_media` is the bounded-buffer counterpart built on top of
it: it collects the chunks under a per-call ``max_bytes`` cap and returns
``(bytes, media type)``. Both are the vendor-media counterpart of
:func:`tai42_kit.net.fetch_url` and honour the same single operator switch,
:func:`tai42_kit.net.url_guard.guard_enabled`: while the guard is enabled the request
runs over the SSRF :class:`~tai42_kit.net.fetch_url.PinningTransport` (host validated
and pinned per hop); when the guard is disabled it runs over a plain transport that
pins nothing. Either way both add what a credentialed vendor fetch needs and the
public-only ``fetch_url`` lacks:

* per-call ``headers``/``auth`` carrying the vendor credential — passed on the call
  only, never returned, never logged. On a cross-origin redirect hop (one whose
  scheme/host/port differs from the previous request), and every hop after it, the
  request carries NO caller-supplied header and no ``auth`` — only httpx's own
  defaults; a same-origin hop keeps them. This is stricter than httpx's own redirect
  rule, which strips only ``Authorization`` and keeps even that on a same-host
  ``http``→``https`` hop (``httpx._client._redirect_headers`` at ``_client.py:546``,
  guarded by ``_is_https_redirect`` at ``_client.py:62``; ``_same_origin`` at
  ``_client.py:83``);
* URL-free faults: httpx's raw exceptions embed the request URL (and any credential in
  its path or query), so every fault carries the vendor HOST plus the HTTP status or
  the transport-exception class ONLY — at open, and while iterating the body.

The SUCCESS-path leak httpx opens on its own — it logs the full request line, URL
included, at INFO (``_client.py:1026`` / ``:1741``) — is closed at the logging seam by
:class:`tai42_kit.logging.logger._HttpxRequestLineRedactor`, not here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import httpx

from tai42_kit.net import url_guard
from tai42_kit.net.fetch_url import PinningTransport, unwrap_guard_error
from tai42_kit.net.request_body import RequestBodyTooLargeError
from tai42_kit.net.url_guard import UrlGuardError

#: Default ports per scheme, so an implicit and an explicit default port compare equal
#: when deciding whether a redirect hop crosses the origin (mirrors httpx's own
#: ``_port_or_default``, ``_client.py:77``).
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


class MediaFetchError(Exception):
    """A vendor media fetch failed at the HTTP or transport layer.

    The message carries the vendor HOST plus the HTTP status code or the
    transport-exception class ONLY — never the request URL — so a credential riding
    the URL path or query never reaches an error string. ``status_code`` is the HTTP
    status when the fault was an error response (or an unfollowed redirect), else
    ``None`` (a transport fault — timeout, connection error, a torn read — or a
    malformed response header). ``transient`` tells the caller whether a redelivery
    could plausibly succeed; it is derived from the status — a 5xx, a 408 (Request
    Timeout) or a 429 (Too Many Requests, a vendor throttle) is transient, and so is a
    transport fault (status ``None``); every other 4xx/3xx is permanent — unless an
    explicit value is given for a fault the status cannot classify (a malformed response
    header is permanent).
    """

    def __init__(
        self,
        *,
        host: str,
        status_code: int | None = None,
        cause_class: str | None = None,
        transient: bool | None = None,
    ) -> None:
        """Build a URL-free fault from the vendor ``host`` and either a ``status_code`` or a ``cause_class``."""
        self.host = host
        self.status_code = status_code
        self.cause_class = cause_class
        if transient is None:
            transient = status_code is None or status_code >= 500 or status_code in (408, 429)
        self.transient = transient
        if status_code is not None:
            detail = f"status {status_code}"
        elif cause_class is not None:
            detail = cause_class
        else:
            detail = "unknown error"
        super().__init__(f"media fetch from host {host!r} failed: {detail}")


@dataclass(frozen=True, slots=True)
class MediaStream:
    """A vendor media response opened for streaming — the final hop's metadata plus its body.

    ``content_type`` is the final response's ``Content-Type`` media type with parameters
    stripped and lower-cased (``None`` when the header is absent or empty).
    ``content_length`` is the parsed ``Content-Length`` (``None`` when absent). ``host`` is
    the final response's host, for error/log text only — never the URL. ``chunks`` yields
    the body as it arrives; a fault while iterating raises :class:`MediaFetchError`
    (``transient=True``, URL-free).
    """

    content_type: str | None
    content_length: int | None
    host: str
    chunks: AsyncIterator[bytes]


def _host(url: httpx.URL) -> str:
    """Return ``url``'s host for a fault message — never its path, query, or userinfo."""
    return url.host or "<unknown host>"


def _same_origin(a: httpx.URL, b: httpx.URL) -> bool:
    """Return whether two URLs share a scheme+host+port origin (default ports normalised)."""
    port_a = a.port if a.port is not None else _DEFAULT_PORTS.get(a.scheme)
    port_b = b.port if b.port is not None else _DEFAULT_PORTS.get(b.scheme)
    return a.scheme == b.scheme and a.host == b.host and port_a == port_b


def _redirect_target(response: httpx.Response, current_url: httpx.URL, *, follow_redirects: bool) -> httpx.URL:
    """Return the next hop's absolute URL, raising when a redirect must not be followed.

    An unfollowed redirect (``follow_redirects`` off) or a redirect with no ``Location``
    is a permanent fault carrying the status only — never the URL.
    """
    location = response.headers.get("Location")
    if not follow_redirects or location is None:
        raise MediaFetchError(host=_host(current_url), status_code=response.status_code)
    return current_url.join(location)


def _media_type(response: httpx.Response) -> str | None:
    """Return the response ``Content-Type`` media type, parameters stripped and lower-cased (``None`` when absent)."""
    raw = response.headers.get("Content-Type")
    if raw is None:
        return None
    return raw.split(";", 1)[0].strip().lower() or None


def _content_length(response: httpx.Response, host: str) -> int | None:
    """Return the parsed ``Content-Length`` (``None`` when absent); a malformed value is a permanent fault."""
    raw = response.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    # Raised OUTSIDE the ``except`` so the vendor's malformed header value is not chained
    # onto the URL-free fault the caller sees. A bad header is permanent — a redelivery
    # would carry the same one — so the fault is non-transient.
    raise MediaFetchError(host=host, cause_class="malformed Content-Length", transient=False)


def _transient_for_transport(exc: BaseException) -> bool | None:
    """Classify a transport fault's transience: an unsupported URL scheme is permanent, else unknown.

    An unsupported scheme (``file:``, ``data:``, …) is a permanently malformed URL — a
    redelivery would carry the same scheme — so it is non-transient. Every other transport
    fault (timeout, connection error, a torn read) is left for :class:`MediaFetchError` to
    derive as transient.
    """
    if isinstance(exc, httpx.UnsupportedProtocol):
        return False
    return None


async def _iter_body(response: httpx.Response, host: str) -> AsyncIterator[bytes]:
    """Yield the response body chunks, re-wrapping a mid-body fault as a URL-free transient error."""
    fault_class: str | None = None
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    except (MediaFetchError, UrlGuardError):
        raise
    except Exception as exc:
        fault_class = type(exc).__name__
    else:
        return
    # An httpx read fault carries the request URL in its text. Re-wrap it OUTSIDE the
    # ``except`` block so that URL-bearing exception is neither chained as ``__cause__``
    # nor left on ``__context__`` of the redacted error the caller sees.
    raise MediaFetchError(host=host, cause_class=fault_class)


@asynccontextmanager
async def _stream_media(
    url: str,
    *,
    headers: Mapping[str, str] | None,
    auth: httpx.Auth | tuple[str, str] | None,
    follow_redirects: bool,
) -> AsyncIterator[MediaStream]:
    """Open the SSRF-pinned GET, follow redirects hop-by-hop, and yield the final response as a stream."""
    max_redirects = url_guard.url_guard_settings().max_redirects
    transport = PinningTransport() if url_guard.guard_enabled() else None
    base_headers = dict(headers) if headers is not None else {}
    current_url = httpx.URL(url)
    credentials_dropped = False

    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(httpx.AsyncClient(transport=transport, follow_redirects=False))

        async def _open() -> httpx.Response:
            nonlocal current_url, credentials_dropped
            for _ in range(max_redirects + 1):
                hop_headers: dict[str, str] = {} if credentials_dropped else base_headers
                hop_auth = None if credentials_dropped else auth
                # The final response stays open (in the exit stack) while the caller reads its
                # ``chunks``; a redirect hop is closed the instant it is followed so its
                # connection is not held for the life of the stream.
                response = await stack.enter_async_context(
                    client.stream("GET", current_url, headers=hop_headers, auth=hop_auth)
                )
                if response.is_redirect:
                    await response.aclose()
                    next_url = _redirect_target(response, current_url, follow_redirects=follow_redirects)
                    if not _same_origin(next_url, current_url):
                        credentials_dropped = True
                    current_url = next_url
                    continue
                if response.status_code >= 400:
                    raise MediaFetchError(host=_host(current_url), status_code=response.status_code)
                return response
            raise UrlGuardError(f"SSRF guard: exceeded max_redirects={max_redirects}.")

        response: httpx.Response | None = None
        transport_class: str | None = None
        transport_transient: bool | None = None
        try:
            response = await _open()
        except (MediaFetchError, UrlGuardError):
            # Already redacted (host only, never the URL); surface it unchanged rather than
            # letting the transport re-wrap below catch it.
            raise
        except Exception as exc:
            guard_error = unwrap_guard_error(exc)
            if guard_error is not None:
                # Re-raise the guard's own rejection (host only) rather than whatever httpx
                # exception is carrying it.
                raise guard_error from guard_error.__context__
            transport_class = type(exc).__name__
            transport_transient = _transient_for_transport(exc)
        if response is None:
            # An httpx transport fault (timeout, connection error, a torn read) carries the
            # request URL in its text. Re-wrap it OUTSIDE the ``except`` block so that
            # URL-bearing exception is neither chained as ``__cause__`` nor left on
            # ``__context__`` of the redacted error the caller sees.
            raise MediaFetchError(host=_host(current_url), cause_class=transport_class, transient=transport_transient)

        host = _host(current_url)
        yield MediaStream(
            content_type=_media_type(response),
            content_length=_content_length(response, host),
            host=host,
            chunks=_iter_body(response, host),
        )


@asynccontextmanager
async def open_media_stream(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    auth: tuple[str, str] | None = None,
    follow_redirects: bool = False,
) -> AsyncIterator[MediaStream]:
    """Open a vendor media object for streaming, SSRF-pinned while the guard is enabled.

    While :func:`~tai42_kit.net.url_guard.guard_enabled` is set the request runs over the
    :class:`~tai42_kit.net.fetch_url.PinningTransport`; when it is unset a plain transport
    that pins nothing is used instead — the same one operator switch ``fetch_url`` honours.
    When ``follow_redirects`` is set, redirects are followed hop-by-hop up to the guard's
    ``max_redirects`` (``follow_redirects`` is never handed to httpx, so the pin re-runs per
    hop while enabled); when it is not, an unfollowed redirect is a fault. ``headers``/``auth``
    carry the vendor credential on the call only — never returned, never logged; ``auth`` is a
    Basic-auth ``(username, password)`` pair only. On a cross-origin hop, and every hop after
    it, the request carries no caller-supplied header and no ``auth`` (only httpx's defaults); a
    same-origin hop keeps them.

    Yields a :class:`MediaStream` for the final response; the response is closed when the
    context exits, including on an exception raised while iterating ``chunks``. Faults never
    carry the request URL: an SSRF rejection surfaces as
    :class:`~tai42_kit.net.url_guard.UrlGuardError` (host only); an HTTP-status, transport,
    or malformed-header fault as :class:`MediaFetchError` (vendor host + status/class only) —
    at open, or from the ``chunks`` iterator on a mid-body fault.
    """
    async with _stream_media(url, headers=headers, auth=auth, follow_redirects=follow_redirects) as stream:
        yield stream


async def fetch_media(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    auth: httpx.Auth | tuple[str, str] | None = None,
    follow_redirects: bool = False,
    max_bytes: int,
) -> tuple[bytes, str | None]:
    """Fetch a vendor media object bounded by ``max_bytes``, SSRF-pinned while the guard is enabled.

    Shares the streaming primitive ``_stream_media`` with :func:`open_media_stream` and collects
    its body, raising :class:`~tai42_kit.net.request_body.RequestBodyTooLargeError` (never
    truncating) the instant the running total crosses ``max_bytes``. Redirect handling, the
    cross-origin credential drop, and the URL-free faults are exactly those of
    ``open_media_stream``. Returns ``(bytes, media type)`` where the media type is the response
    ``Content-Type`` with parameters stripped and lower-cased (``None`` when absent).
    """
    async with _stream_media(url, headers=headers, auth=auth, follow_redirects=follow_redirects) as stream:
        buffer = bytearray()
        async for chunk in stream.chunks:
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise RequestBodyTooLargeError(f"media body exceeds the {max_bytes}-byte cap")
        return bytes(buffer), stream.content_type
