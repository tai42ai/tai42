"""SSRF-guarded server-side URL fetching.

The SSRF guard (:mod:`tai42_kit.net.url_guard`) resolves and validates each target
host, and :func:`fetch_url` is the httpx download built on it: while the guard is
enabled (the default) that download is DNS-rebinding-safe, redirect-safe, and
size-capped. The guard is reusable on its own by any caller that fetches a
caller-supplied URL server-side over its own HTTP client.

:func:`fetch_url` and :func:`fetch_media` are re-exported LAZILY (PEP 562): they pull in
``httpx``/``httpcore``, and a consumer of a lighter ``net`` utility (e.g.
:mod:`tai42_kit.net.request_body`) must not drag that backend into its import graph merely
by importing a sibling module. This is the same reason :mod:`tai42_kit.net.jwt` (which
needs ``joserfc``) stays out of this re-export.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_kit.net.url_guard import (
    UrlGuardError,
    UrlGuardSettings,
    enforce_size,
    guard_enabled,
    resolve_and_validate,
    url_guard_settings,
)

if TYPE_CHECKING:
    from tai42_kit.net.fetch_media import MediaFetchError, MediaStream, fetch_media, open_media_stream
    from tai42_kit.net.fetch_url import fetch_url

__all__ = [
    "MediaFetchError",
    "MediaStream",
    "UrlGuardError",
    "UrlGuardSettings",
    "enforce_size",
    "fetch_media",
    "fetch_url",
    "guard_enabled",
    "open_media_stream",
    "resolve_and_validate",
    "url_guard_settings",
]


def __getattr__(name: str) -> object:
    """Load the ``httpx``-backed fetch helpers and media symbols on first access.

    :func:`fetch_url`, :func:`fetch_media`, :func:`open_media_stream`,
    :class:`MediaFetchError` and :class:`MediaStream` are bound lazily so that
    importing a lighter ``net`` utility never pulls in the ``httpx``/``httpcore``
    backend they ride on.

    All the names are cached in the module globals together, so the FUNCTION or CLASS (not
    the same-named submodule that importing it binds on the package) is what ``net.<name>``
    resolves to thereafter — the shape the eager re-export gave — whichever name is touched
    first.

    Import the fetch helpers only as ``from tai42_kit.net import fetch_url`` /
    ``fetch_media`` (this package), never as the submodule ``tai42_kit.net.fetch_url``: a
    direct submodule import binds the MODULE onto the package's attribute, so this hook
    never runs and a later package-level import yields the module instead of the function.
    """
    if name in ("fetch_url", "fetch_media", "open_media_stream", "MediaFetchError", "MediaStream"):
        # Loading ``fetch_media`` imports the ``fetch_url`` submodule too, which binds that
        # MODULE onto this package; rebinding all the names together on the first access of
        # any of them keeps every later ``from tai42_kit.net import fetch_url`` a function.
        from tai42_kit.net.fetch_media import MediaFetchError, MediaStream, fetch_media, open_media_stream
        from tai42_kit.net.fetch_url import fetch_url

        globals()["fetch_url"] = fetch_url
        globals()["fetch_media"] = fetch_media
        globals()["open_media_stream"] = open_media_stream
        globals()["MediaFetchError"] = MediaFetchError
        globals()["MediaStream"] = MediaStream
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
