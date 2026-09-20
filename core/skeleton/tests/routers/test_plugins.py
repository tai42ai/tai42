"""Studio-plugin router: registry listing, plugin asset serving (traversal +
manifest exclusion + content-type), and SPA hosting (import-map injection, CSP
nonce, history fallback, cache headers)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

import tai42_skeleton.plugins.registry as reg
import tai42_skeleton.routers.plugins as router
from tai42_skeleton.plugins.registry import build_registry, set_current_registry


def _req(**path_params) -> Request:
    return cast(Request, SimpleNamespace(path_params=path_params))


def _json(resp) -> dict:
    return json.loads(bytes(resp.body))


def _build_plugin_studio(root: Path) -> Path:
    studio = root / "studio"
    studio.mkdir(parents=True)
    (studio / "index-a1b2c3.js").write_text("export const x = 1;\n", encoding="utf-8")
    (studio / "panel.html").write_text("<h1>nope</h1>", encoding="utf-8")
    manifest = {
        "name": "acme_plugin",
        "version": "0.1.0",
        "api_version": 1,
        "entry": "index-a1b2c3.js",
        "integrity": {
            "index-a1b2c3.js": reg._hash_file(studio / "index-a1b2c3.js"),
            "panel.html": reg._hash_file(studio / "panel.html"),
        },
        "contributions": {"tool_panels": {"acme_demo": "panel"}, "pages": [], "settings_tabs": []},
    }
    (studio / "studio-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return studio


def _build_spa_dist(root: Path) -> Path:
    dist = root / "dist"
    (dist / "vendor").mkdir(parents=True)
    (dist / "assets").mkdir(parents=True)
    for rel in reg.VENDOR_MODULES.values():
        (dist / rel).write_text("export {};\n", encoding="utf-8")
    (dist / "assets" / "app-h4sh.js").write_text("console.log(1)\n", encoding="utf-8")
    # A stable-named ``public/``-copied root asset (favicon / touch icon), emitted
    # at a fixed URL and NOT content-hashed.
    (dist / "apple-touch-icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (dist / "index.html").write_text(
        "<!doctype html><head><!--tai-importmap-->"
        '<script type="module" src="/assets/app-h4sh.js"></script></head><body></body>',
        encoding="utf-8",
    )
    (dist / "oauth-callback.html").write_text("<!doctype html><body>cb</body>", encoding="utf-8")
    return dist


@pytest.fixture
def studio_env(tmp_path, monkeypatch):
    """A built registry (one plugin + vendor hashes) and a configured SPA dist."""
    plugin_studio = _build_plugin_studio(tmp_path / "plugin")
    dist = _build_spa_dist(tmp_path)
    monkeypatch.setattr(reg, "_studio_root", lambda package: plugin_studio)
    registry = build_registry(["acme_plugin"], str(dist))
    # Preserve/restore the process-global registry around the test.
    prev = reg._current
    set_current_registry(registry)
    monkeypatch.setattr(router, "plugins_settings", lambda: SimpleNamespace(studio_dist_path=str(dist)))
    yield SimpleNamespace(dist=dist, plugin_studio=plugin_studio, registry=registry)
    reg._current = prev


# -- Registry listing --------------------------------------------------------


async def test_registry_listing(studio_env):
    resp = await router.list_studio_plugins(_req())
    assert resp.status_code == 200
    body = _json(resp)
    assert body["data"][0]["name"] == "acme_plugin"
    assert resp.headers["cache-control"] == "no-cache"


async def test_registry_listing_unbuilt_is_loud(monkeypatch):
    monkeypatch.setattr(reg, "_current", None)
    resp = await router.list_studio_plugins(_req())
    assert resp.status_code == 500
    assert "not been built" in _json(resp)["error"]


# -- Asset serving -----------------------------------------------------------


async def test_asset_serves_js_with_long_cache(studio_env):
    resp = await router.serve_studio_asset(_req(name="acme_plugin", path="index-a1b2c3.js"))
    assert resp.status_code == 200
    assert resp.media_type == "text/javascript"
    assert "immutable" in resp.headers["cache-control"]


async def test_asset_unknown_plugin_404(studio_env):
    resp = await router.serve_studio_asset(_req(name="nope", path="x.js"))
    assert resp.status_code == 404


async def test_asset_traversal_rejected(studio_env):
    resp = await router.serve_studio_asset(_req(name="acme_plugin", path="../../etc/passwd"))
    assert resp.status_code == 404


async def test_asset_manifest_excluded(studio_env):
    resp = await router.serve_studio_asset(_req(name="acme_plugin", path="studio-manifest.json"))
    assert resp.status_code == 404


async def test_asset_manifest_exclusion_bypass_variant(studio_env):
    # A normalization bypass that stays under-root must still be refused (checked
    # on the realpath basename, not the raw string).
    resp = await router.serve_studio_asset(_req(name="acme_plugin", path="sub/../studio-manifest.json"))
    assert resp.status_code == 404


def test_asset_manifest_exclusion_urlencoded_variant(studio_env):
    # Through the route stack: the ASGI server percent-decodes the path once, so
    # this double-encoded request reaches the handler as the literal
    # ``sub/%2e%2e/studio-manifest.json``, which must NOT be decoded again into a
    # traversal. The single-decoded ``sub/../`` case is covered by the
    # neighboring test.
    app = Starlette(
        routes=[Route("/api/plugins/{name}/studio/{path:path}", router.serve_studio_asset, methods=["GET"])]
    )
    resp = TestClient(app).get("/api/plugins/acme_plugin/studio/sub/%252e%252e/studio-manifest.json")
    assert resp.status_code == 404


async def test_asset_html_not_served_as_document(studio_env):
    resp = await router.serve_studio_asset(_req(name="acme_plugin", path="panel.html"))
    assert resp.status_code == 200
    assert resp.media_type == "application/octet-stream"
    assert "text/html" not in (resp.media_type or "")


async def test_asset_unlisted_file_404(studio_env):
    # A file shipped in the dist but NOT in the manifest integrity map is
    # un-hashed and un-scanned, so it must be unreachable — only integrity-pinned
    # assets are served.
    (studio_env.plugin_studio / "extra.js").write_text("import '@tai42/studio-sdk/host';\n", encoding="utf-8")
    resp = await router.serve_studio_asset(_req(name="acme_plugin", path="extra.js"))
    assert resp.status_code == 404
    # The listed bundle still serves.
    ok = await router.serve_studio_asset(_req(name="acme_plugin", path="index-a1b2c3.js"))
    assert ok.status_code == 200


# -- SPA hosting -------------------------------------------------------------


async def test_spa_deep_link_falls_back_to_index_with_csp(studio_env):
    resp = await router.serve_spa(_req(spa_path="tools"))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/html; charset=utf-8"
    # The shell must never be pinned by a stale copy (dead asset hashes / desynced nonce).
    assert resp.headers["cache-control"] == "no-store, must-revalidate"
    # ``nosniff`` rides the shell so the browser honors the declared HTML type (H6).
    assert resp.headers["x-content-type-options"] == "nosniff"
    csp = resp.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "'wasm-unsafe-eval'" in csp
    html = bytes(resp.body).decode()
    assert html.count('type="importmap"') == 1
    assert html.index('type="importmap"') < html.index('type="module"')
    # The nonce in the CSP header matches the one stamped on the inline script.
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"' in html


async def test_spa_index_nonce_differs_per_response(studio_env):
    r1 = await router.serve_spa(_req(spa_path="a"))
    r2 = await router.serve_spa(_req(spa_path="b"))
    n1 = r1.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
    n2 = r2.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
    assert n1 != n2


async def test_spa_static_asset_long_cache(studio_env):
    resp = await router.serve_spa(_req(spa_path="assets/app-h4sh.js"))
    assert resp.status_code == 200
    assert resp.media_type == "text/javascript"
    assert "immutable" in resp.headers["cache-control"]


async def test_spa_vendor_asset_revalidates(studio_env):
    resp = await router.serve_spa(_req(spa_path="vendor/react.js"))
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"


async def test_spa_root_public_asset_revalidates(studio_env):
    # A stable-named root ``public/``-copied asset (the favicon / touch icon) is NOT
    # content-hashed: its bytes CAN change at the same URL (a rebrand), so it must
    # revalidate — never ``immutable``, which would strand a stale icon in browsers.
    resp = await router.serve_spa(_req(spa_path="apple-touch-icon.png"))
    assert resp.status_code == 200
    assert resp.media_type == "image/png"
    assert resp.headers["cache-control"] == "no-cache"
    assert "immutable" not in resp.headers["cache-control"]


async def test_spa_direct_index_request_is_injected(studio_env):
    resp = await router.serve_spa(_req(spa_path="index.html"))
    assert resp.status_code == 200
    assert 'type="importmap"' in bytes(resp.body).decode()


def _served_importmap(resp) -> dict:
    """The import map the browser parses out of the served shell."""
    html = bytes(resp.body).decode()
    body = html.split('type="importmap"', 1)[1].split(">", 1)[1].split("</script>", 1)[0]
    return json.loads(body)


async def test_spa_shell_boots_without_the_optional_vendor_module(studio_env):
    # A dist shipping only the required vendor set boots and serves a map with no
    # optional specifier — the optional asset is never a boot requirement.
    resp = await router.serve_spa(_req(spa_path="index.html"))
    assert resp.status_code == 200
    imports = _served_importmap(resp)["imports"]
    assert "@tai42/jq-studio" not in imports
    assert imports["react"] == "/vendor/react.js"


async def test_spa_shell_boots_with_the_optional_vendor_module(studio_env):
    # A dist that ships the optional module resolves it through the map, and the
    # module plus its runtime sidecars all serve from the dist.
    dist = studio_env.dist
    (dist / "vendor" / "jq-studio.js").write_text("export const jq = 1;\n", encoding="utf-8")
    (dist / "vendor" / "jq-studio-worker.js").write_text("self.onmessage = () => {};\n", encoding="utf-8")
    (dist / "vendor" / "jq.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
    set_current_registry(build_registry(["acme_plugin"], str(dist)))
    resp = await router.serve_spa(_req(spa_path="index.html"))
    assert resp.status_code == 200
    parsed = _served_importmap(resp)
    assert parsed["imports"]["@tai42/jq-studio"] == "/vendor/jq-studio.js"
    assert parsed["integrity"]["/vendor/jq-studio.js"].startswith("sha384-")
    module = await router.serve_spa(_req(spa_path="vendor/jq-studio.js"))
    assert module.status_code == 200
    assert module.media_type == "text/javascript"
    worker = await router.serve_spa(_req(spa_path="vendor/jq-studio-worker.js"))
    assert worker.status_code == 200
    assert worker.media_type == "text/javascript"
    wasm = await router.serve_spa(_req(spa_path="vendor/jq.wasm"))
    assert wasm.status_code == 200
    assert wasm.media_type == "application/wasm"


async def test_spa_missing_asset_extension_404s_not_shell(studio_env):
    # A request for a static asset (extension-bearing last segment, not .html) that
    # does not resolve to a real file is a genuine 404 — NEVER the index.html shell.
    # Serving the HTML shell for a missing binary asset masks a 404 as a
    # ``text/html`` body (a missing ``jq.wasm`` then fails wasm streaming compile on
    # the ``<!do`` bytes). This is the industry-standard SPA-fallback contract.
    for missing in ("assets/jq-deadbeef.wasm", "assets/vendor-nope.js", "styles/gone.css"):
        resp = await router.serve_spa(_req(spa_path=missing))
        assert resp.status_code == 404, missing
        assert "text/html" not in (resp.headers.get("content-type") or ""), missing


async def test_spa_missing_extensionless_route_serves_shell(studio_env):
    # A history-API deep link (no file extension on the last segment) keeps the
    # index.html fallthrough so client-side routing works on hard refresh.
    resp = await router.serve_spa(_req(spa_path="agents/settings"))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/html; charset=utf-8"
    assert 'type="importmap"' in bytes(resp.body).decode()


async def test_spa_existing_wasm_served_as_application_wasm(studio_env):
    # An existing ``.wasm`` asset streams with ``application/wasm`` so the browser's
    # streaming compilation (``WebAssembly.instantiateStreaming``) accepts it.
    (studio_env.dist / "assets" / "jq-a1b2c3.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
    resp = await router.serve_spa(_req(spa_path="assets/jq-a1b2c3.wasm"))
    assert resp.status_code == 200
    assert resp.media_type == "application/wasm"


async def test_spa_csp_declares_worker_src_self(studio_env):
    # ``worker-src 'self'`` is explicit so the Web Worker policy does not silently
    # inherit ``script-src`` — the studio's jq worker loads first-party.
    resp = await router.serve_spa(_req(spa_path="tools"))
    csp = resp.headers["content-security-policy"]
    assert "worker-src 'self'" in csp


async def test_spa_guards_api_paths(studio_env):
    resp = await router.serve_spa(_req(spa_path="api/tools"))
    assert resp.status_code == 404


async def test_spa_guards_mcp_paths(studio_env):
    resp = await router.serve_spa(_req(spa_path="mcp/x"))
    assert resp.status_code == 404


async def test_spa_disabled_when_dist_unset(studio_env, monkeypatch):
    monkeypatch.setattr(router, "plugins_settings", lambda: SimpleNamespace(studio_dist_path=None))
    resp = await router.serve_spa(_req(spa_path="tools"))
    assert resp.status_code == 404


async def test_oauth_static_html_carries_security_headers(studio_env):
    resp = await router.serve_spa(_req(spa_path="oauth-callback.html"))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/html; charset=utf-8"
    assert resp.headers["x-content-type-options"] == "nosniff"
    csp = resp.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "'wasm-unsafe-eval'" in csp


# -- SPA realpath containment (H2) -------------------------------------------


async def test_spa_traversal_escaping_root_never_serves_file(studio_env, tmp_path):
    # A traversal that escapes the realpath'd bundle root can NEVER serve the
    # out-of-bundle file: containment fails and the request falls through to the
    # dataless shell, never the requested bytes.
    (tmp_path / "config").write_text("OUT_OF_BUNDLE_SECRET", encoding="utf-8")
    resp = await router.serve_spa(_req(spa_path="static/../../config"))
    assert resp.status_code == 200
    body = bytes(resp.body).decode()
    assert "OUT_OF_BUNDLE_SECRET" not in body
    assert 'type="importmap"' in body  # the shell, not the file


async def test_spa_symlink_escaping_root_never_serves_target(studio_env, tmp_path):
    # An in-bundle symlink pointing OUTSIDE the bundle: realpath follows it out of
    # root, containment fails, and the symlink target's bytes are never served.
    outside = tmp_path / "outside-secret.js"
    outside.write_text("OUTSIDE_SYMLINK_SECRET", encoding="utf-8")
    (studio_env.dist / "leak.js").symlink_to(outside)
    resp = await router.serve_spa(_req(spa_path="leak.js"))
    body = bytes(resp.body).decode()
    assert "OUTSIDE_SYMLINK_SECRET" not in body


def test_spa_urlencoded_traversal_never_serves_file(studio_env, tmp_path):
    # Through the route stack: the ASGI server percent-decodes the path once. A
    # ``..%2f..%2f`` deep link that resolves out of the bundle after decoding still
    # never serves the out-of-bundle file (it serves the shell or 404).
    (tmp_path / "passwd").write_text("URLENCODED_OUT_OF_BUNDLE", encoding="utf-8")
    app = Starlette(routes=[Route("/{spa_path:path}", router.serve_spa, methods=["GET"])])
    resp = TestClient(app).get("/agents/..%2f..%2fpasswd")
    assert "URLENCODED_OUT_OF_BUNDLE" not in resp.text
