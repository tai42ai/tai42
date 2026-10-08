"""Routes declare their reach and every access-control reader takes it from the declaration.

``self_service`` (a caller self-service surface the seeded editor/viewer ceilings carve in),
``any_authenticated`` (reachable by any authenticated identity whatever the route table maps)
and ``pre_auth`` (a public pre-authentication surface whose presented credential is never
verified). The settings carry no route knowledge: with their prefix and pattern defaults
empty, the login doors, the asset door and the identity-introspection route keep their
reach from the declarations alone. A made-up plugin's route modules (not any real router),
imported under their mount bindings as the plugin loader imports them, prove a plugin's
declarations are read exactly like the platform's own.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from types import ModuleType

import pytest
from fastmcp.server.auth import AccessToken
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send
from tai42_contract.app import tai42_app
from tai42_kit.utils.data import run_jq_first

from tai42_skeleton.access_control import policy as policy_module
from tai42_skeleton.access_control import role_gate
from tai42_skeleton.access_control import store as store_module
from tai42_skeleton.access_control import verifier as verifier_module
from tai42_skeleton.access_control.adapter import AuthAdapter
from tai42_skeleton.access_control.middleware import ResourceGuardMiddleware
from tai42_skeleton.access_control.roles import editor_jq, self_service_routes, viewer_jq
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.access_control.user import TaiUser
from tai42_skeleton.access_control.verifier import AccessControlVerifier
from tai42_skeleton.app.mount_map import bind_module
from tai42_skeleton.app.route_registry import _SpecApp, load_api_routes, route_registry

from ..app._fixtures import synthetic_reach_plugin
from .conftest import FakeAccessControlPg, FakeRedis, make_client_ctx, make_pg_ctx


@pytest.fixture
def synthetic_routes() -> Iterator[ModuleType]:
    """Import the made-up plugin's route modules into the process registry, each under its
    item's mount binding; drop their rows afterwards."""
    routes_before = dict(route_registry._routes)
    shapes = route_registry._shapes.committed()
    shapes_before = list(shapes)
    module_names = [name for name, _binding in synthetic_reach_plugin.ROUTE_ITEMS]
    for name in module_names:
        sys.modules.pop(name, None)
    with tai42_app.bound(_SpecApp()):
        for name, binding in synthetic_reach_plugin.ROUTE_ITEMS:
            with bind_module(binding):
                importlib.import_module(name)
    role_gate.reset_route_index()
    verifier_module.reset_registered_reserved_paths()
    try:
        yield synthetic_reach_plugin
    finally:
        for name in module_names:
            sys.modules.pop(name, None)
        route_registry._routes = routes_before
        shapes[:] = shapes_before
        role_gate.reset_route_index()
        verifier_module.reset_registered_reserved_paths()


async def _ok(_request: Request) -> Response:
    return JSONResponse({"data": {}})


async def _jq_allows(jq: str, path: str, method: str) -> bool:
    return (await run_jq_first(jq, {"request": {"path": path, "method": method}})) is True


def _by_pair() -> dict[tuple[str, str], object]:
    return {(method, meta.path): meta for meta in load_api_routes() for method in meta.methods}


# -- the platform's own declarations ------------------------------------------------


def test_platform_login_doors_declare_pre_auth() -> None:
    by_pair = _by_pair()
    for pair in (("GET", "/api/login/methods"), ("POST", "/api/login/claim")):
        meta = by_pair[pair]
        assert meta.pre_auth is True, pair  # type: ignore[attr-defined]
        assert meta.public is True, pair  # type: ignore[attr-defined]


def test_identity_introspection_route_declares_any_authenticated() -> None:
    meta = _by_pair()[("GET", "/api/auth/me")]
    assert meta.any_authenticated is True  # type: ignore[attr-defined]
    assert meta.authed is True  # type: ignore[attr-defined]


def test_platform_self_service_declarations() -> None:
    # The platform's own caller self-service surfaces under the control plane: every own-key
    # route of the api-keys subtree, the tokens payload, the mint capabilities, the identity
    # projection, claim-link creation, the read-only scopes listing and logout.
    declared = {
        (method, meta.path)
        for meta in load_api_routes()
        if meta.self_service and meta.owner.kind == "core"
        for method in meta.methods
    }
    assert declared == {
        ("GET", "/api/auth/scopes"),
        ("GET", "/api/auth/tokens-payload"),
        ("POST", "/api/auth/api-keys"),
        ("PUT", "/api/auth/api-keys/{user_id}"),
        ("DELETE", "/api/auth/api-keys/{user_id}"),
        ("POST", "/api/auth/claim-links"),
        ("GET", "/api/auth/capabilities"),
        ("GET", "/api/auth/me"),
        ("GET", "/api/auth/api-keys/{user_id}/policy/versions"),
        ("POST", "/api/auth/api-keys/{user_id}/policy/rollback"),
        ("POST", "/api/auth/api-keys/{user_id}/scopes"),
        ("POST", "/api/auth/logout"),
    }


