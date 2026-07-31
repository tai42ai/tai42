"""Request execution for the ``request`` tool, backed by tai42-kit's pooled curl client.

Serializes a curl response to a plain dict, decodes bodies against the declared charset,
and — when the SSRF guard is on — pins curl to the validated address and streams the body
under the size cap. Needs the ``http`` extra; missing it raises a loud install hint at import.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from urllib.parse import urlparse

try:
    from curl_cffi import CurlOpt
    from tai42_kit.clients.impl.curl import CurlClient
except ImportError as exc:
    raise ImportError(
        "tai42-toolbox 'http' tool requires the 'http' optional dependency "
        "(tai42-kit[curl]). "
        "Install it with: pip install 'tai42-toolbox[http]'"
    ) from exc

from tai42_contract.app import tai42_app
from tai42_kit.net import url_guard

# Body fields (``content``/``text``) are set explicitly so the streaming path can supply an already-read body.
_RESPONSE_FIELDS = (
    "url",
    "status_code",
    "reason",
    "ok",
    "encoding",
    "charset",
    "charset_encoding",
    "primary_ip",
    "primary_port",
    "local_ip",
    "local_port",
    "redirect_count",
    "redirect_url",
    "http_version",
    "download_size",
    "upload_size",
    "header_size",
    "request_size",
    "response_size",
)


def _decode_body(body: bytes, resp: Any) -> str:
    """Decode ``body`` to text using the response's declared charset.

    Falls back through the default encoding to UTF-8, always replacing undecodable bytes. An
    unknown codec name is caught so a bad ``Content-Type`` degrades to a safe decode, never raises.
    """
    for label in (resp.charset, resp.encoding, getattr(resp, "default_encoding", None), "utf-8"):
        if not label:
            continue
        try:
            return body.decode(label, errors="replace")
        except LookupError:
            continue
    return body.decode("utf-8", errors="replace")


def _serialize_response(
    resp: Any,
    body: bytes | None = None,
    elapsed: float | None = None,
    download_size: int | None = None,
    response_size: int | None = None,
) -> dict[str, Any]:
    """Serialize a curl response to a plain dict.

    When ``body`` is given (the streaming path), ``content``/``text`` come from it, and the
    passed ``elapsed``/``download_size``/``response_size`` override curl's metrics (which reflect
    only time-to-first-byte). Otherwise body and metrics are read from the response directly.
    """
    data = {field: value for field in _RESPONSE_FIELDS if (value := getattr(resp, field, None)) is not None}
    if body is None:
        data["content"] = resp.content
        data["text"] = resp.text
    else:
        data["content"] = body
        data["text"] = _decode_body(body, resp)
    if download_size is not None:
        data["download_size"] = download_size
    if response_size is not None:
        data["response_size"] = response_size
    data.update(
        {
            "headers": dict(resp.headers),
            "cookies": dict(resp.cookies),
            "history": [r.url for r in resp.history],
            "elapsed": elapsed if elapsed is not None else resp.elapsed.total_seconds(),
        }
    )
    return data


async def _validate_proxies(session_params: dict[str, Any]) -> list[str]:
    """Resolve and validate every proxy host in ``session_params``, returning the curl
    ``--resolve`` pins that hold each proxy to its validated address.

    A proxy carried as ``proxy`` (URL) or ``proxies`` (scheme-to-URL map) is caller-supplied,
    so each host is resolved and validated (a rejected host raises ``UrlGuardError``) and pinned
    with a ``host:port:address`` entry — closing DNS-rebinding on the proxy hop, since validation
    alone leaves curl free to re-resolve at connect. Port is from the URL or the scheme default
    (https 443, socks 1080, else 80).
    """
    proxy_urls: list[str] = []
    proxies = session_params.get("proxies")
    if isinstance(proxies, dict):
        proxy_urls.extend(value for value in proxies.values() if value)
    proxy = session_params.get("proxy")
    if proxy:
        proxy_urls.append(proxy)
    pins: list[str] = []
    for proxy_url in proxy_urls:
        parsed = urlparse(proxy_url)
        proxy_host = parsed.hostname
        if not proxy_host:
            raise url_guard.UrlGuardError(
                f"SSRF guard: proxy URL has no host to check: {_redact_userinfo(proxy_url)!r}"
            )
        scheme = parsed.scheme.lower()
        if scheme.startswith("socks"):
            default_port = 1080
        elif scheme == "https":
            default_port = 443
        else:
            default_port = 80
        # Read the port before resolving so a malformed one fails fast with a domain-specific error.
        try:
            explicit_port = parsed.port
        except ValueError as exc:
            raise url_guard.UrlGuardError(
                f"SSRF guard: proxy URL has an out-of-range port: {_redact_userinfo(proxy_url)!r}"
            ) from exc
        proxy_port = explicit_port or default_port
        validated_ip = await url_guard.resolve_and_validate(proxy_host)
        pins.append(_resolve_pin_entry(proxy_host, proxy_port, validated_ip))
    return pins


def _redact_userinfo(url: str) -> str:
    """Return ``url`` with any ``user:pass@`` credentials replaced by ``***@`` so a URL echoed
    in an error never leaks the caller's credentials. Host and everything after it are preserved."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    netloc, slash, tail = rest.partition("/")
    _creds, at, hostport = netloc.rpartition("@")
    if not at:
        return url
    return f"{scheme}://***@{hostport}{slash}{tail}"


