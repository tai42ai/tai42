"""The published success media types are the routes' own declarations; the emitter only reads them."""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from tai42_skeleton.app.route_registry import RouteRegistry, load_all_routes
from tai42_skeleton.cli.openapi import _success_response

from .conftest import schemas_for

_SPEC_DECLARED = [
    ("/api/agents/{name}/runs", "post", ["text/event-stream"]),
    ("/api/agents/authored/{name}/runs", "post", ["text/event-stream"]),
    ("/api/interactions/callback/{ticket}", "get", ["text/html"]),
    ("/api/interactions/stream", "get", ["text/event-stream"]),
    ("/api/observability/runs/export", "get", ["text/csv", "application/octet-stream"]),
    ("/api/observability/runs/{trace_id}/trace/export", "get", ["application/octet-stream"]),
    ("/api/plugins/{name}/studio/{path}", "get", ["application/octet-stream"]),
    ("/api/storage/resources/{resource_id}/content", "get", ["application/octet-stream"]),
    ("/api/interactions/media/{media_id}", "get", ["application/octet-stream"]),
]


@pytest.mark.parametrize(("path", "method", "media"), _SPEC_DECLARED)
def test_a_declared_route_publishes_its_media_types(spec: dict, path: str, method: str, media: list[str]) -> None:
    content = spec["paths"][path][method]["responses"]["200"]["content"]
    assert list(content) == media
    for media_type in media:
        assert content[media_type] == {"schema": {"type": "string"}}


def test_the_media_door_declares_head_too() -> None:
    (meta,) = [m for m in load_all_routes() if m.path == "/api/interactions/media/{media_id}"]
    assert meta.success_media_types == {"GET": ("application/octet-stream",), "HEAD": ("application/octet-stream",)}


@pytest.mark.parametrize("path", ["/metrics", "/health"])
def test_the_plain_text_operational_doors_declare_text_plain(path: str) -> None:
    (meta,) = [m for m in load_all_routes() if m.path == path]
    assert meta.success_media_types == {"GET": ("text/plain",)}


async def _synthetic(request: Request) -> Response:
    """A synthetic plain-text door."""
    return PlainTextResponse("ok")


def test_a_synthetic_plugin_route_publishes_what_it_declares(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.app import http as app_http
    from tai42_skeleton.app.route_registry.scan import _SpecApp

    registry = RouteRegistry()
    monkeypatch.setattr(app_http, "route_registry", registry)
    _SpecApp().http.custom_route(
        "/api/synthetic/plain",
        methods=["GET"],
        summary="Synthetic plain-text door",
        tags=["synthetic"],
        response_model=None,
        no_body_reason="synthetic plain text",
        authed=False,
        success_media_types={"GET": ("text/plain",)},
    )(_synthetic)
    (meta,) = registry.routes()
    schemas, _ = schemas_for([meta])
    response = _success_response(meta, "GET", schemas)
    assert response["content"] == {"text/plain": {"schema": {"type": "string"}}}
    assert response["description"] == "synthetic plain text"
