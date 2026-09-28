"""Bounded, authenticated, SSRF-pinned streaming fetch for vendor media.

:func:`fetch_media` is the vendor-media counterpart of :func:`tai42_kit.net.fetch_url`.
It honours the same single operator switch, :func:`tai42_kit.net.url_guard.guard_enabled`:
while the guard is enabled it runs over the SSRF
:class:`~tai42_kit.net.fetch_url._PinningTransport` (host validated and pinned per hop);
when the guard is disabled it runs over a plain transport that pins nothing. Either way it
follows redirects hop-by-hop and adds what a credentialed vendor fetch needs and the
public-only ``fetch_url`` lacks:

* per-call ``headers``/``auth`` carrying the vendor credential — passed on the call
  only, never returned, never logged. On a cross-origin redirect hop (one whose
  scheme/host/port differs from the previous request), and every hop after it, the request
  carries NO caller-supplied header and no ``auth`` — only httpx's own defaults; a
  same-origin hop keeps them. This is stricter than httpx's own redirect rule, which strips
  only ``Authorization`` and keeps even that on a same-host ``http``→``https`` hop
  (``httpx._client._redirect_headers`` at ``_client.py:546``, guarded by
  ``_is_https_redirect`` at ``_client.py:62``; ``_same_origin`` at ``_client.py:83``);
* a per-call ``max_bytes`` cap enforced while streaming — the read stops and raises the
  instant the running total crosses it, so nothing is ever truncated into a shorter body;
* URL-free faults: httpx's raw exceptions embed the request URL (and any credential in
  its path or query), so every fault :func:`fetch_media` raises carries the vendor HOST
  plus the HTTP status or the transport-exception class ONLY.

The SUCCESS-path leak httpx opens on its own — it logs the full request line, URL
included, at INFO (``_client.py:1026`` / ``:1741``) — is closed at the logging seam by
:class:`tai42_kit.logging.logger._HttpxRequestLineRedactor`, not here.
"""

from __future__ import annotations

import httpx

from tai42_kit.net import url_guard
from tai42_kit.net.fetch_url import _PinningTransport, _unwrap_guard_error
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
    ``None`` (a transport fault — timeout, connection error, a torn read).
    ``transient`` tells the caller whether a redelivery could plausibly succeed.
    """

    def __init__(self, *, host: str, status_code: int | None = None, cause_class: str | None = None) -> None:
        """Build a URL-free fault from the vendor ``host`` and either a ``status_code`` or a ``cause_class``."""
        self.host = host
        self.status_code = status_code
        self.cause_class = cause_class
        if status_code is not None:
            detail = f"status {status_code}"
        elif cause_class is not None:
            detail = cause_class
        else:
            detail = "unknown error"
        super().__init__(f"media fetch from host {host!r} failed: {detail}")

    @property
    def transient(self) -> bool:
        """Whether a redelivery could plausibly succeed — a transport fault or a 5xx."""
        return self.status_code is None or self.status_code >= 500


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


async def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, str | None]:
    """Stream the body into a buffer, raising the instant it crosses ``max_bytes`` (never truncating)."""
    buffer = bytearray()
    async for chunk in response.aiter_bytes():
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise RequestBodyTooLargeError(f"media body exceeds the {max_bytes}-byte cap")
    return bytes(buffer), response.headers.get("Content-Type")


async def fetch_media(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    auth: httpx.Auth | tuple[str, str] | None = None,
    follow_redirects: bool = False,
    max_bytes: int,
) -> tuple[bytes, str | None]:
    """Fetch a vendor media object bounded by ``max_bytes``, SSRF-pinned while the guard is enabled.

    While :func:`~tai42_kit.net.url_guard.guard_enabled` is set the request runs over the
    :class:`~tai42_kit.net.fetch_url._PinningTransport`; when it is unset a plain transport
    that pins nothing is used instead — the same one operator switch ``fetch_url`` honours.
    Streams the body in chunks, raising :class:`~tai42_kit.net.request_body.RequestBodyTooLargeError`
    (never truncating) the instant the running total crosses ``max_bytes``. When
    ``follow_redirects`` is set, redirects are followed hop-by-hop up to the guard's
    ``max_redirects`` (``follow_redirects`` is never handed to httpx, so the pin re-runs per
    hop while enabled); when it is not, an unfollowed redirect is a fault. ``headers``/``auth``
    carry the vendor credential on the call only — never returned, never logged. On a
    cross-origin hop, and every hop after it, the request carries no caller-supplied header
    and no ``auth`` (only httpx's defaults); a same-origin hop keeps them.

    Faults never carry the request URL: an SSRF rejection surfaces as
    :class:`~tai42_kit.net.url_guard.UrlGuardError` (host only), an over-cap body as
    ``RequestBodyTooLargeError``, and an HTTP-status or transport fault as
    :class:`MediaFetchError` (vendor host + status/class only). Returns
    ``(bytes, Content-Type)``.
    """
    max_redirects = url_guard.url_guard_settings().max_redirects
    transport = _PinningTransport() if url_guard.guard_enabled() else None
    base_headers = dict(headers) if headers is not None else {}
    current_url = httpx.URL(url)
    credentials_dropped = False

    async def _stream() -> tuple[bytes, str | None]:
        nonlocal current_url, credentials_dropped
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            for _ in range(max_redirects + 1):
                hop_headers: dict[str, str] = {} if credentials_dropped else base_headers
                hop_auth = None if credentials_dropped else auth
                async with client.stream("GET", current_url, headers=hop_headers, auth=hop_auth) as response:
                    if response.is_redirect:
                        next_url = _redirect_target(response, current_url, follow_redirects=follow_redirects)
                        if not _same_origin(next_url, current_url):
                            credentials_dropped = True
                        current_url = next_url
                        continue
                    if response.status_code >= 400:
                        raise MediaFetchError(host=_host(current_url), status_code=response.status_code)
                    return await _read_capped(response, max_bytes)
            raise UrlGuardError(f"SSRF guard: exceeded max_redirects={max_redirects}.")

    transport_class: str | None = None
    try:
        return await _stream()
    except (RequestBodyTooLargeError, MediaFetchError, UrlGuardError):
        # Already redacted (host only, never the URL); surface it unchanged rather than
        # letting the transport re-wrap below catch it.
        raise
    except Exception as exc:
        guard_error = _unwrap_guard_error(exc)
        if guard_error is not None:
            # Re-raise the guard's own rejection (host only) rather than whatever httpx
            # exception is carrying it.
            raise guard_error from guard_error.__context__
        transport_class = type(exc).__name__
    # An httpx transport fault (timeout, connection error, a torn read) carries the
    # request URL in its text. Re-wrap it OUTSIDE the ``except`` block so that
    # URL-bearing exception is neither chained as ``__cause__`` nor left on
    # ``__context__`` of the redacted error the caller sees.
    raise MediaFetchError(host=_host(current_url), cause_class=transport_class)
