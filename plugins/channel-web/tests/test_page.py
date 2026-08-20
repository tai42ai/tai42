"""The chat page shell + the built-bundle loader — manifest validation (every
failure loud), the rendered shell, the refusal pages the door answers instead of it,
and the asset content-type map."""

from __future__ import annotations

from pathlib import Path

import pytest

from tai42_channel_web import page
from tai42_channel_web.page import (
    PAGE_CSP,
    REFUSAL_CODE_META,
    REFUSAL_CSP,
    PublicBuildError,
    asset_content_type,
    load_build,
    render_page,
    render_refusal,
)

from .conftest import ENTRY_ASSET, IDENTITY, STYLE_ASSET, write_manifest


def test_public_dir_is_the_packaged_bundle_directory():
    # The seam every other test replaces: the real bundle ships inside the package.
    assert page._public_dir() == Path(page.__file__).resolve().parent / "public"


def test_load_build_reads_entry_styles_and_integrity(public_build: Path):
    build = load_build()
    assert build.entry == ENTRY_ASSET
    assert build.styles == (STYLE_ASSET,)
    assert build.integrity[ENTRY_ASSET] == f"sha384-{ENTRY_ASSET}"


def test_load_build_without_a_bundle_names_the_build_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(page, "_public_dir", lambda: tmp_path / "absent")
    with pytest.raises(PublicBuildError, match="is not built"):
        load_build()