# -- a plugin's declarations are read like the platform's ---------------------------


def test_synthetic_plugin_routes_record_their_reach(synthetic_routes: ModuleType) -> None:
    template = synthetic_routes.SELF_SERVICE_TEMPLATE
    assert route_registry.match("/api/auth/synthetic/one", "GET").self_service is True  # type: ignore[union-attr]
    assert route_registry.match("/api/auth/synthetic/one", "PUT").self_service is True  # type: ignore[union-attr]
    assert (template, frozenset({"GET", "HEAD"})) in self_service_routes()
    assert (template, frozenset({"PUT"})) in self_service_routes()
    identity = route_registry.match(synthetic_routes.ANY_AUTHENTICATED_PATH, "GET")
    assert identity is not None
    assert identity.any_authenticated is True
    entry = route_registry.match(synthetic_routes.PRE_AUTH_PATH, "POST")
    assert entry is not None
    assert (entry.pre_auth, entry.public) == (True, True)
    # Every route is recorded as the plugin's own, never as a core route.
    self_service = route_registry.match("/api/auth/synthetic/one", "GET")
    assert self_service is not None
    assert {meta.owner.kind for meta in (self_service, identity, entry)} == {"plugin"}


@pytest.mark.parametrize(
    ("method", "editor", "viewer"),
    [
        ("GET", True, True),
        ("HEAD", True, True),
        ("PUT", True, True),  # a state-changing self-service method is open to a viewer too
        ("POST", False, False),  # a method the route does not serve is carved in for no one
    ],
)
async def test_synthetic_self_service_route_reached_per_the_derived_rule(
    synthetic_routes: ModuleType, method: str, editor: bool, viewer: bool
) -> None:
    path = "/api/auth/synthetic/one"
    assert await _jq_allows(editor_jq(), path, method) is editor
    assert await _jq_allows(viewer_jq(), path, method) is viewer
    # The template binds one segment: a deeper path is not the declared route.
    assert await _jq_allows(editor_jq(), "/api/auth/synthetic/one/extra", "GET") is False


# -- the guard reads a plugin's any-authenticated declaration ------------------------


def _scoped_key_scope(path: str) -> dict:
    """An authenticated HTTP request from a scoped key holding ``reports`` and no ``*``."""
    user = TaiUser(AccessToken(token="t", client_id="scoped-key", scopes=["reports"], claims={}))
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode("ascii"),
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "user": user,
        "auth": AuthCredentials(["reports"]),
    }


async def _asgi_ok(_scope: Scope, _receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _guard_status(monkeypatch, path: str) -> int:
    """The status the resource guard answers ``path`` with, over the real verifier and no route rows."""
    settings = AccessControlSettings()
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(FakeAccessControlPg()))
    monkeypatch.setattr(policy_module, "client_ctx", make_client_ctx(FakeRedis()))
    guard = ResourceGuardMiddleware(
        _asgi_ok, AccessControlVerifier(settings, providers=[]), settings.public_resource_id
    )
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await guard(_scoped_key_scope(path), receive, send)
    return next(message["status"] for message in sent if message["type"] == "http.response.start")


async def test_a_scoped_key_reaches_a_plugin_any_authenticated_route(synthetic_routes: ModuleType, monkeypatch) -> None:
    # The authenticated route resolves to the universal scope the key does not hold, yet its
    # ``any_authenticated`` declaration admits every authenticated identity.
    assert await _guard_status(monkeypatch, synthetic_routes.ANY_AUTHENTICATED_PATH) == 200


async def test_a_scoped_key_is_refused_a_plugin_route_without_the_declaration(
    synthetic_routes: ModuleType, monkeypatch
) -> None:
    # The same key on the plugin's authenticated route that declares no ``any_authenticated``:
    # the universal scope it resolves to is not held, so the guard refuses it.
    assert await _guard_status(monkeypatch, "/api/auth/synthetic/one") == 403


# -- the login surface: a stale credential never locks a caller out ----------------


def _login_client(settings: AccessControlSettings, *paths: tuple[str, str]) -> TestClient:
    routes = [Route(path, _ok, methods=[method]) for method, path in paths]
    return TestClient(Starlette(routes=routes, middleware=AuthAdapter(settings).get_middleware()))


def test_login_door_ignores_a_stale_bearer_with_the_defaults_empty() -> None:
    settings = AccessControlSettings()
    assert settings.always_public_path_prefixes == ()
    client = _login_client(settings, ("GET", "/api/login/methods"), ("POST", "/api/login/claim"))
    assert client.get("/api/login/methods", headers={"Authorization": "Bearer sk-stale"}).status_code == 200
    assert client.post("/api/login/claim", headers={"X-Api-Key": "tai-sess-stale"}).status_code == 200


