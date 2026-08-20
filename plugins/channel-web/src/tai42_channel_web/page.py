"""The standalone chat page: its HTML shell, and the built bundle it points at.

The page the plugin serves is a shell only — title, theme metas, an empty ``#root``
carrying the route identity, and the ``<link>``/``<script>`` tags of the built
bundle. The bundle itself lives in the packaged ``public/`` directory:
``public-manifest.json`` plus the content-hashed files it lists.

When the page door refuses instead of serving that shell, it answers a minimal
refusal page (``render_refusal``) rather than the API doors' JSON: the caller is a
browser navigating to the chat URL, and a JSON body would be rendered as text in the
visitor's window.

The manifest's ``integrity`` map is ALSO the serving allowlist: the asset door looks
a requested name up in it by exact match, so only built, hash-pinned files are
reachable and no requested name can address anything outside the bundle.

A missing or malformed manifest is a loud 500 naming the build step — a shell whose
asset tags point at nothing would render as a silent blank page instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from html import escape
from pathlib import Path

PUBLIC_MANIFEST_FILENAME = "public-manifest.json"
HTML_CONTENT_TYPE = "text/html; charset=utf-8"

# Named in every build error so an operator is told the one command that fixes it.
_BUILD_STEP = "`pnpm install && pnpm build` in plugins/channel-web"

# The page's own CSP: no inline script, and nothing at all from a foreign origin —
# the chat page talks only to the doors that served it.
#
# ``style-src`` admits ``'unsafe-inline'``: the design-system overlays compiled into
# this bundle inject a ``<style>`` element at runtime for their scroll lock, which
# ``'self'`` alone refuses. ``script-src`` stays strict. ``font-src 'self'`` is the
# bundle's own webfonts, served by the asset door. ``img-src`` admits ``https:`` for an
# agent-sent media card (the contract constrains a media-card image to an absolute https
# source, never http) and ``data:`` for the bundled schema-form media field's inline
# ``<img>`` preview of a visitor-picked image, whose source is a ``data:`` URL.
PAGE_CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "font-src 'self'",
        "connect-src 'self'",
        "img-src 'self' data: https:",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
)

# The refusal pages' own CSP. They link nothing, run nothing and style nothing, so
# everything is denied: no allowance the shell needs is repeated here, and the pages
# below must therefore stay free of any inline ``style`` — ``default-src 'none'``
# refuses one. ``base-uri``/``frame-ancestors``/``form-action`` are not covered by
# ``default-src`` and are named explicitly.
REFUSAL_CSP = "; ".join(
    [
        "default-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'none'",
    ]
)

# Carries the same machine-readable refusal code the API doors put in their JSON
# ``code`` field, so an operator reading the refused navigation still gets it.
REFUSAL_CODE_META = "tai42-refusal-code"

# Explicit content-type mapping — never OS mimetype guessing (a wrong module MIME
# silently breaks ESM loading). Every UNMAPPED extension serves as
# application/octet-stream; this map must NEVER yield text/html.
_CONTENT_TYPES: dict[str, str] = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}
_OCTET_STREAM = "application/octet-stream"

# The browser paints the surrounding chrome (address bar, status bar) with these
# while the bundle's own token CSS paints the document.
_THEME_COLOR_LIGHT = "#ffffff"
_THEME_COLOR_DARK = "#111214"


class PublicBuildError(RuntimeError):
    """The built chat-page bundle is absent or malformed (answered as a loud 500)."""


@dataclass(frozen=True)
class PublicBuild:
    """The built bundle as the page and asset doors need it: the module entry, the
    stylesheets to link, and the name -> sha384 map that is both the SRI source and
    the serving allowlist."""

    entry: str
    styles: tuple[str, ...]
    integrity: dict[str, str]


def _public_dir() -> Path:
    """The packaged bundle directory. A module-level seam so a test can point the
    loader at a fixture build — repointing it requires ``load_build.cache_clear()``."""
    return Path(__file__).resolve().parent / "public"


def _require_str(data: dict[str, object], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise PublicBuildError(f"{path}: {key!r} must be a non-empty string — rebuild with {_BUILD_STEP}")
    return value


@lru_cache(maxsize=1)
def load_build() -> PublicBuild:
    """Read and validate ``public-manifest.json``, once per process.

    The bundle ships inside the wheel and cannot change under a running server, so
    the parsed manifest is cached — every page and asset request would otherwise be
    a synchronous disk read on the event loop. ``lru_cache`` never stores a raised
    exception, so an unbuilt bundle is re-read (and re-refused) on every request.

    Every failure mode is loud: an unbuilt bundle, unreadable or non-JSON manifest,
    a missing/mistyped field, or an entry/stylesheet absent from the integrity map
    (which would serve a 404 for a file the page links)."""
    path = _public_dir() / PUBLIC_MANIFEST_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublicBuildError(f"the chat page bundle is not built: {path} is missing — run {_BUILD_STEP}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PublicBuildError(f"{path} is not valid JSON — rebuild with {_BUILD_STEP}") from exc
    if not isinstance(data, dict):
        raise PublicBuildError(f"{path} must hold a JSON object — rebuild with {_BUILD_STEP}")

    entry = _require_str(data, "entry", path)
    raw_styles = data.get("styles")
    if not isinstance(raw_styles, list) or not all(isinstance(name, str) and name for name in raw_styles):
        raise PublicBuildError(f"{path}: 'styles' must be a list of file names — rebuild with {_BUILD_STEP}")
    raw_integrity = data.get("integrity")
    if not isinstance(raw_integrity, dict) or not all(
        isinstance(name, str) and isinstance(digest, str) and digest for name, digest in raw_integrity.items()
    ):
        raise PublicBuildError(f"{path}: 'integrity' must map every emitted file to its digest — {_BUILD_STEP}")

    styles = tuple(str(name) for name in raw_styles)
    integrity = {str(name): str(digest) for name, digest in raw_integrity.items()}
    for referenced in (entry, *styles):
        if referenced not in integrity:
            raise PublicBuildError(
                f"{path}: {referenced!r} is linked by the page but absent from the integrity map "
                f"(only integrity-listed files are served) — rebuild with {_BUILD_STEP}"
            )
    return PublicBuild(entry=entry, styles=styles, integrity=integrity)


def asset_path(name: str) -> Path:
    """The on-disk path of one built asset. ``name`` MUST already have matched a key
    of the build's integrity map — that exact-name lookup is what keeps this join
    inside the bundle."""
    return _public_dir() / name


def asset_content_type(filename: str) -> str:
    """Content-type for a built asset by extension. Unmapped -> octet-stream; never
    text/html."""
    lower = filename.lower()
    for suffix, content_type in _CONTENT_TYPES.items():
        if lower.endswith(suffix):
            return content_type
    return _OCTET_STREAM


def _asset_url(mount_base: str, name: str) -> str:
    return f"{mount_base}/assets/{name}"


def render_page(identity: str, title: str, build: PublicBuild, mount_base: str) -> str:
    """The chat page shell around the built bundle.

    ``mount_base`` is this deployment's absolute mount prefix for the web channel,
    read from the serving request; asset URLs hang off ``{mount_base}/assets/`` so a
    remapped base is followed rather than the default hardcoded.

    The bundle reads the route it talks to from ``#root``'s ``data-identity`` and the
    mount its own API doors sit under from ``#root``'s ``data-api-base`` (this same
    ``mount_base``), so a remapped mount is followed there too rather than a default
    assumed. Every interpolated value is HTML-escaped — the identity is a URL segment
    and the title is operator config, neither of which may break out of its attribute
    or element.
    """
    head = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
        '<meta name="color-scheme" content="light dark">',
        f'<meta name="theme-color" media="(prefers-color-scheme: light)" content="{_THEME_COLOR_LIGHT}">',
        f'<meta name="theme-color" media="(prefers-color-scheme: dark)" content="{_THEME_COLOR_DARK}">',
        f"<title>{escape(title)}</title>",
    ]
    head += [
        f'<link rel="stylesheet" href="{escape(_asset_url(mount_base, name))}" '
        f'integrity="{escape(build.integrity[name])}">'
        for name in build.styles
    ]
    body = [
        f'<div id="root" data-identity="{escape(identity)}" data-api-base="{escape(mount_base)}"></div>',
        f'<script type="module" src="{escape(_asset_url(mount_base, build.entry))}" '
        f'integrity="{escape(build.integrity[build.entry])}"></script>',
    ]
    lines = ["<!doctype html>", '<html lang="en">', "<head>", *head, "</head>", "<body>", *body, "</body>", "</html>"]
    return "\n".join(lines) + "\n"


def render_refusal(title: str, message: str, code: str | None = None) -> str:
    """A minimal page for a navigation the chat door refuses.

    Self-contained under ``REFUSAL_CSP``: no script, no stylesheet, and no inline
    ``style`` — the browser's own defaults render it, with ``color-scheme`` so a dark
    browser does not paint it unreadably. ``code`` rides a meta named
    ``REFUSAL_CODE_META``. Callers pass module constants only, so every refusal page
    is byte-constant: no request-derived value is ever interpolated.
    """
    head = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="light dark">',
        f"<title>{escape(title)}</title>",
    ]
    if code is not None:
        head.append(f'<meta name="{REFUSAL_CODE_META}" content="{escape(code)}">')
    body = [f"<h1>{escape(title)}</h1>", f"<p>{escape(message)}</p>"]
    lines = ["<!doctype html>", '<html lang="en">', "<head>", *head, "</head>", "<body>", *body, "</body>", "</html>"]
    return "\n".join(lines) + "\n"
