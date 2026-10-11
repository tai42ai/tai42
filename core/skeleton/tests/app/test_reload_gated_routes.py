"""``custom_route`` enforces a declared ``reload_gated``: the served handler and the returned one are both gated."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from tai42_contract.app import DeclaredRouteMetadata

from tai42_skeleton.app import http as app_http
from tai42_skeleton.app.reload_gate import REJECT_MESSAGE
from tai42_skeleton.app.route_registry import RouteRegistry


class _RecordingFastMCP:
    """Captures what the surface hands the serving route table."""

    def __init__(self) -> None:
        self.served: list = []

    def custom_route(self, path, methods, name, include_in_schema):
        def register(fn):
            self.served.append(fn)
            return fn

        return register


async def _synthetic(request: Request) -> Response:
    """A synthetic door."""
    return PlainTextResponse("ran")


def _register(monkeypatch: pytest.MonkeyPatch, *, reload_gated: bool):
    registry = RouteRegistry()
    monkeypatch.setattr(app_http, "route_registry", registry)
    fast = _RecordingFastMCP()
    surface = app_http.HttpSurface(SimpleNamespace(_fast_mcp=fast))  # type: ignore[arg-type]
    returned = surface.custom_route(
        "/api/synthetic/run",
        methods=["POST"],
        summary="Synthetic run",
        tags=["synthetic"],
        response_model=None,
        no_body_reason="synthetic",
        action="write",
        declared=DeclaredRouteMetadata(
            reload_gated=reload_gated, reads_body=False, error_statuses=(), success_status=200
        ),
    )(_synthetic)
    return registry, fast, returned


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/synthetic/run", "headers": []})


async def test_a_reload_gated_route_is_refused_while_reloading(monkeypatch: pytest.MonkeyPatch) -> None:
    registry, fast, returned = _register(monkeypatch, reload_gated=True)
    monkeypatch.setattr(app_http, "reload_gate", SimpleNamespace(locked=True, reject_response=_reject))
    (served,) = fast.served
    # The served handler AND the module-level name are the wrapper.
    assert served is returned
    for handler in (served, returned):
        response = await handler(_request())
        assert response.status_code == 503
        assert json.loads(bytes(response.body)) == {"error": REJECT_MESSAGE, "reloading": True}
    # The registry describes the undecorated handler.
    (meta,) = registry.routes()
    assert meta.description == "A synthetic door."


async def test_a_reload_gated_route_runs_when_the_gate_is_free(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry, _fast, returned = _register(monkeypatch, reload_gated=True)
    monkeypatch.setattr(app_http, "reload_gate", SimpleNamespace(locked=False, reject_response=_reject))
    response = await returned(_request())
    assert bytes(response.body) == b"ran"


async def test_an_ungated_route_is_served_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry, fast, returned = _register(monkeypatch, reload_gated=False)
    monkeypatch.setattr(app_http, "reload_gate", SimpleNamespace(locked=True, reject_response=_reject))
    assert fast.served == [_synthetic]
    assert returned is _synthetic
    assert bytes((await returned(_request())).body) == b"ran"


def _reject() -> Response:
    from tai42_skeleton.app.reload_gate import reload_gate

    return reload_gate.reject_response()
