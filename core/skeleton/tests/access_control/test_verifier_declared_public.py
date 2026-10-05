"""The verifier's declared-public tier: a route DECLARED public answers
unauthenticated, per method, regardless of owner, and never opens a reserved prefix."""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tai42_skeleton.access_control import store as store_module
from tai42_skeleton.access_control import verifier as verifier_module
from tai42_skeleton.access_control.settings import AccessControlSettings
from tai42_skeleton.access_control.verifier import AccessControlVerifier
from tai42_skeleton.app.route_registry import CORE_OWNER, RouteOwner, RouteRegistry

from .conftest import FakeAccessControlPg, FakeRedis, make_client_ctx, make_pg_ctx

_PLUGIN_OWNER = RouteOwner(kind="plugin", owner_ref="acme/one", item_name="web")


async def _handler(request: Request) -> Response:
    """A plain handler."""
    return JSONResponse({"data": {}})


@pytest.fixture(autouse=True)
def _reset_reserved_memo():
    verifier_module.reset_registered_reserved_paths()
    yield
    verifier_module.reset_registered_reserved_paths()


def _record(
    registry: RouteRegistry, path: str, methods: list[str], *, public: bool, owner: RouteOwner = _PLUGIN_OWNER
) -> None:
    registry.record(
        path=path,
        methods=methods,
        name=None,
        handler=_handler,
        summary="s",
        tags=["t"],
        authed=not public,
        action=None if public else "write",
        request_model=None,
        response_model=None,
        no_body_reason="test fixture: response body not under test",
        owner=owner,
        public=public,
    )


def _plugin_registry() -> RouteRegistry:
    registry = RouteRegistry()
    # A public GET and its AUTHED POST sibling share one shape.
    _record(registry, "/api/acme/one/chat/{id}", ["GET"], public=True)
    _record(registry, "/api/acme/one/chat/{id}", ["POST"], public=False)
    return registry


def _wire(monkeypatch, registry: RouteRegistry, pg: FakeAccessControlPg) -> None:
    monkeypatch.setattr(verifier_module, "route_registry", registry)
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(pg))
    monkeypatch.setattr(verifier_module, "client_ctx", make_client_ctx(FakeRedis()))


async def test_declared_public_route_resolves_public_anonymously(monkeypatch) -> None:
    settings = AccessControlSettings()
    v = AccessControlVerifier(settings, providers=[])
    _wire(monkeypatch, _plugin_registry(), FakeAccessControlPg())
    ids = await v.resolve_resource_ids("/api/acme/one/chat/42", "GET")
    assert ids == [settings.public_resource_id]


async def test_sibling_authed_method_is_not_opened_by_the_tier(monkeypatch) -> None:
    settings = AccessControlSettings()
    v = AccessControlVerifier(settings, providers=[])
    _wire(monkeypatch, _plugin_registry(), FakeAccessControlPg())
    # The POST sibling is declared authed: the declared-public tier does not match it,
    # and with no route row it resolves to nothing (gated).
    ids = await v.resolve_resource_ids("/api/acme/one/chat/42", "POST")
    assert ids == []


async def test_declared_public_tier_never_opens_a_reserved_prefix(monkeypatch) -> None:
    settings = AccessControlSettings()
    v = AccessControlVerifier(settings, providers=[])
    registry = RouteRegistry()
    # A public route resolved under the reserved /api/auth prefix must NOT be served
    # public — the tier's reserved guard drops it, mirroring the mount-map boot-fail.
    _record(registry, "/api/auth/backdoor", ["GET"], public=True)
    _wire(monkeypatch, registry, FakeAccessControlPg())
    ids = await v.resolve_resource_ids("/api/auth/backdoor", "GET")
    assert ids == []


async def test_core_public_route_is_granted_by_the_declared_public_tier(monkeypatch) -> None:
    settings = AccessControlSettings()
    v = AccessControlVerifier(settings, providers=[])
    registry = RouteRegistry()
    # The tier is owner-agnostic: a core-owned authed=False /api route is granted the
    # public id by DECLARATION, with no route row and no path pattern.
    _record(registry, "/api/core/thing", ["GET"], public=True, owner=CORE_OWNER)
    _wire(monkeypatch, registry, FakeAccessControlPg())
    ids = await v.resolve_resource_ids("/api/core/thing", "GET")
    assert ids == [settings.public_resource_id]


async def test_core_public_templated_route_needs_no_path_pattern(monkeypatch) -> None:
    settings = AccessControlSettings()
    # An empty pattern table proves the grant flows from the declaration alone, not a row.
    assert settings.compiled_patterns == []
    v = AccessControlVerifier(settings, providers=[])
    registry = RouteRegistry()
    _record(registry, "/api/core/item/{item_id}", ["GET"], public=True, owner=CORE_OWNER)
    _wire(monkeypatch, registry, FakeAccessControlPg())
    ids = await v.resolve_resource_ids("/api/core/item/abc123", "GET")
    assert ids == [settings.public_resource_id]


