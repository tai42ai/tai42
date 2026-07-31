"""SSRF-guarded server-side URL fetching.

The SSRF guard (:mod:`tai42_kit.net.url_guard`) resolves and validates each target
host, and :func:`fetch_url` is the httpx download built on it: while the guard is
enabled (the default) that download is DNS-rebinding-safe, redirect-safe, and
size-capped. The guard is reusable on its own by any caller that fetches a
caller-supplied URL server-side over its own HTTP client.
"""

from tai42_kit.net.fetch_url import fetch_url
from tai42_kit.net.url_guard import (
    UrlGuardError,
    UrlGuardSettings,
    enforce_size,
    guard_enabled,
    resolve_and_validate,
    url_guard_settings,
)

__all__ = [
    "UrlGuardError",
    "UrlGuardSettings",
    "enforce_size",
    "fetch_url",
    "guard_enabled",
    "resolve_and_validate",
    "url_guard_settings",
]
