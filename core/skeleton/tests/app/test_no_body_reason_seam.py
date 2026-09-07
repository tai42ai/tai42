"""The ``no_body_reason`` / ``enveloped`` declaration seam and its import-time guard.

A route declares a typed body (``response_model=<M>``) OR a reasoned no-body
(``response_model=None`` + a non-blank ``no_body_reason``) — never a bare ``None``
and never both. A typed body is wrapped in the ``{"data": ...}`` envelope by default;
``enveloped=False`` declares a RAW top-level body and REQUIRES a ``response_model`` (a
bare ``None`` under it is refused). The enforcement lives at the one chokepoint
:meth:`RouteRegistry.record`, so it fires through EVERY registration door:
``record`` directly, the native ``HttpSurface.custom_route``, and the
``@operation`` adapter (which funnels through ``custom_route`` too).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tai42_skeleton.app.http import HttpSurface
from tai42_skeleton.app.route_registry import RouteRegistry
from tai42_skeleton.app.route_registry import _SpecApp as SpecApp
from tai42_skeleton.operations import OperationRegistry, operation, register_operation_route
from tai42_skeleton.operations.decorator import operation_metadata_of


class _Model(BaseModel):
    value: int


async def _handler(request: Request) -> Response:
    """A plain handler."""
    return JSONResponse({"data": {}})


class _FakeFastMCP:
    def custom_route(self, path: str, methods: list[str], name: str | None, include_in_schema: bool):
        return lambda fn: fn


class _FakeApp:
    def __init__(self) -> None:
        self._fast_mcp = _FakeFastMCP()


def _record(registry: RouteRegistry, **overrides: object) -> None:
    kwargs: dict[str, object] = {
        "path": "/api/thing",
        "methods": ["POST"],
        "name": "thing",
        "handler": _handler,
        "summary": "Thing",
        "tags": ["t"],
        "authed": True,
        "action": "write",
        "request_model": None,
        "response_model": None,
    }
    kwargs.update(overrides)
    registry.record(**kwargs)  # type: ignore[arg-type]


# -- Door 1: RouteRegistry.record (the chokepoint) ----------------------------


def test_record_bare_none_raises() -> None:
    with pytest.raises(ValueError, match="declares no response_model and no no_body_reason"):
        _record(RouteRegistry())


def test_record_blank_reason_raises() -> None:
    with pytest.raises(ValueError, match="declares no response_model and no no_body_reason"):
        _record(RouteRegistry(), no_body_reason="   ")


def test_record_both_supplied_raises() -> None:
    with pytest.raises(ValueError, match="passes both a response_model and a no_body_reason"):
        _record(RouteRegistry(), response_model=_Model, no_body_reason="oops")


def test_record_reasoned_no_body_passes() -> None:
    registry = RouteRegistry()
    _record(registry, no_body_reason="serves a raw streaming body")
    (meta,) = registry.routes()
    assert meta.response_model is None
    assert meta.no_body_reason == "serves a raw streaming body"


def test_record_typed_body_passes() -> None:
    registry = RouteRegistry()
    _record(registry, response_model=_Model)
    (meta,) = registry.routes()
    assert meta.response_model is _Model
    assert meta.no_body_reason is None
    assert meta.enveloped is True


def test_record_enveloped_false_without_model_raises() -> None:
    with pytest.raises(ValueError, match="declares enveloped=False but no response_model"):
        _record(RouteRegistry(), enveloped=False)


def test_record_unwrapped_typed_body_passes() -> None:
    registry = RouteRegistry()
    _record(registry, response_model=_Model, enveloped=False)
    (meta,) = registry.routes()
    assert meta.response_model is _Model
    assert meta.no_body_reason is None
    assert meta.enveloped is False


# -- Door 2: HttpSurface.custom_route -----------------------------------------


def _surface(monkeypatch: pytest.MonkeyPatch) -> HttpSurface:
    registry = RouteRegistry()
    monkeypatch.setattr("tai42_skeleton.app.http.route_registry", registry)
    surface = HttpSurface(_FakeApp())  # type: ignore[arg-type]
    surface._registry = registry  # type: ignore[attr-defined]
    return surface


def test_custom_route_bare_none_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    surface = _surface(monkeypatch)
    with pytest.raises(ValueError, match="declares no response_model and no no_body_reason"):
        surface.custom_route("/api/thing", ["POST"], summary="s", tags=["t"], response_model=None, action="write")(
            _handler
        )


def test_custom_route_both_supplied_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    surface = _surface(monkeypatch)
    with pytest.raises(ValueError, match="passes both a response_model and a no_body_reason"):
        surface.custom_route(
            "/api/thing",
            ["POST"],
            summary="s",
            tags=["t"],
            response_model=_Model,
            no_body_reason="oops",
            action="write",
        )(_handler)


def test_custom_route_reasoned_no_body_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    surface = _surface(monkeypatch)
    surface.custom_route(
        "/api/thing",
        ["POST"],
        summary="s",
        tags=["t"],
        response_model=None,
        no_body_reason="serves a redirect",
        action="write",
    )(_handler)
    meta = surface._registry.match("/api/thing", "POST")  # type: ignore[attr-defined]
    assert meta is not None
    assert meta.no_body_reason == "serves a redirect"


def test_custom_route_enveloped_false_without_model_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    surface = _surface(monkeypatch)
    with pytest.raises(ValueError, match="declares enveloped=False but no response_model"):
        surface.custom_route(
            "/api/thing", ["POST"], summary="s", tags=["t"], response_model=None, enveloped=False, action="write"
        )(_handler)


def test_custom_route_unwrapped_typed_body_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    surface = _surface(monkeypatch)
    surface.custom_route(
        "/api/thing", ["POST"], summary="s", tags=["t"], response_model=_Model, enveloped=False, action="write"
    )(_handler)
    meta = surface._registry.match("/api/thing", "POST")  # type: ignore[attr-defined]
    assert meta is not None
    assert meta.response_model is _Model
    assert meta.enveloped is False


# -- Door 3: the @operation adapter -------------------------------------------


def _register_op(monkeypatch: pytest.MonkeyPatch, **op_kwargs: object):
    registry = RouteRegistry()
    monkeypatch.setattr("tai42_skeleton.app.http.route_registry", registry)
    reg = OperationRegistry()

    @operation(summary="Op", tags=["t"], registry=reg, **op_kwargs)  # type: ignore[arg-type]
    async def op(value: int) -> dict:
        """An op."""
        return {"value": value}

    register_operation_route(SpecApp(), operation_metadata_of(op), path="/api/op", method="POST", action="write")
    return registry


def test_operation_adapter_bare_none_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="declares no response_model and no no_body_reason"):
        _register_op(monkeypatch)


def test_operation_adapter_both_supplied_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="passes both a response_model and a no_body_reason"):
        _register_op(monkeypatch, response_model=_Model, no_body_reason="oops")


def test_operation_adapter_reasoned_no_body_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _register_op(monkeypatch, no_body_reason="serves an SSE stream")
    meta = registry.match("/api/op", "POST")
    assert meta is not None
    assert meta.no_body_reason == "serves an SSE stream"


def test_operation_adapter_typed_body_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _register_op(monkeypatch, response_model=_Model)
    meta = registry.match("/api/op", "POST")
    assert meta is not None
    assert meta.response_model is _Model
    assert meta.no_body_reason is None
    assert meta.enveloped is True


def test_operation_adapter_enveloped_false_without_model_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="declares enveloped=False but no response_model"):
        _register_op(monkeypatch, enveloped=False)


def test_operation_adapter_unwrapped_typed_body_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _register_op(monkeypatch, response_model=_Model, enveloped=False)
    meta = registry.match("/api/op", "POST")
    assert meta is not None
    assert meta.response_model is _Model
    assert meta.enveloped is False