async def test_authed_route_stays_gated_regardless_of_owner(monkeypatch) -> None:
    settings = AccessControlSettings()
    v = AccessControlVerifier(settings, providers=[])
    registry = RouteRegistry()
    # A core-owned authed route is not declared public: the tier does not match it and,
    # with no route row, it resolves to nothing (gated) — the owner-agnostic mirror.
    _record(registry, "/api/core/thing", ["POST"], public=False, owner=CORE_OWNER)
    _wire(monkeypatch, registry, FakeAccessControlPg())
    ids = await v.resolve_resource_ids("/api/core/thing", "POST")
    assert ids == []


# -- Non-/api doors: the webhook and trigger ingress, public by declaration, every shape --


def _ingress_registry() -> RouteRegistry:
    registry = RouteRegistry()
    # The two non-/api public ingress doors, each serving POST and GET.
    _record(registry, "/universal_webhook/{topic}", ["POST", "GET"], public=True, owner=CORE_OWNER)
    _record(registry, "/trigger/{token}", ["POST", "GET"], public=True, owner=CORE_OWNER)
    return registry


async def test_non_api_public_webhook_door_is_public_per_method(monkeypatch) -> None:
    # ``spa_shell_public=False`` so the shell tier can admit nothing — any public id here is
    # the declared-public tier granting a non-/api templated door by its declaration.
    settings = AccessControlSettings(spa_shell_public=False)
    v = AccessControlVerifier(settings, providers=[])
    _wire(monkeypatch, _ingress_registry(), FakeAccessControlPg())
    for method in ("POST", "GET", "HEAD"):
        assert await v.resolve_resource_ids("/universal_webhook/events", method) == [settings.public_resource_id], (
            method
        )
    # A method the door does not declare is not opened by the tier.
    assert await v.resolve_resource_ids("/universal_webhook/events", "PUT") == []


async def test_non_api_public_trigger_door_is_public_per_method(monkeypatch) -> None:
    settings = AccessControlSettings(spa_shell_public=False)
    v = AccessControlVerifier(settings, providers=[])
    _wire(monkeypatch, _ingress_registry(), FakeAccessControlPg())
    for method in ("POST", "GET"):
        assert await v.resolve_resource_ids("/trigger/tok", method) == [settings.public_resource_id], method


async def test_concrete_non_api_public_probe_needs_no_acknowledgment(monkeypatch) -> None:
    # /health is public by its registration; the runtime no longer consults
    # ``acknowledged_public_routes`` — an empty acknowledged list still serves it public.
    settings = AccessControlSettings(spa_shell_public=False, acknowledged_public_routes=())
    v = AccessControlVerifier(settings, providers=[])
    registry = RouteRegistry()
    _record(registry, "/health", ["GET"], public=True, owner=CORE_OWNER)
    _wire(monkeypatch, registry, FakeAccessControlPg())
    for method in ("GET", "HEAD"):
        assert await v.resolve_resource_ids("/health", method) == [settings.public_resource_id], method


async def test_protected_row_does_not_re_protect_a_declared_public_non_api_door(monkeypatch) -> None:
    # Precedence: the declaration is authoritative above the route table, so a later
    # protected row on the webhook door never re-protects it (the tier short-circuits).
    settings = AccessControlSettings(spa_shell_public=False)
    v = AccessControlVerifier(settings, providers=[])
    pg = FakeAccessControlPg()
    pg.add_route("/universal_webhook/events", "locked")
    _wire(monkeypatch, _ingress_registry(), pg)
    assert await v.resolve_resource_ids("/universal_webhook/events", "POST") == [settings.public_resource_id]


async def test_authed_non_api_route_resolves_universal_scope(monkeypatch) -> None:
    # A registered authed=True non-/api route with no row resolves to the universal scope via
    # the declared-protection tier (a role-holder reaches it); the same path unregistered
    # resolves to nothing. The declared-protection tier reads the role gate's served surface,
    # so point that at the same registry and rebuild its index.
    from tai42_skeleton.access_control import role_gate

    settings = AccessControlSettings(spa_shell_public=False)
    v = AccessControlVerifier(settings, providers=[])
    registry = RouteRegistry()
    _record(registry, "/inbound", ["POST"], public=False, owner=CORE_OWNER)
    _wire(monkeypatch, registry, FakeAccessControlPg())
    monkeypatch.setattr(role_gate, "load_all_routes", registry.routes)
    role_gate.reset_route_index()
    try:
        assert await v.resolve_resource_ids("/inbound", "POST") == ["*"]
        empty = RouteRegistry()
        monkeypatch.setattr(verifier_module, "route_registry", empty)
        monkeypatch.setattr(role_gate, "load_all_routes", empty.routes)
        role_gate.reset_route_index()
        assert await v.resolve_resource_ids("/inbound", "POST") == []
    finally:
        role_gate.reset_route_index()
