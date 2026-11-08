"""``custom_route`` enforces a declared ``holds_writer_exit``: the route holds the exit from before its handler.

On a worker that decided to leave the route answers the leaving ``503`` before the handler runs
and before the request body is read; a route declaring no hold runs as written; the reload gate
answers before the hold is taken; a held route served outside a writer-exit scope raises.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from tai42_contract.app import DeclaredRouteMetadata

from tai42_skeleton.app import http as app_http
from tai42_skeleton.app import instance
from tai42_skeleton.app.reload_gate import REJECT_MESSAGE
from tai42_skeleton.app.route_registry import RouteRegistry
from tai42_skeleton.app.writer_exit import writer_exit_scope

from .._fakes.bus import FakeBus

_SUMMARY = "Synthetic change"
_LEAVING = "a fleet recycle op"


class _RecordingFastMCP:
    """Captures what the surface hands the serving route table."""

    def __init__(self) -> None:
        self.served: list = []

    def custom_route(self, path, methods, name, include_in_schema):
        def register(fn):
            self.served.append(fn)
            return fn

        return register


Handler = Callable[[Request], Awaitable[Response]]


def _door(bus: FakeBus, held_while_running: list[str | None]) -> Handler:
    """A synthetic door recording, on each run, the exit held while it ran."""

    async def synthetic(request: Request) -> Response:
        """A synthetic door."""
        held_while_running.append(bus.exit_deferred)
        return PlainTextResponse("ran")

    return synthetic


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch) -> FakeBus:
    """This process's bus member, the writer whose exit a held route holds."""
    fake = FakeBus(origin="serve-writer", remotes=[])
    monkeypatch.setattr(instance.app, "_bus", fake)
    return fake


def _register(
    monkeypatch: pytest.MonkeyPatch, handler: Handler, *, holds_writer_exit: bool, reload_gated: bool = False
) -> tuple[_RecordingFastMCP, Any]:
    monkeypatch.setattr(app_http, "route_registry", RouteRegistry())
    fast = _RecordingFastMCP()
    surface = app_http.HttpSurface(SimpleNamespace(_fast_mcp=fast))  # type: ignore[arg-type]
    returned = surface.custom_route(
        "/api/synthetic/change",
        methods=["POST"],
        summary=_SUMMARY,
        tags=["synthetic"],
        response_model=None,
        no_body_reason="synthetic",
        action="write",
        declared=DeclaredRouteMetadata(
            reload_gated=reload_gated,
            reads_body=False,
            holds_writer_exit=holds_writer_exit,
            error_statuses=(),
            success_status=200,
        ),
    )(handler)
    return fast, returned


def _request(received: list[str]) -> Request:
    async def receive() -> dict[str, Any]:
        received.append("body")
        return {"type": "http.request", "body": b"{}", "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/api/synthetic/change", "headers": []}, receive)


def _gate(*, locked: bool) -> SimpleNamespace:
    def reject() -> Response:
        from tai42_skeleton.app.reload_gate import reload_gate

        return reload_gate.reject_response()

    return SimpleNamespace(locked=locked, reject_response=reject)


async def test_a_route_holding_the_writer_exit_on_a_leaving_worker_answers_the_leaving_503_before_its_handler(
    monkeypatch: pytest.MonkeyPatch, bus: FakeBus
) -> None:
    held_while_running: list[str | None] = []
    handler = _door(bus, held_while_running)
    fast, returned = _register(monkeypatch, handler, holds_writer_exit=True)
    assert bus.decide_departure(_LEAVING) is None
    (served,) = fast.served
    received: list[str] = []

    # The served handler AND the module-level name are the wrapper.
    assert served is returned
    for route in (served, returned):
        with writer_exit_scope() as exits:
            response = await route(_request(received))
            assert not exits.holds_exit
        assert response.status_code == 503
        assert json.loads(bytes(response.body)) == {
            "error": f"leaving — this worker is leaving ({_LEAVING}) and begins no change; retry shortly",
            "leaving": _LEAVING,
        }

    assert held_while_running == []
    assert received == []
    assert bus.exit_deferred is None


async def test_a_route_holding_the_writer_exit_holds_it_from_before_its_handler(
    monkeypatch: pytest.MonkeyPatch, bus: FakeBus
) -> None:
    held_while_running: list[str | None] = []
    handler = _door(bus, held_while_running)
    _fast, returned = _register(monkeypatch, handler, holds_writer_exit=True)

    with writer_exit_scope() as exits:
        response = await returned(_request([]))
        assert exits.holds_exit
        await exits.settle()

    assert bytes(response.body) == b"ran"
    assert held_while_running == [_SUMMARY]
    assert bus.exit_deferred is None


async def test_a_route_declaring_no_hold_runs_as_written_on_a_leaving_worker(
    monkeypatch: pytest.MonkeyPatch, bus: FakeBus
) -> None:
    held_while_running: list[str | None] = []
    handler = _door(bus, held_while_running)
    fast, returned = _register(monkeypatch, handler, holds_writer_exit=False)
    assert bus.decide_departure(_LEAVING) is None

    assert fast.served == [handler]
    assert returned is handler
    with writer_exit_scope() as exits:
        response = await returned(_request([]))
        assert not exits.holds_exit

    assert bytes(response.body) == b"ran"
    assert held_while_running == [None]


async def test_the_reload_gate_answers_before_the_hold(monkeypatch: pytest.MonkeyPatch, bus: FakeBus) -> None:
    held_while_running: list[str | None] = []
    handler = _door(bus, held_while_running)
    _fast, returned = _register(monkeypatch, handler, holds_writer_exit=True, reload_gated=True)
    monkeypatch.setattr(app_http, "reload_gate", _gate(locked=True))

    with writer_exit_scope() as exits:
        response = await returned(_request([]))
        assert not exits.holds_exit

    assert response.status_code == 503
    assert json.loads(bytes(response.body)) == {"error": REJECT_MESSAGE, "reloading": True}
    assert held_while_running == []
    assert bus.exit_deferred is None


async def test_a_held_route_served_outside_a_writer_exit_scope_raises(
    monkeypatch: pytest.MonkeyPatch, bus: FakeBus
) -> None:
    held_while_running: list[str | None] = []
    handler = _door(bus, held_while_running)
    _fast, returned = _register(monkeypatch, handler, holds_writer_exit=True)

    with pytest.raises(RuntimeError) as raised:
        await returned(_request([]))

    assert str(raised.value) == (
        f"writer exit: {_SUMMARY} is served outside a writer-exit scope — the route surface holds the "
        "exit only under WriterExitMiddleware"
    )
    assert held_while_running == []
    assert bus.exit_deferred is None
