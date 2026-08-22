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
