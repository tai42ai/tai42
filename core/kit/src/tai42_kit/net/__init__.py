"""SSRF-guarded server-side URL fetching.

The SSRF guard (:mod:`tai42_kit.net.url_guard`) resolves and validates each target
host, and :func:`fetch_url` is the httpx download built on it: while the guard is
enabled (the default) that download is DNS-rebinding-safe, redirect-safe, and
size-capped. The guard is reusable on its own by any caller that fetches a
caller-supplied URL server-side over its own HTTP client.

:func:`fetch_url` is re-exported LAZILY (PEP 562): it pulls in ``httpx``/``httpcore``,
and a consumer of a lighter ``net`` utility (e.g. :mod:`tai42_kit.net.request_body`)
must not drag that backend into its import graph merely by importing a sibling module.
This is the same reason :mod:`tai42_kit.net.jwt` (which needs ``joserfc``) stays out of
this re-export.
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
    from tai42_kit.net.fetch_url import fetch_url

__all__ = [
    "UrlGuardError",
    "UrlGuardSettings",
    "enforce_size",
    "fetch_url",
    "guard_enabled",
    "resolve_and_validate",
    "url_guard_settings",
]


def __getattr__(name: str) -> object:
    """Load :func:`fetch_url` on first access so importing a lighter ``net`` utility
    never pulls in the ``httpx``/``httpcore`` backend it rides on.

    The name is cached in the module globals so the FUNCTION (not the same-named
    submodule that importing it binds on the package) is what ``net.fetch_url``
    resolves to thereafter — the shape the eager re-export gave.

    Import it only as ``from tai42_kit.net import fetch_url`` (this package), never as
    the submodule ``tai42_kit.net.fetch_url``: a direct submodule import binds the
    MODULE onto the package's ``fetch_url`` attribute, so this hook never runs and a
    later package-level import yields the module instead of the function."""
    if name == "fetch_url":
        from tai42_kit.net.fetch_url import fetch_url

        globals()["fetch_url"] = fetch_url
        return fetch_url
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
