"""``HttpSurface.custom_route`` mount resolution: relative-path resolution under a
binding, explicit-authed rejection, undeclared/forbidden rejection, and the
core-route passthrough off a binding."""

from __future__ import annotations

from typing import cast

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from tai42_contract.plugins import RouteDecl

from tai42_skeleton.app.http import HttpSurface
from tai42_skeleton.app.mount_map import MountBinding, MountRegistrationError, bind_module
from tai42_skeleton.app.route_registry import RouteOwner, RouteRegistry
from tai42_skeleton.app.server import TaiMCP


async def _handler(request: Request) -> Response:
    """A plain handler."""
    return JSONResponse({"data": {}})


class _FakeFastMCP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        # Mirror FastMCP's real route table so the savepoint/rollback path exercises
        # the same append-then-truncate contract the production surface reaches into.
        self._additional_http_routes: list[Route] = []

    def custom_route(self, path: str, methods: list[str], name: str | None, include_in_schema: bool):
        self.calls.append((path, tuple(methods)))

        def decorator(fn):
            self._additional_http_routes.append(
                Route(path, endpoint=fn, methods=methods, name=name, include_in_schema=include_in_schema)
            )
            return fn

        return decorator


class _FakeApp:
    def __init__(self) -> None:
        self._fast_mcp = _FakeFastMCP()


@pytest.fixture
def surface(monkeypatch: pytest.MonkeyPatch) -> HttpSurface:
    registry = RouteRegistry()
    monkeypatch.setattr("tai42_skeleton.app.http.route_registry", registry)
    app = _FakeApp()
    bound = HttpSurface(app)  # type: ignore[arg-type]
    bound._registry = registry  # type: ignore[attr-defined]  # test-only handle
    return bound


def _register(surface: HttpSurface, path: str, methods: list[str], **extra: object):
    return surface.custom_route(
        path,
        methods,
        summary="s",
        tags=["t"],
        response_model=None,
        **extra,  # type: ignore[arg-type]
    )(_handler)


def _binding(
    base: str = "acme/one", *, path: str = "/ping", public: bool = True, forbidden: bool = False
) -> MountBinding:
    routes = () if forbidden else (RouteDecl(path=path, methods=["GET"], public=public),)
    return MountBinding("acme/one", "web", "" if forbidden else base, routes, forbidden=forbidden)


def test_core_route_off_binding_keeps_absolute_path_and_defaults_authed_true(surface: HttpSurface) -> None:
    _register(surface, "/api/thing", ["POST"], action="write")
    fast_mcp = cast(_FakeFastMCP, surface._app._fast_mcp)  # type: ignore[attr-defined]
    assert fast_mcp.calls[0][0] == "/api/thing"
    meta = surface._registry.match("/api/thing", "POST")  # type: ignore[attr-defined]
    assert meta is not None
    assert meta.authed is True
    assert meta.owner.kind == "core"


def test_declared_public_plugin_route_resolves_to_absolute_public(surface: HttpSurface) -> None:
    with bind_module(_binding()):
        _register(surface, "/ping", ["GET"])
    fast_mcp = cast(_FakeFastMCP, surface._app._fast_mcp)  # type: ignore[attr-defined]
    assert fast_mcp.calls[0][0] == "/api/acme/one/ping"
    meta = surface._registry.match("/api/acme/one/ping", "GET")  # type: ignore[attr-defined]
    assert meta is not None
    assert meta.public is True
    assert meta.authed is False
    assert meta.owner == RouteOwner(kind="plugin", owner_ref="acme/one", item_name="web")


def test_declared_authed_plugin_route_resolves_to_absolute_authed(surface: HttpSurface) -> None:
    with bind_module(_binding(path="/gate", public=False)):
        _register(surface, "/gate", ["GET"], action="read")
    meta = surface._registry.match("/api/acme/one/gate", "GET")  # type: ignore[attr-defined]
    assert meta is not None
    assert meta.public is False
    assert meta.authed is True


def test_explicit_authed_on_declared_route_is_rejected(surface: HttpSurface) -> None:
    with pytest.raises(MountRegistrationError, match="explicit authed"), bind_module(_binding()):
        _register(surface, "/ping", ["GET"], authed=True)


def test_undeclared_route_under_binding_is_rejected(surface: HttpSurface) -> None:
    with pytest.raises(MountRegistrationError, match="not declared"), bind_module(_binding()):
        _register(surface, "/nope", ["GET"])


def test_route_from_route_less_item_is_rejected(surface: HttpSurface) -> None:
    with pytest.raises(MountRegistrationError, match="declares no routes"), bind_module(_binding(forbidden=True)):
        _register(surface, "/whatever", ["GET"])


def test_mount_base_returns_resolved_base_under_binding(surface: HttpSurface) -> None:
    with bind_module(_binding()):
        assert surface.mount_base() == "/api/acme/one"
        _register(surface, "/ping", ["GET"])


def test_mount_base_follows_the_override_base(surface: HttpSurface) -> None:
    with bind_module(_binding(base="acme/remapped")):
        assert surface.mount_base() == "/api/acme/remapped"
        _register(surface, "/ping", ["GET"])


def test_mount_base_raises_off_binding(surface: HttpSurface) -> None:
    with pytest.raises(MountRegistrationError, match="no mount binding present"):
        surface.mount_base()


def test_mount_base_raises_from_route_less_item(surface: HttpSurface) -> None:
    with pytest.raises(MountRegistrationError, match="declares no routes"), bind_module(_binding(forbidden=True)):
        surface.mount_base()


