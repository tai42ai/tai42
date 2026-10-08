"""``RouteRegistry.serves_operation``: the committed route generation is the served surface."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tai42_skeleton.app.route_registry import CORE_OWNER, RouteOwner, RouteRegistry

_PLUGIN = RouteOwner(kind="plugin", owner_ref="acme/one", item_name="relay")


async def _handler(request: Request) -> Response:
    """A plain handler."""
    return JSONResponse({"data": {}})


def _record(registry: RouteRegistry, path: str, name: str, owner: RouteOwner = CORE_OWNER) -> None:
    registry.record(
        path=path,
        methods=["GET"],
        name=name,
        handler=_handler,
        summary="s",
        tags=["t"],
        authed=True,
        action="read",
        request_model=None,
        response_model=None,
        no_body_reason="test fixture: response body not under test",
        owner=owner,
    )


def test_a_core_route_of_the_operation_is_served_once_committed() -> None:
    registry = RouteRegistry()
    registry.begin_shape_staging()
    _record(registry, "/api/gizmos/status", "gizmo_status")
    assert registry.serves_operation("gizmo_status") is False
    registry.commit_shape_staging()
    assert registry.serves_operation("gizmo_status") is True
    assert registry.serves_operation("gizmo_other") is False


def test_a_generation_that_omits_the_route_no_longer_serves_it() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/gizmos/status", "gizmo_status")
    assert registry.serves_operation("gizmo_status") is True
    registry.begin_shape_staging()
    _record(registry, "/api/gizmos/other", "gizmo_other")
    registry.commit_shape_staging()
    assert registry.serves_operation("gizmo_status") is False
    assert registry.serves_operation("gizmo_other") is True


def test_an_aborted_generation_keeps_the_served_one() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/gizmos/status", "gizmo_status")
    registry.begin_shape_staging()
    registry.abort_shape_staging()
    assert registry.serves_operation("gizmo_status") is True


def test_a_plugin_owned_route_of_the_same_name_is_not_counted() -> None:
    registry = RouteRegistry()
    _record(registry, "/api/acme/one/status", "gizmo_status", owner=_PLUGIN)
    assert registry.serves_operation("gizmo_status") is False
