"""The provider-door routes: their declared action class, the bodies their HTTP edge parses, the
request-derived origin a look-up receives, and the client secret kept out of every answer."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request

import tai42_skeleton.routers.connectors as router
from tai42_skeleton.app.route_registry import load_all_routes
from tai42_skeleton.connectors.oauth.discovery import DiscoveredAuthorization
from tai42_skeleton.operations import connector_providers as ops

_DOOR_ROUTES = {
    ("POST", "/api/connectors/providers/discover"),
    ("POST", "/api/connectors/providers"),
    ("DELETE", "/api/connectors/providers/{provider_id}"),
    ("PUT", "/api/connectors/providers/{provider_id}/client"),
}

SECRET = "route-secret-value-123"


@pytest.fixture(autouse=True)
def _connector_store_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "x")


def _req(body: Any = None, *, origin: str | None = "https://app.example.com", **path_params: str) -> Request:
    async def _json() -> Any:
        return body

    headers = {} if origin is None else {"origin": origin}
    return cast(
        Request,
        SimpleNamespace(
            json=_json,
            path_params=path_params,
            query_params={},
            headers=headers,
            base_url="http://testserver/",
        ),
    )


def _body(resp: Any) -> dict:
    return json.loads(bytes(resp.body))


def _found() -> DiscoveredAuthorization:
    return DiscoveredAuthorization(
        issuer="https://auth.example.com",
        authorization_endpoint="https://auth.example.com/authorize",
        token_endpoint="https://auth.example.com/token",
        registration_endpoint=None,
        revocation_endpoint=None,
        resource="https://mcp.example.com/mcp",
        resource_name=None,
        challenge_scopes=None,
        scopes_supported=(),
        offline_access_supported=False,
        token_endpoint_auth_methods_supported=None,
        client_id_document_supported=False,
        iss_parameter_supported=False,
        authorization_servers=("https://auth.example.com",),
    )


def test_the_four_routes_are_authed_and_fenced() -> None:
    routes = {(method, meta.path): meta for meta in load_all_routes() for method in meta.methods}
    for route in _DOOR_ROUTES:
        assert route in routes, f"{route} is not registered"
        assert routes[route].authed, route
        assert routes[route].action == "fenced", route


def test_the_add_and_remove_routes_are_reload_gated_and_the_look_up_is_not() -> None:
    routes = {(method, meta.path): meta for meta in load_all_routes() for method in meta.methods}
    assert routes[("POST", "/api/connectors/providers")].reload_gated is True
    assert routes[("DELETE", "/api/connectors/providers/{provider_id}")].reload_gated is True
    assert routes[("POST", "/api/connectors/providers/discover")].reload_gated is False


async def test_a_look_up_hands_the_request_origin_to_the_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def discover(url: str, *, fresh: bool = False, bound_issuer: str | None = None) -> DiscoveredAuthorization:
        seen.update(url=url, fresh=fresh)
        return _found()

    async def taken() -> set[str]:
        return set()

    monkeypatch.setenv("CONNECTORS_REDIRECT_URI_ALLOWLIST", "https://app.example.com")
    from tai42_kit.settings import reset_all_settings

    reset_all_settings()
    monkeypatch.setattr(ops.discovery, "discover", discover)
    monkeypatch.setattr(ops.provider_service, "taken_provider_ids", taken)
    resp = await router.discover_provider_server(_req({"url": "https://mcp.example.com/mcp"}))
    assert resp.status_code == 200, _body(resp)
    data = _body(resp)["data"]
    assert seen == {"url": "https://mcp.example.com/mcp", "fresh": True}
    assert data["redirect_uri"] == "https://app.example.com/oauth-bridge.html"
    assert data["suggested_id"] == "mcp_example_com"


@pytest.mark.parametrize("body", [{}, {"url": "https://a.example/mcp", "provider_id": "x"}, [1], "nope"])
async def test_a_look_up_body_that_is_not_one_address_or_one_provider_is_a_400(body: Any) -> None:
    resp = await router.discover_provider_server(_req(body))
    assert resp.status_code == 400


async def test_an_add_body_maps_its_id_to_the_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops.provider_service, "id_taken_reason", _reason)
    monkeypatch.setattr(ops.discovery, "discover", _discover_ok)
    resp = await router.add_provider(
        _req({"url": "https://mcp.example.com/mcp", "id": "remote", "scopes": ["a", "b"], "category": "data"})
    )
    assert resp.status_code == 409
    assert "a provider with this id is in the manifest" in _body(resp)["error"]


async def _reason(provider_id: str) -> str | None:
    return "a provider with this id is in the manifest" if provider_id == "remote" else None


async def _discover_ok(url: str, *, fresh: bool = False, bound_issuer: str | None = None) -> DiscoveredAuthorization:
    return _found()


async def test_an_add_body_with_a_bad_id_is_a_400_naming_the_field() -> None:
    resp = await router.add_provider(_req({"url": "https://mcp.example.com/mcp", "id": "Bad-Id"}))
    assert resp.status_code == 400
    assert any(field["loc"] == ["id"] for field in _body(resp)["fields"])


async def test_a_remove_reads_its_provider_from_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def unknown(provider_id: str) -> Any:
        raise KeyError(provider_id)

    async def no_rows_left(provider_id: str) -> bool:
        return False

    monkeypatch.setattr(ops.connection_service, "get_provider", unknown)
    monkeypatch.setattr(ops.provider_service, "clients_left_without_provider", no_rows_left)
    resp = await router.remove_provider(_req(provider_id="ghost"))
    assert resp.status_code == 404
    assert "ghost" in _body(resp)["error"]


async def test_a_set_client_body_never_echoes_the_secret() -> None:
    resp = await router.set_provider_client(
        _req({"client_secret": SECRET, "token_endpoint_auth_method": "private_key_jwt"}, provider_id="remote")
    )
    assert resp.status_code == 400
    assert SECRET not in bytes(resp.body).decode()
    locs = {tuple(field["loc"]) for field in _body(resp)["fields"]}
    assert ("client_id",) in locs
    assert ("token_endpoint_auth_method",) in locs


async def test_a_set_client_hands_the_secret_to_the_operation_as_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def unknown(provider_id: str) -> Any:
        raise KeyError(provider_id)

    monkeypatch.setattr(ops.connection_service, "get_provider", unknown)
    resp = await router.set_provider_client(
        _req({"client_id": "app", "client_secret": SECRET, "issuer": "https://a.example"}, provider_id="remote")
    )
    assert resp.status_code == 404
    assert SECRET not in bytes(resp.body).decode()
