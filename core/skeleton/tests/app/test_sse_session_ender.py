"""An SSE app records, when it is assembled, how its live sessions are ended; the layout it reads is pinned.

The legacy HTTP+SSE transport has no session-termination message and the pinned MCP SDK has
no call to end a session, so the ender reads the session table of the SDK transport behind
the message-path mount fastmcp's ``create_sse_app`` registers. Assembly raises, naming the
fact, when that layout is not the pinned one. Ending a live session over a real server
answers the client's pending call with ``Connection closed``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.http import create_sse_app
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.shared.exceptions import McpError
from sse_starlette.sse import AppStatus
from starlette.applications import Starlette
from starlette.datastructures import State
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from tai42_skeleton.app.sub_mcp_app import SubAppLifespan
from tai42_skeleton.app.transport_stream import bind_sse_session_ender, transport_session_ender

pytestmark = pytest.mark.filterwarnings("ignore:Unclosed <MemoryObjectReceiveStream:ResourceWarning")

BOUND_S = 10.0


def _server(started: asyncio.Event) -> FastMCP:
    mcp = FastMCP("ender-probe")

    @mcp.tool
    async def wait_forever() -> str:
        started.set()
        await asyncio.Event().wait()
        return "never"

    return mcp


@asynccontextmanager
async def _serving(app: Starlette) -> AsyncIterator[str]:
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, lifespan="on", log_level="warning", timeout_graceful_shutdown=1
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        await task


async def test_ending_the_sessions_of_an_assembled_sse_app_answers_a_pending_call_with_connection_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(AppStatus, "should_exit", False)
    started = asyncio.Event()
    app = create_sse_app(_server(started), message_path="/messages", sse_path="/sse")
    bind_sse_session_ender(app, "/messages")
    end_sessions = transport_session_ender(app)
    assert end_sessions is not None
    async with (
        asyncio.timeout(BOUND_S),
        _serving(app) as origin,
        sse_client(f"{origin}/sse") as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        pending = asyncio.create_task(session.call_tool("wait_forever", {}))
        await started.wait()
        await end_sessions()
        with pytest.raises(McpError, match="Connection closed"):
            await pending


async def test_the_ender_finds_the_transport_behind_fastmcps_auth_wrapper() -> None:
    verifier = StaticTokenVerifier(tokens={"t": {"client_id": "c", "scopes": []}})
    app = create_sse_app(_server(asyncio.Event()), message_path="/messages", sse_path="/sse", auth=verifier)
    bind_sse_session_ender(app, "/messages")
    assert transport_session_ender(app) is not None


def test_assembly_raises_when_the_message_mount_is_not_the_sdk_transport() -> None:
    async def _other(scope, receive, send) -> None:
        return None

    app = Starlette(routes=[Mount("/messages", app=_other)])
    with pytest.raises(RuntimeError, match="does not serve the MCP SDK's SseServerTransport"):
        bind_sse_session_ender(app, "/messages")


def test_assembly_raises_when_the_app_has_no_message_mount() -> None:
    app = create_sse_app(_server(asyncio.Event()), message_path="/messages", sse_path="/sse")
    with pytest.raises(RuntimeError, match="has no message mount at '/elsewhere'"):
        bind_sse_session_ender(app, "/elsewhere")


def test_an_app_with_no_recorded_ender_has_none() -> None:
    assert transport_session_ender(Starlette()) is None
    assert transport_session_ender(object()) is None


class _LifespanApp:
    """A lifespan-bearing app recording its lifespan's exit into ``log``."""

    def __init__(self, log: list[str]) -> None:
        self.state = State()
        self._log = log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("the lifespan app serves no request")  # pragma: no cover

    @asynccontextmanager
    async def lifespan(self, _app: object) -> AsyncIterator[None]:
        yield
        self._log.append("lifespan exited")


async def test_closing_the_lifespan_that_owns_an_sse_app_ends_its_sessions_first_then_exits_it() -> None:
    log: list[str] = []

    app = _LifespanApp(log)

    async def _end() -> None:
        log.append("sessions ended")

    app.state.tai42_end_sse_sessions = _end
    lifespan = SubAppLifespan(app)
    await lifespan.start()
    await lifespan.aclose()
    await lifespan.aclose()
    assert log == ["sessions ended", "lifespan exited"]


async def test_the_lifespan_exits_even_when_ending_the_sessions_fails() -> None:
    log: list[str] = []

    app = _LifespanApp(log)

    async def _end() -> None:
        raise RuntimeError("session end failed")

    app.state.tai42_end_sse_sessions = _end
    lifespan = SubAppLifespan(app)
    await lifespan.start()
    with pytest.raises(RuntimeError, match="session end failed"):
        await lifespan.aclose()
    assert log == ["lifespan exited"]
