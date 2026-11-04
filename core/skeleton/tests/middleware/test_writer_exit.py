"""The HTTP edge of the writer's own exit: a request's change that owes it is settled after the reply.

:class:`WriterExitMiddleware` serves every request under a writer-exit scope. A change the route
makes whose roll armed the writer's restart owes that exit into the scope; once the reply's last
body chunk is sent the exit runs — for a plain route, a streamed reply, and an error reply an
exception handler renders alike. A reply that is never sent (an unhandled error the outer
``ServerErrorMiddleware`` answers) abandons the exit: its hold is released, ERROR-logged. A failed
exit is logged with its traceback and releases its hold.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.types import Message

from tai42_skeleton.app.bus import FleetResult, WorkerBus
from tai42_skeleton.app.error_envelope import install_error_handlers
from tai42_skeleton.app.writer_exit import WRITER_EXIT_REASON, open_writer_exit_scope
from tai42_skeleton.middleware.writer_exit import WriterExitMiddleware
from tai42_skeleton.operations._broadcast import FleetBroadcastError

from .._fakes.bus import FakeBus

_CHANGE = "the env change"


def _owe(bus: FakeBus) -> None:
    """Stand in for an armed roll: hold the writer's exit and owe it into the open scope."""
    scope = open_writer_exit_scope(_CHANGE)
    scope.hold_exit(cast("WorkerBus", bus), _CHANGE)
    scope.owe(cast("WorkerBus", bus))


def _app(bus: FakeBus) -> Starlette:
    async def changes(request: Request) -> JSONResponse:
        _owe(bus)
        return JSONResponse({"exit_requested_before_reply": list(bus.exit_requests)})

    async def streams(request: Request) -> StreamingResponse:
        _owe(bus)

        async def chunks() -> AsyncIterator[bytes]:
            yield b"first "
            yield f"{len(bus.exit_requests)}".encode()

        return StreamingResponse(chunks())

    async def fails_after_landing(request: Request) -> JSONResponse:
        _owe(bus)
        cause = ConnectionError("internal-host:6379")
        error = FleetBroadcastError("reload_config", FleetResult(op="reload_config", reachable=False), cause)
        error.__cause__ = cause
        raise error

    async def crashes(request: Request) -> JSONResponse:
        _owe(bus)
        raise ValueError("a code fault")

    async def reads_only(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/changes", changes, methods=["POST"]),
            Route("/streams", streams, methods=["POST"]),
            Route("/fails-after-landing", fails_after_landing, methods=["POST"]),
            Route("/crashes", crashes, methods=["POST"]),
            Route("/reads-only", reads_only),
        ],
        middleware=[Middleware(WriterExitMiddleware)],
    )
    install_error_handlers(app)
    return app


def _client(app: Starlette) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_a_route_change_that_owes_the_exit_runs_it_after_its_reply() -> None:
    bus = FakeBus(origin="serve-writer", remotes=[])

    async with _client(_app(bus)) as client:
        resp = await client.post("/changes")

    assert resp.status_code == 200
    assert resp.json() == {"exit_requested_before_reply": []}
    assert bus.exit_requests == [WRITER_EXIT_REASON]
    assert bus.recycling_marks == 1
    assert bus.exit_deferred is None


async def test_a_streamed_reply_runs_the_exit_only_after_its_last_chunk() -> None:
    bus = FakeBus(origin="serve-writer", remotes=[])

    async with _client(_app(bus)) as client:
        resp = await client.post("/streams")

    # The last chunk was rendered with no exit asked yet.
    assert resp.text == "first 0"
    assert bus.exit_requests == [WRITER_EXIT_REASON]


async def test_an_error_reply_an_exception_handler_renders_runs_the_exit_after_it() -> None:
    bus = FakeBus(origin="serve-writer", remotes=[])

    async with _client(_app(bus)) as client:
        resp = await client.post("/fails-after-landing")

    assert resp.status_code == 500
    assert bus.exit_requests == [WRITER_EXIT_REASON]
    assert bus.exit_deferred is None


async def test_an_unhandled_error_abandons_the_exit_releasing_its_hold_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = FakeBus(origin="serve-writer", remotes=[])

    with caplog.at_level("ERROR"):
        async with _client(_app(bus)) as client:
            resp = await client.post("/crashes")

    assert resp.status_code == 500
    assert bus.exit_requests == []
    assert bus.exit_deferred is None
    assert any(
        "owes this writer's own restart" in record.message and "awaits a respawn" in record.message
        for record in caplog.records
    )


async def test_an_exit_whose_recycling_mark_fails_still_leaves_is_logged_and_releases_its_hold(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The departure was decided before the mark, so the worker still signals it, then the failure is logged.
    bus = FakeBus(origin="serve-writer", remotes=[])
    monkeypatch.setattr(bus, "mark_recycling", AsyncMock(side_effect=ConnectionError("presence write lost")))

    with caplog.at_level("ERROR"):
        async with _client(_app(bus)) as client:
            resp = await client.post("/changes")

    assert resp.status_code == 200
    assert bus.exit_requests == [WRITER_EXIT_REASON]
    assert bus.exit_deferred is None
    failed = [record for record in caplog.records if "after its reply failed" in record.message]
    assert failed
    assert failed[0].exc_info is not None


async def test_a_request_owing_no_exit_runs_none() -> None:
    bus = FakeBus(origin="serve-writer", remotes=[])

    async with _client(_app(bus)) as client:
        resp = await client.get("/reads-only")

    assert resp.status_code == 200
    assert bus.exit_requests == []


async def test_a_non_http_scope_passes_through_with_no_scope_open() -> None:
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        with pytest.raises(RuntimeError, match="no writer-exit scope is open"):
            open_writer_exit_scope(_CHANGE)
        seen.append(scope["type"])

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        return None

    await WriterExitMiddleware(inner)({"type": "lifespan"}, receive, send)

    assert seen == ["lifespan"]
