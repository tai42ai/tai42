"""SSRF-guarded server-side URL fetching.

The SSRF guard (:mod:`tai42_kit.net.url_guard`) resolves and validates each target
host, and :func:`fetch_url` is the httpx download built on it: while the guard is
enabled (the default) that download is DNS-rebinding-safe, redirect-safe, and
size-capped. :func:`guarded_request` is the single guarded GET/POST/DELETE beside it
(no redirects followed, a size cap, one total deadline). The guard is reusable on its
own by any caller that fetches a caller-supplied URL server-side over its own HTTP client.

:func:`fetch_url`, :func:`fetch_media` and :func:`guarded_request` are re-exported LAZILY (PEP 562): they pull in
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
    is_loopback_host,
    resolve_and_validate,
    url_guard_settings,
)

if TYPE_CHECKING:
    from tai42_kit.net.fetch_media import MediaFetchError, MediaStream, fetch_media, open_media_stream
    from tai42_kit.net.fetch_url import fetch_url
    from tai42_kit.net.guarded_request import (
        GuardedRequestError,
        GuardedRequestTimeoutError,
        GuardedResponse,
        guarded_request,
    )

_LAZY_NAMES = (
    "fetch_url",
    "fetch_media",
    "open_media_stream",
    "MediaFetchError",
    "MediaStream",
    "guarded_request",
    "GuardedRequestError",
    "GuardedRequestTimeoutError",
    "GuardedResponse",
)

__all__ = [
    "GuardedRequestError",
    "GuardedRequestTimeoutError",
    "GuardedResponse",
    "MediaFetchError",
    "MediaStream",
    "UrlGuardError",
    "UrlGuardSettings",
    "enforce_size",
    "fetch_media",
    "fetch_url",
    "guard_enabled",
    "guarded_request",
    "is_loopback_host",
    "open_media_stream",
    "resolve_and_validate",
    "url_guard_settings",
]


def __getattr__(name: str) -> object:
    """Load the ``httpx``-backed request helpers and media symbols on first access.

    :func:`fetch_url`, :func:`fetch_media`, :func:`open_media_stream`, :func:`guarded_request` and
    their error and result types are bound lazily so that importing a lighter ``net`` utility never
    pulls in the ``httpx``/``httpcore`` backend they ride on.

    All the names are cached in the module globals together, so the FUNCTION or CLASS (not
    the same-named submodule that importing it binds on the package) is what ``net.<name>``
    resolves to thereafter, whichever name is touched first.

    Import the helpers only from this package (``from tai42_kit.net import fetch_url`` /
    ``guarded_request``), never as the same-named submodule: a direct submodule import binds the
    MODULE onto the package's attribute, so this hook never runs and a later package-level import
    yields the module instead of the function.
    """
    if name in _LAZY_NAMES:
        # Loading these submodules binds the ``fetch_url`` and ``guarded_request`` MODULES onto
        # this package; rebinding every name together on the first access of any of them keeps
        # every later package-level import a function.
        from tai42_kit.net.fetch_media import MediaFetchError, MediaStream, fetch_media, open_media_stream
        from tai42_kit.net.fetch_url import fetch_url
        from tai42_kit.net.guarded_request import (
            GuardedRequestError,
            GuardedRequestTimeoutError,
            GuardedResponse,
            guarded_request,
        )

        globals()["fetch_url"] = fetch_url
        globals()["fetch_media"] = fetch_media
        globals()["open_media_stream"] = open_media_stream
        globals()["MediaFetchError"] = MediaFetchError
        globals()["MediaStream"] = MediaStream
        globals()["guarded_request"] = guarded_request
        globals()["GuardedRequestError"] = GuardedRequestError
        globals()["GuardedRequestTimeoutError"] = GuardedRequestTimeoutError
        globals()["GuardedResponse"] = GuardedResponse
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
