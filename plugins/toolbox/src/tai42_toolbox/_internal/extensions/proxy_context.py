"""Proxy configuration parsing for the ``proxy`` tool extension.

Turns a chosen proxy URL into a :class:`RouteConfig` the routing core dispatches on.
The pool source and selection policy live in :class:`ProxySettings`:

- ``allow_caller_urls=False`` (default): the ``proxies`` kwarg may only select entries
  already in the operator pool; omitted, it rotates over the pool. An out-of-pool URL is
  refused loudly.
- ``allow_caller_urls=True``: the operator opts into caller-supplied URLs.

The operator pool is trusted-by-configuration and skips the SSRF guard; a caller-supplied
host is resolved and validated (see :func:`build_route`).
"""

from __future__ import annotations

import random
from urllib.parse import urlparse, urlunparse

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.net import url_guard
from tai42_kit.settings import TaiBaseSettings, settings_cache

from tai42_toolbox._internal.extensions.socket_routing import RouteConfig, load_socks


class ProxySettings(TaiBaseSettings):
    """Operator proxy pool and routing policy (env prefix ``PROXY_``)."""

    model_config = SettingsConfigDict(env_prefix="PROXY_")

    pool: list[str] = Field(default_factory=list)
    allow_caller_urls: bool = False
    # A non-positive timeout would silently break every proxied connect.
    connect_timeout: int = Field(default=30, gt=0)


@settings_cache
def proxy_settings() -> ProxySettings:
    return ProxySettings()


def _redact_userinfo(proxy_url: str) -> str:
    """Return ``proxy_url`` with any ``user:pass@`` credentials replaced by ``***@`` so an
    error echoing the URL never leaks a proxy password. Operates on the raw netloc."""
    parsed = urlparse(proxy_url)
    if "@" not in parsed.netloc:
        return proxy_url
    _userinfo, _, hostport = parsed.netloc.rpartition("@")
    return urlunparse(parsed._replace(netloc=f"***@{hostport}"))


def _select_proxy_url(proxies: list[str] | None, settings: ProxySettings) -> str:
    """Pick one proxy URL to route through, enforcing the pool policy.

    Omitted (or empty) ``proxies`` rotates over the operator pool. When caller URLs
    are not allowed, every explicitly-passed URL must already be in the pool — an
    out-of-pool URL is refused loudly, nothing routed.
    """
    if not proxies:
        candidates = settings.pool
    elif settings.allow_caller_urls:
        candidates = proxies
    else:
        for candidate in proxies:
            if candidate not in settings.pool:
                raise ValueError(
                    f"Proxy URL {_redact_userinfo(candidate)!r} is not in the operator pool and "
                    "PROXY_ALLOW_CALLER_URLS is not set; refusing to route through a caller-supplied proxy."
                )
        candidates = proxies

    if not candidates:
        raise ValueError("No proxies available")
    return random.choice(candidates)


async def build_route(proxies: list[str] | None) -> RouteConfig:
    """Select a proxy URL and parse it into a :class:`RouteConfig`.

    A caller-supplied proxy host (a selected URL not in the operator pool) is run
    through the SSRF guard when the guard is enabled, and the route connects to the
    validated address. Operator-pool URLs are trusted-by-configuration and skip the
    guard.
    """
    settings = proxy_settings()
    proxy_url = _select_proxy_url(proxies, settings)
    caller_supplied = proxy_url not in settings.pool

    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    if not host:
        raise ValueError(f"Proxy URL has no host: {_redact_userinfo(proxy_url)!r}")
    # ``.port`` raises a bare "Port out of range"; surface a domain-specific error instead.
    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Proxy URL has an out-of-range port: {_redact_userinfo(proxy_url)!r}") from exc

    if scheme.startswith("socks"):
        socks = load_socks()
        if scheme.startswith("socks5"):
            socks_type = socks.SOCKS5
            rdns = scheme == "socks5h"
        else:
            socks_type = socks.SOCKS4
            rdns = scheme == "socks4a"
        port = explicit_port or 1080
        is_socks = True
        is_https = False
    elif scheme in ("http", "https"):
        socks_type = None
        # Remote DNS: let the proxy resolve the destination so the visited host doesn't leak locally.
        rdns = True
        port = explicit_port or (443 if scheme == "https" else 80)
        is_socks = False
        is_https = scheme == "https"
    else:
        raise ValueError(f"Unsupported proxy scheme: {scheme}. Supported: socks5(h), socks4(a), http, https.")

    connect_address = host
    if caller_supplied and url_guard.guard_enabled():
        # Connect to the validated address, never re-resolving the hostname (closes DNS rebinding).
        connect_address = await url_guard.resolve_and_validate(host)

    return RouteConfig(
        is_socks=is_socks,
        is_https=is_https,
        proxy_host=host,
        proxy_port=port,
        connect_address=connect_address,
        username=parsed.username,
        password=parsed.password,
        rdns=rdns,
        connect_timeout=settings.connect_timeout,
        socks_type=socks_type,
    )