def test_load_build_rejects_non_json(public_build: Path):
    (public_build / page.PUBLIC_MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(PublicBuildError, match="not valid JSON"):
        load_build()


def test_load_build_rejects_a_non_object_manifest(public_build: Path):
    (public_build / page.PUBLIC_MANIFEST_FILENAME).write_text("[]", encoding="utf-8")
    with pytest.raises(PublicBuildError, match="must hold a JSON object"):
        load_build()


@pytest.mark.parametrize("entry", [None, "", 7])
def test_load_build_rejects_a_missing_or_mistyped_entry(public_build: Path, entry: object):
    write_manifest(public_build, entry=entry)
    with pytest.raises(PublicBuildError, match="'entry' must be a non-empty string"):
        load_build()


@pytest.mark.parametrize("styles", [None, "one.css", ["", "two.css"]])
def test_load_build_rejects_mistyped_styles(public_build: Path, styles: object):
    write_manifest(public_build, styles=styles)
    with pytest.raises(PublicBuildError, match="'styles' must be a list"):
        load_build()


@pytest.mark.parametrize("integrity", [None, [], {"a.js": ""}])
def test_load_build_rejects_a_mistyped_integrity_map(public_build: Path, integrity: object):
    write_manifest(public_build, integrity=integrity)
    with pytest.raises(PublicBuildError, match="'integrity' must map"):
        load_build()


def test_load_build_rejects_a_linked_file_absent_from_the_integrity_map(public_build: Path):
    # Only integrity-listed files are served, so a linked-but-unlisted one would 404
    # in the browser — the build is refused instead.
    write_manifest(public_build, integrity={ENTRY_ASSET: "sha384-e"})
    with pytest.raises(PublicBuildError, match="absent from the integrity map"):
        load_build()


def test_render_page_links_every_asset_with_its_integrity(public_build: Path):
    build = load_build()
    html = render_page(IDENTITY, "Chat", build, "/api/channels/web")
    assert html.startswith("<!doctype html>")
    assert (
        f'<script type="module" src="/api/channels/web/assets/{ENTRY_ASSET}" integrity="sha384-{ENTRY_ASSET}">'
    ) in html
    assert (
        f'<link rel="stylesheet" href="/api/channels/web/assets/{STYLE_ASSET}" integrity="sha384-{STYLE_ASSET}">'
    ) in html
    assert '<div id="root" data-identity="site-alpha" data-api-base="/api/channels/web"></div>' in html
    assert 'name="theme-color"' in html


def test_render_page_hangs_asset_urls_off_the_given_mount_base(public_build: Path):
    # A remapped mount base moves the asset URLs with it — the default is never
    # hardcoded into the served shell.
    html = render_page(IDENTITY, "Chat", load_build(), "/api/channels/relay")
    assert f'src="/api/channels/relay/assets/{ENTRY_ASSET}"' in html
    assert "/api/channels/web/assets/" not in html


def test_render_page_carries_the_mount_base_on_root_for_the_bundle(public_build: Path):
    # The bundle reads its API base off #root's data-api-base; a remapped mount is
    # carried there too, so the browser's own calls follow it rather than a default.
    html = render_page(IDENTITY, "Chat", load_build(), "/api/channels/relay")
    assert f'data-identity="{IDENTITY}" data-api-base="/api/channels/relay"' in html
    assert 'data-api-base="/api/channels/web"' not in html


def test_render_page_escapes_interpolated_values(public_build: Path):
    html = render_page('x" onload="boom', "</title><script>", load_build(), "/api/channels/web")
    assert 'onload="boom' not in html
    assert "<script>" not in html.split('<script type="module"')[0]


@pytest.mark.parametrize(
    "directive",
    [
        "default-src 'none'",
        # The bundled design-system overlays inject a <style> element for their
        # scroll lock; 'self' alone refuses it and every modal on the page breaks.
        "style-src 'self' 'unsafe-inline'",
        # The bundle's own webfonts, served by the asset door beside the stylesheet.
        "font-src 'self'",
        "connect-src 'self'",
        # ``https:`` serves an agent-sent media card's image (http is refused, matching
        # the contract's https-only rule); ``data:`` serves the schema-form media field's
        # inline <img> preview of a visitor-picked image, whose source is a data: URL.
        "img-src 'self' data: https:",
        "frame-ancestors 'none'",
    ],
)
def test_page_csp_carries_the_directive(directive: str):
    assert directive in PAGE_CSP.split("; ")


def test_page_csp_never_admits_inline_script():
    # The bundle ships as a file; an inline-script allowance would hand an injected
    # string a way to run, which no part of this page needs.
    script_src = next(part for part in PAGE_CSP.split("; ") if part.startswith("script-src"))
    assert script_src == "script-src 'self'"


def test_render_refusal_is_a_self_contained_page_carrying_its_code():
    html = render_refusal("No entry", "Nothing to see here.", "some_code")

    assert html.startswith("<!doctype html>")
    assert "<title>No entry</title>" in html
    assert "<h1>No entry</h1>" in html
    assert "<p>Nothing to see here.</p>" in html
    assert f'<meta name="{REFUSAL_CODE_META}" content="some_code">' in html
    # Nothing to fetch, nothing to run, and no inline style — REFUSAL_CSP allows none
    # of the three, so a refusal page that needed one would render unstyled anyway.
    for forbidden in ("<script", "<link", "<style", "style="):
        assert forbidden not in html


def test_render_refusal_escapes_its_copy_and_omits_an_absent_code():
    html = render_refusal("<b>title</b>", 'a "quoted" & marked <i>line</i>')

    assert "<b>" not in html
    assert "<i>" not in html
    assert "&lt;b&gt;title&lt;/b&gt;" in html
    assert "&amp;" in html
    assert REFUSAL_CODE_META not in html


def test_refusal_csp_admits_nothing_at_all():
    # The refusal page links, runs and styles nothing, so it needs no allowance —
    # including the shell's inline-style one, which is why it carries no <style>.
    assert REFUSAL_CSP.split("; ") == [
        "default-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'none'",
    ]
    assert "unsafe-inline" not in REFUSAL_CSP


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("app-A1.js", "text/javascript"),
        ("app-A1.css", "text/css"),
        ("app-A1.js.map", "application/json"),
        ("icon.svg", "image/svg+xml"),
        ("icon.png", "image/png"),
        ("favicon.ico", "image/x-icon"),
        ("inter.woff2", "font/woff2"),
        ("page.html", "application/octet-stream"),
    ],
)
def test_asset_content_type_map(filename: str, content_type: str):
    # An unmapped extension is octet-stream — this door must NEVER emit text/html.
    assert asset_content_type(filename) == content_type
