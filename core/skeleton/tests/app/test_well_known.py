"""``GET /.well-known/oauth-protected-resource/{endpoint_path}`` — one metadata document per MCP endpoint.

The handler's three rows (nothing advertised → 404; a path that is not an MCP endpoint
itself → 404; otherwise the document), then the route as a served app answers it: public
by its own declaration with the Studio shell tier on and off and with an operator's route
row present, the same document for every well-formed slug, HEAD, and mounted on a lean
``default_routers: "none"`` boot.
"""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.requests import Request
from tai42_contract.app import tai42_app

from tai42_skeleton.app.http import record_sub_mcp_mount
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.serving_core import record_sse_surface
from tai42_skeleton.app.sub_mcp_app import ROOT_PREFIX
from tai42_skeleton.routers.well_known import protected_resource_metadata

from ..access_control.conftest import _FakeApp
from ._door_fixtures import (
    ISSUER,
    PUBLIC_BASE_URL,
    DeclaringTokenProvider,
    install_declaring_provider,
    public_base_url,
    register_declaring_provider,
)
from ._served_app import REQUEST_TIMEOUT_S, install_access_control, manifest, served

pytestmark = pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning")

DOC = "/.well-known/oauth-protected-resource"


def _request(endpoint_path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"{DOC}/{endpoint_path}",
            "headers": [],
            "path_params": {"endpoint_path": endpoint_path},
        }
    )


@pytest.fixture
def _surfaces(monkeypatch: pytest.MonkeyPatch, streamable_http_mounted) -> None:
    from tai42_skeleton.app.route_registry import route_registry

    monkeypatch.setattr(route_registry, "_routes", dict(route_registry._routes))
    record_sse_surface("/sse", "/messages/")
    record_sub_mcp_mount(ROOT_PREFIX)


@pytest.fixture
def _declaring(monkeypatch: pytest.MonkeyPatch):
    install_access_control(monkeypatch)
    register_declaring_provider(DeclaringTokenProvider())
    with tai42_app.bound(_FakeApp()):
        yield


@pytest.mark.usefixtures("_surfaces", "_declaring")
@pytest.mark.parametrize("endpoint", ["mcp", "sse", "app/svc", "app/svc/sse"])
async def test_an_mcp_endpoint_gets_its_document(monkeypatch: pytest.MonkeyPatch, endpoint: str) -> None:
    with public_base_url(monkeypatch, PUBLIC_BASE_URL):
        response = await protected_resource_metadata(_request(endpoint))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert json.loads(bytes(response.body)) == {
        "resource": f"{PUBLIC_BASE_URL}/{endpoint}",
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
    }


@pytest.mark.usefixtures("_surfaces", "_declaring")
@pytest.mark.parametrize(
    "endpoint", ["messages", "messages/x", "app/svc/messages", "app/svc/other", "api/tools", "", "app/Bad"]
)
async def test_a_path_that_is_not_an_mcp_endpoint_itself_is_404(monkeypatch: pytest.MonkeyPatch, endpoint: str) -> None:
    with public_base_url(monkeypatch, PUBLIC_BASE_URL):
        response = await protected_resource_metadata(_request(endpoint))
    assert response.status_code == 404


@pytest.mark.usefixtures("_surfaces", "_declaring")
async def test_nothing_advertised_without_a_public_url_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    with public_base_url(monkeypatch, None):
        response = await protected_resource_metadata(_request("mcp"))
    assert response.status_code == 404


@pytest.mark.usefixtures("_surfaces", "_declaring")
async def test_nothing_advertised_with_access_control_off_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCESS_CONTROL_ENABLE", "false")
    with public_base_url(monkeypatch, PUBLIC_BASE_URL):
        response = await protected_resource_metadata(_request("mcp"))
    assert response.status_code == 404


@pytest.mark.usefixtures("_surfaces")
async def test_nothing_advertised_when_no_provider_declares_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    install_access_control(monkeypatch)
    with tai42_app.bound(_FakeApp()), public_base_url(monkeypatch, PUBLIC_BASE_URL):
        response = await protected_resource_metadata(_request("mcp"))
    assert response.status_code == 404


# -- the served route --------------------------------------------------------------------


@pytest.fixture
def _served_door(monkeypatch: pytest.MonkeyPatch) -> None:
    access = install_access_control(monkeypatch)
    # An operator's own row mapping the document path to a protected scope changes nothing:
    # the route is public by its own declaration.
    access.pg.add_route(f"{DOC}/mcp", "operator-scope")
    install_declaring_provider(monkeypatch, DeclaringTokenProvider())
    monkeypatch.setenv("ACCESS_CONTROL_PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    monkeypatch.setattr(app, "_serving_selection", app._serving_selection)


@pytest.mark.usefixtures("_served_door")
@pytest.mark.parametrize("shell_tier", ["true", "false"])
async def test_the_served_document_is_public_on_a_lean_boot(monkeypatch: pytest.MonkeyPatch, shell_tier: str) -> None:
    monkeypatch.setenv("ACCESS_CONTROL_SPA_SHELL_PUBLIC", shell_tier)
    async with served(monkeypatch, manifest("none")) as server:
        await app.sub_app.mcp_sub_app_router.register_sub_mcp_app("known", ["greet"], transport="http")  # type: ignore[attr-defined]
        async with httpx.AsyncClient(base_url=server.base_url, timeout=REQUEST_TIMEOUT_S) as client:
            main = await client.get(f"{DOC}/mcp", headers={"Host": "internal-host:9000"})
            head = await client.head(f"{DOC}/mcp")
            known = await client.get(f"{DOC}/app/known")
            unknown = await client.get(f"{DOC}/app/never-registered")
            message_prefix = await client.get(f"{DOC}/messages")
    assert main.status_code == 200, main.text
    assert main.json()["resource"] == f"{PUBLIC_BASE_URL}/mcp"
    assert main.headers["cache-control"] == "no-store"
    assert head.status_code == 200
    assert known.status_code == unknown.status_code == 200
    assert known.json()["resource"] == f"{PUBLIC_BASE_URL}/app/known"
    assert unknown.json()["resource"] == f"{PUBLIC_BASE_URL}/app/never-registered"
    assert {key: value for key, value in known.json().items() if key != "resource"} == {
        key: value for key, value in unknown.json().items() if key != "resource"
    }
    assert message_prefix.status_code == 404


@pytest.mark.usefixtures("_served_door")
async def test_a_presented_invalid_credential_is_refused_on_the_document(monkeypatch: pytest.MonkeyPatch) -> None:
    async with (
        served(monkeypatch, manifest("none")) as server,
        httpx.AsyncClient(base_url=server.base_url, timeout=REQUEST_TIMEOUT_S) as client,
    ):
        response = await client.get(f"{DOC}/mcp", headers={"Authorization": "Bearer not-a-valid-key"})
    assert response.status_code == 401
