"""``package_mounts``: a studio plugin's route items and their bases, read off the committed route generation.

Driven through a real :class:`RouteRegistry` and ``HttpSurface.custom_route`` under ``bind_module``, staged and
committed the way an epoch build does, with a neutral synthetic plugin whose router item ``synthetic`` mounts at
base ``synthetic``.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from tai42_contract.plugins import PluginItem, PluginItemKind, PluginSpec, RouteDecl, RoutesDecl

from tai42_skeleton.app.http import HttpSurface
from tai42_skeleton.app.mount_map import bind_module, build_mount_map, package_mounts
from tai42_skeleton.app.route_registry import RouteRegistry

_MODULE = "synthetic_pkg.routes"
_PACKAGE = "synthetic_pkg"


async def _ping(request: Request) -> Response:
    """The synthetic plugin's one route."""
    return JSONResponse({"data": {}})


class _FakeFastMCP:
    def __init__(self) -> None:
        self._additional_http_routes: list[Route] = []

    def custom_route(self, path: str, methods: list[str], name: str | None, include_in_schema: bool):
        def decorator(fn):
            self._additional_http_routes.append(Route(path, endpoint=fn, methods=methods, name=name))
            return fn

        return decorator


class _FakeApp:
    def __init__(self) -> None:
        self._fast_mcp = _FakeFastMCP()


def _spec() -> PluginSpec:
    return PluginSpec(
        spec_version=1,
        namespace="acme",
        name="synthetic",
        package="acme-synthetic",
        version="1.0.0",
        description="a synthetic router plugin",
        license="MIT",
        contract=">=2,<3",
        categories=["utilities"],
        provides=[
            PluginItem(
                kind=PluginItemKind.ROUTER,
                name="synthetic",
                module=_MODULE,
                description="the synthetic router item",
                routes=RoutesDecl(base="synthetic", paths=[RouteDecl(path="/ping", methods=["GET"], public=False)]),
            )
        ],
    )


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> RouteRegistry:
    fresh = RouteRegistry()
    monkeypatch.setattr("tai42_skeleton.app.http.route_registry", fresh)
    spec = _spec()
    monkeypatch.setattr(
        "tai42_skeleton.app.mount_map._packaged_spec_for_module",
        lambda module: spec if module.partition(".")[0] == _PACKAGE else None,
    )
    return fresh


def _register_pass(overrides: dict[str, dict[str, str]]) -> None:
    """One registration pass of the synthetic module under the mount map ``overrides`` resolve to."""
    binding = build_mount_map([_MODULE], ("/api/auth",), lambda: overrides)[_MODULE]
    surface = HttpSurface(_FakeApp())  # type: ignore[arg-type]
    with bind_module(binding):
        surface.custom_route(
            "/ping", ["GET"], summary="ping", tags=["synthetic"], response_model=None, no_body_reason="fixture"
        )(_ping)


def _bases_in_routes(registry: RouteRegistry) -> set[str]:
    return {meta.path for meta in registry.routes() if meta.path.endswith("/ping")}


def test_boot_serves_the_declared_base(registry: RouteRegistry) -> None:
    _register_pass({})
    assert package_mounts(_PACKAGE, registry.live_routes()) == {"synthetic": "/api/synthetic"}


def test_a_remap_reads_the_new_base_while_the_dedup_map_holds_both(registry: RouteRegistry) -> None:
    _register_pass({})
    registry.begin_shape_staging()
    _register_pass({"acme/synthetic": {"synthetic": "synthetic-alt"}})
    registry.commit_shape_staging()
    assert package_mounts(_PACKAGE, registry.live_routes()) == {"synthetic": "/api/synthetic-alt"}
    assert _bases_in_routes(registry) == {"/api/synthetic/ping", "/api/synthetic-alt/ping"}


def test_a_dropped_router_has_no_mount_while_the_dedup_map_keeps_its_rows(registry: RouteRegistry) -> None:
    _register_pass({})
    registry.begin_shape_staging()
    registry.commit_shape_staging()
    assert package_mounts(_PACKAGE, registry.live_routes()) == {}
    assert _bases_in_routes(registry) == {"/api/synthetic/ping"}


def test_a_failed_build_keeps_the_previous_generation(registry: RouteRegistry) -> None:
    _register_pass({})
    registry.begin_shape_staging()
    _register_pass({"acme/synthetic": {"synthetic": "synthetic-alt"}})
    registry.abort_shape_staging()
    assert package_mounts(_PACKAGE, registry.live_routes()) == {"synthetic": "/api/synthetic"}


def test_two_bases_for_one_item_in_one_generation_raise(registry: RouteRegistry) -> None:
    _register_pass({})
    _register_pass({"acme/synthetic": {"synthetic": "synthetic-alt"}})
    with pytest.raises(RuntimeError, match=r"route item 'synthetic' of acme/synthetic is served under two bases"):
        package_mounts(_PACKAGE, registry.live_routes())


def test_a_package_shipping_no_plugin_spec_has_no_mounts(registry: RouteRegistry) -> None:
    _register_pass({})
    assert package_mounts("operator_pkg", registry.live_routes()) == {}


def test_a_core_route_carries_no_mount_base(registry: RouteRegistry) -> None:
    surface = HttpSurface(_FakeApp())  # type: ignore[arg-type]
    surface.custom_route(
        "/api/core-thing", ["GET"], summary="core", tags=["core"], response_model=None, no_body_reason="fixture"
    )(_ping)
    (meta,) = registry.live_routes()
    assert meta.mount_base is None
