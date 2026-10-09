"""``GET /oauth/client-metadata.json``: the document served for the allow-listed origin the request's ``Host``
names, the scheme taken from the allow-list, 404 when nothing matches or the setting is off, and the route
mounted on every ``default_routers`` value."""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.requests import Request
from tai42_contract.app import tai42_app
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.server import TaiMCP
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.routers import client_identity

from ..app._fixtures.route_table import route_table


def _request(host: str | None, *, scheme: str = "https") -> Request:
    headers = [] if host is None else [(b"host", host.encode())]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": scheme,
            "path": "/oauth/client-metadata.json",
            "raw_path": b"/oauth/client-metadata.json",
            "query_string": b"",
            "headers": headers,
            "server": ("internal", 8080),
        }
    )


@pytest.fixture
def allow(monkeypatch: pytest.MonkeyPatch):
    def _allow(origins: str, *, document: bool = True, bridge: str | None = None) -> None:
        monkeypatch.setenv("CONNECTORS_REDIRECT_URI_ALLOWLIST", origins)
        monkeypatch.setenv("CONNECTORS_CLIENT_ID_DOCUMENT", "true" if document else "false")
        if bridge is None:
            monkeypatch.delenv("CONNECTORS_OAUTH_BRIDGE_URL", raising=False)
        else:
            monkeypatch.setenv("CONNECTORS_OAUTH_BRIDGE_URL", bridge)
        reset_all_settings()

    yield _allow
    reset_all_settings()


async def _get(host: str | None, *, scheme: str = "https"):
    response = await client_identity.client_identity_document(_request(host, scheme=scheme))
    body = json.loads(bytes(response.body))
    return response, body


async def test_the_document_for_an_allow_listed_https_origin(allow):
    allow("https://app.example.com,http://localhost:5173")
    response, body = await _get("app.example.com")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "max-age=300"
    assert body == {
        "client_id": "https://app.example.com/oauth/client-metadata.json",
        "client_name": "Tai",
        "client_uri": "https://app.example.com",
        "redirect_uris": ["https://app.example.com/oauth-bridge.html"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


async def test_a_request_arriving_as_http_behind_a_proxy_still_gets_the_https_id(allow):
    allow("https://app.example.com")
    _, body = await _get("app.example.com", scheme="http")
    assert body["client_id"] == "https://app.example.com/oauth/client-metadata.json"


async def test_one_listed_origin_is_served_under_any_host(allow):
    allow("https://app.example.com")
    response, body = await _get("10.0.0.7:8080")
    assert response.status_code == 200
    assert body["client_uri"] == "https://app.example.com"


async def test_with_two_origins_the_host_chooses_and_an_unlisted_host_is_404(allow):
    allow("https://a.example.com,https://B.example.com:443")
    _, body = await _get("a.example.com")
    assert body["client_id"] == "https://a.example.com/oauth/client-metadata.json"
    _, body = await _get("b.example.com")
    assert body["client_id"] == "https://B.example.com:443/oauth/client-metadata.json"
    response, body = await _get("unlisted.example.com")
    assert response.status_code == 404
    assert body == {"error": "not found"}


async def test_an_http_only_allow_list_is_404(allow):
    allow("http://localhost:5173")
    response, _ = await _get("localhost:5173")
    assert response.status_code == 404


async def test_the_setting_off_is_404(allow):
    allow("https://app.example.com", document=False)
    response, _ = await _get("app.example.com")
    assert response.status_code == 404


async def test_the_bridge_origin_is_no_document_origin(allow):
    allow("https://app.example.com,https://bridge.example.com", bridge="https://bridge.example.com")
    _, body = await _get("bridge.example.com")
    assert body["client_uri"] == "https://app.example.com"
    assert body["redirect_uris"] == ["https://app.example.com/oauth-bridge.html"]


@pytest.mark.parametrize("default_routers", ["all", "api", "none"])
def test_the_route_is_mounted_on_every_default_routers_value(default_routers: str) -> None:
    instance = TaiMCP(name=f"client-identity-{default_routers}")
    manifest = Manifest.model_validate(
        {"default_routers": default_routers, "routers_modules": ["tai42_skeleton.routers.tools"]}
    )

    async def run() -> set[str | None]:
        async with instance.app_context(manifest):
            return {getattr(route, "path", None) for route in route_table(instance)}

    with tai42_app.bound(None):
        served = asyncio.run(run())
    assert "/oauth/client-metadata.json" in served
