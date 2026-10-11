"""Route-metadata registry tests — the record-time behavior both gates depend on.

Each route DECLARES its behavioral OpenAPI properties (``reload_gated`` /
``reads_body`` / error statuses / success status) through
:class:`DeclaredRouteMetadata`; a route that declares nothing records trivial
defaults. The per-method success content type is the route's own
``success_media_types`` declaration. These tests pin the declared/default handling,
the media-type declaration, and the record-time minimum-bar enforcement.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tai42_skeleton.app.route_registry import DeclaredRouteMetadata, RouteRegistry, load_api_routes


async def _plain(request: Request) -> Response:
    """A plain read."""
    return JSONResponse({"data": []})


# -- Declared / default behavioral metadata ----------------------------------


def test_declared_metadata_populates_the_record() -> None:
    registry = RouteRegistry()
    registry.record(
        path="/api/thing",
        methods=["POST"],
        name="submit",
        handler=_plain,
        summary="Thing",
        tags=["t"],
        authed=True,
        action="write",
        request_model=None,
        response_model=None,
        no_body_reason="test fixture: response body not under test",
        declared=DeclaredRouteMetadata(
            reload_gated=True,
            reads_body=True,
            error_statuses=(400, 401, 503),
            success_status=202,
        ),
    )
    (meta,) = registry.routes()
    assert meta.reload_gated is True
    assert meta.reads_body is True
    assert meta.error_statuses == (400, 401, 503)
    assert meta.success_status == 202
    assert meta.name == "submit"


def test_undeclared_route_records_trivial_defaults() -> None:
    # A route that declares nothing (one outside the /api/* spec surface) records
    # trivial defaults — its behavioral metadata is never emitted.
    registry = RouteRegistry()
    registry.record(
        path="/health",
        methods=["GET"],
        name=None,
        handler=_plain,
        summary="Health",
        tags=["t"],
        authed=False,
        request_model=None,
        response_model=None,
        no_body_reason="test fixture: response body not under test",
    )
    (meta,) = registry.routes()
    assert meta.reload_gated is False
    assert meta.reads_body is False
    assert meta.error_statuses == ()
    assert meta.success_status == 200


def test_docstring_becomes_description() -> None:
    registry = RouteRegistry()
    registry.record(
        path="/api/plain",
        methods=["GET"],
        name=None,
        handler=_plain,
        summary="Plain",
        tags=["t"],
        authed=False,
        request_model=None,
        response_model=None,
        no_body_reason="test fixture: response body not under test",
    )
    (meta,) = registry.routes()
    assert meta.description == "A plain read."


# -- Success media-type declaration ------------------------------------------


def _record_media(
    registry: RouteRegistry,
    methods: list[str],
    media: dict[str, tuple[str, ...]] | None,
    *,
    enveloped: bool = True,
) -> None:
    from pydantic import BaseModel

    class _Body(BaseModel):
        ok: bool

    registry.record(
        path="/api/declared",
        methods=methods,
        name=None,
        handler=_plain,
        summary="Declared",
        tags=["t"],
        authed=False,
        request_model=None,
        response_model=_Body,
        enveloped=enveloped,
        success_media_types=media,
    )


def test_an_undeclared_method_publishes_json() -> None:
    registry = RouteRegistry()
    _record_media(registry, ["GET", "POST"], None)
    (meta,) = registry.routes()
    assert meta.success_media_types == {"GET": ("application/json",), "POST": ("application/json",)}


def test_a_declared_method_publishes_its_types_and_the_rest_json() -> None:
    registry = RouteRegistry()
    _record_media(registry, ["GET", "POST"], {"GET": ("text/csv", "application/octet-stream")})
    (meta,) = registry.routes()
    assert meta.success_media_types == {"GET": ("text/csv", "application/octet-stream"), "POST": ("application/json",)}


def test_a_media_key_outside_the_methods_is_refused() -> None:
    with pytest.raises(ValueError, match="names method 'POST', which the route does not serve"):
        _record_media(RouteRegistry(), ["GET"], {"POST": ("text/plain",)})


def test_an_empty_media_tuple_is_refused() -> None:
    with pytest.raises(ValueError, match="must name at least one media type"):
        _record_media(RouteRegistry(), ["GET"], {"GET": ()})


def test_a_non_json_type_on_a_raw_json_body_is_refused() -> None:
    with pytest.raises(ValueError, match="non-JSON type on an enveloped=False route"):
        _record_media(RouteRegistry(), ["GET"], {"GET": ("text/html",)}, enveloped=False)


def test_a_raw_json_body_may_declare_json() -> None:
    registry = RouteRegistry()
    _record_media(registry, ["GET"], {"GET": ("application/json",)}, enveloped=False)
    (meta,) = registry.routes()
    assert meta.success_media_types == {"GET": ("application/json",)}


# -- Record-time minimum bar --------------------------------------------------


def test_missing_summary_is_rejected_loudly() -> None:
    registry = RouteRegistry()
    with pytest.raises(ValueError, match="summary"):
        registry.record(
            path="/api/x",
            methods=["GET"],
            name=None,
            handler=_plain,
            summary="",
            tags=["t"],
            authed=True,
            request_model=None,
            response_model=None,
        )


def test_missing_tags_is_rejected_loudly() -> None:
    registry = RouteRegistry()
    with pytest.raises(ValueError, match="tag"):
        registry.record(
            path="/api/x",
            methods=["GET"],
            name=None,
            handler=_plain,
            summary="X",
            tags=[],
            authed=True,
            request_model=None,
            response_model=None,
        )


# -- Surface version ----------------------------------------------------------


def test_version_moves_on_every_record() -> None:
    # A consumer that derives a table from the registry (the rate limiter's public-door
    # coverage) memoizes against this number: it must move on EVERY record, including a
    # reload re-recording the same metadata, or that consumer keeps matching requests
    # against the previous deployment's doors.
    registry = RouteRegistry()
    start = registry.version
    for _ in range(2):
        registry.record(
            path="/api/thing",
            methods=["GET"],
            name=None,
            handler=_plain,
            summary="Thing",
            tags=["t"],
            authed=True,
            action="read",
            request_model=None,
            response_model=None,
            no_body_reason="test fixture: response body not under test",
        )
    assert registry.version == start + 2


def test_version_does_not_move_on_a_refused_record() -> None:
    # The bump rides the recorded route, not the attempt: a refused registration leaves
    # the surface unchanged and must not invalidate a consumer's table.
    registry = RouteRegistry()
    start = registry.version
    with pytest.raises(ValueError, match="summary"):
        registry.record(
            path="/api/x",
            methods=["GET"],
            name=None,
            handler=_plain,
            summary="",
            tags=["t"],
            authed=True,
            request_model=None,
            response_model=None,
        )
    assert registry.version == start


def test_load_api_routes_returns_only_api_paths() -> None:
    routes = load_api_routes()
    assert routes
    assert all(meta.path.startswith("/api/") for meta in routes)
    # /health and /ready are registered but are not part of the /api surface.
    assert not any(meta.path in ("/health", "/ready") for meta in routes)
