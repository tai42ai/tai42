"""One SSRF-guarded HTTP request to a URL a remote party named: no redirects, a size cap, one total deadline.

:func:`guarded_request` sends a single GET/POST/DELETE. While the URL guard is enabled the connection
runs over :class:`~tai42_kit.net.fetch_url.PinningTransport`, so the target host is resolved, refused
when it is non-public (unless ``TAI_URL_GUARD_ALLOW_CIDRS`` admits it), and connected to at the
validated address. Whatever the guard setting, ``https`` is required unless the host is loopback, a
3xx answer is returned to the caller rather than followed, the body is capped while it streams, and
``timeout`` is ONE deadline for the whole call — resolve, connect, send and read together — so a
server that drips its answer is cut off exactly like a silent one. Error text never carries the URL's
query, a request header value or a request body.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from tai42_kit.net import url_guard
from tai42_kit.net.fetch_url import PinningTransport, unwrap_guard_error
from tai42_kit.net.url_guard import UrlGuardError, is_loopback_host


class GuardedRequestError(Exception):
    """A guarded request that reached no usable answer: a transport failure or a body over the cap."""


class GuardedRequestTimeoutError(GuardedRequestError):
    """A guarded request that did not complete within its total deadline."""


@dataclass(frozen=True)
class GuardedResponse:
    """The complete answer to a guarded request: status, headers and the whole (capped) body."""

    status: int
    headers: httpx.Headers
    body: bytes


def _display_url(url: httpx.URL) -> str:
    """``scheme://host[:port]/path`` — the URL without its query, fragment or userinfo."""
    port = f":{url.port}" if url.port is not None else ""
    return f"{url.scheme}://{url.host}{port}{url.path}"


def _redact(text: str, url: httpx.URL) -> str:
    query = url.query.decode("ascii", errors="replace")
    return text.replace(query, "<query>") if query else text


async def _send(
    method: str,
    url: httpx.URL,
    *,
    headers: Mapping[str, str] | None,
    form: Mapping[str, str] | None,
    json_body: Any,
    max_bytes: int,
    timeout: float,
) -> GuardedResponse:
    # The phase timers are the same length and start after the total deadline, so on a responsive
    # loop the total deadline ends a slow call; either error is a GuardedRequestError.
    phases = httpx.Timeout(timeout)
    if url_guard.guard_enabled():
        client = httpx.AsyncClient(transport=PinningTransport(), follow_redirects=False, timeout=phases)
    else:
        client = httpx.AsyncClient(follow_redirects=False, timeout=phases, trust_env=False)
    async with (
        client,
        client.stream(method, url, headers=dict(headers or {}), data=form, json=json_body) as response,
    ):
        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise GuardedRequestError(f"{method} {_display_url(url)}: response body exceeds max_bytes={max_bytes}")
        return GuardedResponse(status=response.status_code, headers=response.headers, body=bytes(buffer))


async def guarded_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    form: Mapping[str, str] | None = None,
    json_body: Any = None,
    timeout: float,
    max_bytes: int,
) -> GuardedResponse:
    """Send one guarded request and return its complete answer, whatever its status.

    Raises :class:`~tai42_kit.net.url_guard.UrlGuardError` when the URL is refused (not ``https`` on
    a non-loopback host, or a host the guard blocks), :class:`GuardedRequestTimeoutError` when the
    whole call outlasts ``timeout`` seconds, and :class:`GuardedRequestError` on a transport failure
    or a body over ``max_bytes``. ``form`` is sent form-encoded, ``json_body`` as JSON; at most one.
    """
    if form is not None and json_body is not None:
        raise ValueError("guarded_request takes form or json_body, not both")
    parsed = httpx.URL(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback_host(parsed.host)):
        raise UrlGuardError(f"refused {parsed.scheme}://{parsed.host}: https is required unless the host is loopback")
    try:
        async with asyncio.timeout(timeout):
            return await _send(
                method, parsed, headers=headers, form=form, json_body=json_body, max_bytes=max_bytes, timeout=timeout
            )
    except TimeoutError:
        raise GuardedRequestTimeoutError(
            f"{method} {_display_url(parsed)}: no complete answer within {timeout}s"
        ) from None
    except (UrlGuardError, GuardedRequestError):
        raise
    except Exception as exc:
        guard_error = unwrap_guard_error(exc)
        if guard_error is not None:
            raise guard_error from guard_error.__context__
        if not isinstance(exc, httpx.HTTPError):
            raise
        detail = _redact(str(exc), parsed)
        raise GuardedRequestError(f"{method} {_display_url(parsed)}: {type(exc).__name__}: {detail}") from exc


__all__ = ["GuardedRequestError", "GuardedRequestTimeoutError", "GuardedResponse", "guarded_request"]