def test_a_plugin_pre_auth_door_ignores_a_stale_bearer(synthetic_routes: ModuleType) -> None:
    client = _login_client(AccessControlSettings(), ("POST", synthetic_routes.PRE_AUTH_PATH))
    resp = client.post(synthetic_routes.PRE_AUTH_PATH, headers={"Authorization": "Bearer sk-stale"})
    assert resp.status_code == 200


def test_a_public_route_without_pre_auth_still_verifies_a_presented_credential() -> None:
    # Publicness alone never skips verification: a public route keeps the caller's optional
    # identity, so a stale credential presented on it is a loud 401.
    client = _login_client(AccessControlSettings(), ("GET", "/health"))
    assert client.get("/health").status_code == 200
    assert client.get("/health", headers={"Authorization": "Bearer sk-stale"}).status_code == 401


def test_an_operator_prefix_still_feeds_the_pre_auth_skip() -> None:
    settings = AccessControlSettings(always_public_path_prefixes=("/operator-login",))
    client = _login_client(settings, ("GET", "/operator-login/form"))
    assert client.get("/operator-login/form", headers={"Authorization": "Bearer sk-stale"}).status_code == 200


# -- the asset door: public by its declaration, served methods only ----------------


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_asset_door_is_public_with_the_defaults_empty(monkeypatch, method: str) -> None:
    settings = AccessControlSettings()
    assert settings.always_public_route_patterns == ()
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(FakeAccessControlPg()))
    monkeypatch.setattr(policy_module, "client_ctx", make_client_ctx(FakeRedis()))
    verifier = AccessControlVerifier(settings, providers=[])
    ids = await verifier.resolve_resource_ids("/api/plugins/acme-panel/studio/assets/index.js", method)
    assert ids == [settings.public_resource_id]


async def test_asset_door_unserved_method_falls_to_resolution(monkeypatch) -> None:
    settings = AccessControlSettings()
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(FakeAccessControlPg()))
    monkeypatch.setattr(policy_module, "client_ctx", make_client_ctx(FakeRedis()))
    verifier = AccessControlVerifier(settings, providers=[])
    ids = await verifier.resolve_resource_ids("/api/plugins/acme-panel/studio/assets/index.js", "POST")
    assert settings.public_resource_id not in ids


# -- the control-plane prefix set comes from the mount records ---------------------


@pytest.fixture
def fresh_mount_registry(monkeypatch):
    from tai42_skeleton.app import raw_path_route, serving_core
    from tai42_skeleton.app.route_registry import RouteRegistry

    registry = RouteRegistry()
    for module in (serving_core, verifier_module, raw_path_route):
        monkeypatch.setattr(module, "route_registry", registry)
    return registry


def test_control_plane_prefixes_are_api_alone_until_a_streamable_path_is_recorded(fresh_mount_registry) -> None:
    from tai42_skeleton.app import serving_core

    assert fresh_mount_registry.control_plane_prefixes() == ("/api",)
    # The SSE transport was never part of the control-plane pair.
    serving_core.record_sse_surface("/sse", "/messages")
    assert fresh_mount_registry.control_plane_prefixes() == ("/api",)
    serving_core.record_streamable_http_surface("/mcp", stateless=False)
    assert fresh_mount_registry.control_plane_prefixes() == ("/api", "/mcp")
    # A rebuilt epoch that mounts the transport elsewhere replaces the recorded path.
    serving_core.record_streamable_http_surface("/rpc/", stateless=True)
    assert fresh_mount_registry.control_plane_prefixes() == ("/api", "/rpc")


def test_spa_shell_follows_a_non_default_streamable_path(fresh_mount_registry) -> None:
    from tai42_skeleton.app import serving_core

    serving_core.record_streamable_http_surface("/rpc", stateless=False)
    verifier = AccessControlVerifier(AccessControlSettings(), providers=[])
    assert verifier._is_spa_shell_fallback("/rpc/x", "GET") is False
    assert verifier._is_spa_shell_fallback("/rpc", "GET") is False
    assert verifier._is_spa_shell_fallback("/mcp/x", "GET") is True
    assert verifier._is_spa_shell_fallback("/api/x", "GET") is False


def test_spa_catch_all_route_follows_a_non_default_streamable_path(fresh_mount_registry) -> None:
    from starlette.routing import Match

    from tai42_skeleton.app import serving_core
    from tai42_skeleton.app.raw_path_route import SpaFallbackRoute

    serving_core.record_streamable_http_surface("/rpc", stateless=False)
    route = SpaFallbackRoute("/{spa_path:path}", endpoint=_ok, methods=["GET"])

    def _match(path: str) -> Match:
        scope = {"type": "http", "method": "GET", "path": path, "root_path": ""}
        return route.matches(scope)[0]

    assert _match("/rpc/x") is Match.NONE
    assert _match("/api/x") is Match.NONE
    assert _match("/mcp/x") is Match.FULL