def test_declared_route_resolves_through_the_full_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    # The production door: tai42_app.http / TaiMCP.http -> HttpFacet -> HttpSurface.
    # A declared plugin route registers cleanly with NO explicit authed, resolving to
    # the mounted absolute path and public (authed False) from its declaration.
    registry = RouteRegistry()
    monkeypatch.setattr("tai42_skeleton.app.http.route_registry", registry)
    app = TaiMCP(name="mount-facade")

    with bind_module(_binding()):
        app.http.custom_route("/ping", ["GET"], summary="s", tags=["t"], response_model=None)(_handler)

    meta = registry.match("/api/acme/one/ping", "GET")
    assert meta is not None
    assert meta.public is True
    assert meta.authed is False
    assert meta.owner == RouteOwner(kind="plugin", owner_ref="acme/one", item_name="web")


def _fastmcp_paths(surface: HttpSurface) -> list[str]:
    fast_mcp = cast(_FakeFastMCP, surface._app._fast_mcp)  # type: ignore[attr-defined]
    return [route.path for route in fast_mcp._additional_http_routes]


def _import_rows(surface: HttpSurface, binding: MountBinding, *paths: str) -> None:
    """Mimic one bound module import: register each relative ``path`` under the binding,
    letting a custom_route raise (or bind_module's completeness check on clean exit)
    propagate out exactly as ``_import_additive_plugin`` sees it."""
    with bind_module(binding):
        for path in paths:
            _register(surface, path, ["GET"])


def test_rollback_clears_a_module_that_raised_mid_import(surface: HttpSurface) -> None:
    # A bound module registers declared row A, then a later custom_route for an
    # UNDECLARED row raises — mirroring a module quarantined mid-import. The
    # quarantined module must serve nothing.
    binding = _binding()  # declares only /ping
    savepoint = surface.route_table_savepoint()
    with pytest.raises(MountRegistrationError, match="not declared"):
        _import_rows(surface, binding, "/ping", "/leak")  # /ping commits, /leak raises

    registry = cast(RouteRegistry, surface._registry)  # type: ignore[attr-defined]
    # Before rollback the row committed ahead of the raising one is live across every
    # surface — the partial-registration leak rollback must clear.
    assert registry.match("/api/acme/one/ping", "GET") is not None
    assert "/api/acme/one/ping" in _fastmcp_paths(surface)

    # Rollback deregisters the quarantined module from all three surfaces.
    surface.rollback_module_routes(binding, savepoint)
    assert registry.match("/api/acme/one/ping", "GET") is None
    assert "/api/acme/one/ping" not in _fastmcp_paths(surface)
    assert {meta.path for meta in registry.routes()} == set()


def test_rollback_clears_a_module_failing_the_completeness_check(surface: HttpSurface) -> None:
    # The OTHER failure shape — the module imports cleanly but a declared row was
    # never registered, so bind_module's _verify_all_registered raises on clean exit.
    binding = MountBinding(
        "acme/one",
        "web",
        "acme/one",
        (
            RouteDecl(path="/ping", methods=["GET"], public=True),
            RouteDecl(path="/pong", methods=["POST"], public=True),
        ),
    )
    savepoint = surface.route_table_savepoint()
    with pytest.raises(MountRegistrationError, match="never registered"), bind_module(binding):
        _register(surface, "/ping", ["GET"])  # only one of two declared rows registered

    registry = cast(RouteRegistry, surface._registry)  # type: ignore[attr-defined]
    assert registry.match("/api/acme/one/ping", "GET") is not None  # committed before the raise
    surface.rollback_module_routes(binding, savepoint)
    assert registry.match("/api/acme/one/ping", "GET") is None
    assert _fastmcp_paths(surface) == []


def test_rollback_only_truncates_this_modules_routes(surface: HttpSurface) -> None:
    # A healthy earlier module's routes sit below the savepoint and survive a later
    # module's rollback.
    keep = MountBinding("acme/keep", "web", "acme/keep", (RouteDecl(path="/stable", methods=["GET"], public=True),))
    with bind_module(keep):
        _register(surface, "/stable", ["GET"])
    savepoint = surface.route_table_savepoint()
    failing = _binding(base="acme/gone")
    with pytest.raises(MountRegistrationError):
        _import_rows(surface, failing, "/ping", "/leak")
    surface.rollback_module_routes(failing, savepoint)
    assert "/api/acme/keep/stable" in _fastmcp_paths(surface)
    assert "/api/acme/gone/ping" not in _fastmcp_paths(surface)


def test_rollback_rejects_a_savepoint_outside_the_table(surface: HttpSurface) -> None:
    with pytest.raises(ValueError, match="savepoint"):
        surface.rollback_module_routes(_binding(), 99)


def test_core_route_through_the_facade_defaults_authed_true(monkeypatch: pytest.MonkeyPatch) -> None:
    # Off a binding, the facade's authed default (None) resolves to True at the
    # surface, so a core caller stays authenticated by default.
    registry = RouteRegistry()
    monkeypatch.setattr("tai42_skeleton.app.http.route_registry", registry)
    app = TaiMCP(name="mount-facade-core")

    app.http.custom_route("/api/thing", ["POST"], summary="s", tags=["t"], response_model=None, action="write")(
        _handler
    )

    meta = registry.match("/api/thing", "POST")
    assert meta is not None
    assert meta.authed is True
    assert meta.owner.kind == "core"