def _resolve_pin_entry(host: str, port: int, validated_ip: str) -> str:
    """Build curl's ``--resolve`` entry pinning ``host:port`` to the validated address. An IPv6
    literal on either side is bracketed so its colons don't collide with the triple's separators."""
    pinned_host = f"[{host}]" if ":" in host else host
    pinned_ip = f"[{validated_ip}]" if ":" in validated_ip else validated_ip
    return f"{pinned_host}:{port}:{pinned_ip}"


# A pooled ``CurlClient`` shares one ``AsyncSession`` across same-``session_key`` callers, and
# curl reads the RESOLVE pin only after an await inside ``request``; concurrent same-key requests
# would race on the shared pin, unpinning a host and reopening DNS-rebinding. A per-``session_key``
# lock held across [set RESOLVE + request + read body] serializes them so each keeps its own pin.
# The registry is keyed by running loop (an ``asyncio.Lock`` binds to its first loop). Each entry
# is a mutable ``[lock, refcount]`` evicted the moment the refcount hits zero, so the map shrinks
# to empty when idle.
_session_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, list[Any]]] = weakref.WeakKeyDictionary()


@asynccontextmanager
async def _session_guard(session_key: str) -> AsyncIterator[None]:
    """Serialize same-``session_key`` requests on the current loop under a refcounted lock
    evicted once no holder or waiter references it.

    Refcount mutations are synchronous (no await between check and mutate), so concurrent guards
    for one key cannot interleave their bookkeeping.
    """
    loop = asyncio.get_running_loop()
    locks = _session_locks.get(loop)
    if locks is None:
        locks = {}
        _session_locks[loop] = locks
    entry = locks.get(session_key)
    if entry is None:
        entry = [asyncio.Lock(), 0]
        locks[session_key] = entry
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            locks.pop(session_key, None)


async def _perform_pinned(
    session: Any, url: str, method: str, pin_entries: list[str], request_params: dict[str, Any]
) -> dict[str, Any]:
    """Pin curl to the validated addresses, then stream the size-capped body.

    Curl's ``--resolve`` mapping makes it connect to the validated address instead of re-resolving,
    closing DNS-rebinding; ``pin_entries`` holds the destination pin plus one per caller-supplied
    proxy, so every hop connects to the validated address. The body is size-capped chunk by chunk,
    refused the moment it crosses the limit. Full-body ``elapsed``/``download_size``/``response_size``
    are measured here and override curl's time-to-first-byte metrics.
    """
    session.curl_options = {**session.curl_options, CurlOpt.RESOLVE: pin_entries}
    start = time.perf_counter()
    resp = await session.request(url=url, method=cast("Any", method.upper()), stream=True, **request_params)
    try:
        buffer = bytearray()
        async for chunk in resp.aiter_content():
            buffer.extend(chunk)
            url_guard.enforce_size(len(buffer))
    finally:
        await resp.aclose()
    elapsed = time.perf_counter() - start
    download_size = len(buffer)
    response_size = (getattr(resp, "header_size", None) or 0) + download_size
    return _serialize_response(
        resp,
        body=bytes(buffer),
        elapsed=elapsed,
        download_size=download_size,
        response_size=response_size,
    )


async def perform_request(
    url: str,
    method: str = "GET",
    session_key: str | None = None,
    clear_session_cookies: bool = True,
    session_params: dict[str, Any] | None = None,
    request_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute an HTTP request through a curl-backed session and return the
    response data (status, body, headers, cookies, timing, and transfer metrics)."""
    session_params = dict(session_params or {})
    request_params = dict(request_params or {})

    guard_on = url_guard.guard_enabled()
    if guard_on:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            raise url_guard.UrlGuardError(f"SSRF guard: URL has no host to check: {url!r}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        # Resolve and validate once, then pin curl to this exact address below.
        validated_ip = await url_guard.resolve_and_validate(host)
        request_params["allow_redirects"] = False
        # Validate every proxy host and capture its pin so the proxy hop is validated too.
        proxy_pins = await _validate_proxies(session_params)

    if session_key is not None:
        session_params["session_key"] = session_key
        session_ctx = tai42_app.clients.client_ctx(CurlClient, session_params=session_params)
    else:
        session_ctx = tai42_app.clients.client_ctx(CurlClient, session_params=session_params, fresh=True)

    async with session_ctx as session:
        if clear_session_cookies:
            session.cookies.clear()
        if not guard_on:
            resp = await session.request(url=url, method=cast("Any", method.upper()), **request_params)
            return _serialize_response(resp)

        # Set the pin per request (a pooled session reused across hosts pins each to its own
        # validated address): the destination pin plus one per caller-supplied proxy.
        pin_entries = [_resolve_pin_entry(host, port, validated_ip), *proxy_pins]

        # A pooled (keyed) session is shared, so the pins and the request-plus-body-read run under
        # the per-key lock (see ``_session_guard``). A fresh (keyless) session is never shared.
        if session_key is not None:
            async with _session_guard(session_key):
                return await _perform_pinned(session, url, method, pin_entries, request_params)
        return await _perform_pinned(session, url, method, pin_entries, request_params)
