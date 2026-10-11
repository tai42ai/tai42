"""A config reload rebuilds the serving app on the transport the server was started with.

The assembled app runs under uvicorn on a loopback port, started on the SSE transport or
on stateless streamable HTTP; after a real config reload a fresh client still reaches it
the same way: an SSE session opens at ``/sse``, and a stateless endpoint mints no session.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.reload_gate import reload_gate

from ._served_app import (
    ADMIN_KEY,
    REQUEST_TIMEOUT_S,
    TOOL_NAME,
    ResponseLog,
    bearer,
    install_access_control,
    logging_client_factory,
    manifest,
    served,
    sse_session_tools,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning"),
    # The SDK answers a streamable-HTTP POST with an SSE response whose reader stream the
    # SSE response iterates to its end and never closes; anyio reports it when freed.
    pytest.mark.filterwarnings("ignore:Unclosed <MemoryObjectReceiveStream:ResourceWarning"),
]


@pytest.fixture(autouse=True)
def _gate(monkeypatch: pytest.MonkeyPatch) -> None:
    install_access_control(monkeypatch)


@pytest.fixture(autouse=True)
def _process_state_restored(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restore what a boot leaves on the process: the env it applied and the registry's records."""
    from tai42_skeleton.app.route_registry import route_registry

    snapshot = dict(os.environ)
    monkeypatch.setattr(route_registry, "_routes", dict(route_registry._routes))
    monkeypatch.setattr(route_registry, "_control_plane_mount", route_registry._control_plane_mount)
    monkeypatch.setattr(app.config.config_manager, "read_env", dict)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    epoch_mod._loaded_env_keys = set()


async def _reload() -> None:
    await reload_gate.run(lambda: app.admin.reload_config(), reimports=False)


async def test_an_sse_server_still_serves_sse_after_a_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(monkeypatch, manifest("none"), transport="sse") as server:
        await _reload()
        tools = await sse_session_tools(f"{server.base_url}/sse", bearer(ADMIN_KEY), ResponseLog())
    assert TOOL_NAME in tools


async def test_a_stateless_server_stays_stateless_after_a_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(monkeypatch, manifest("none"), stateless_http=True) as server:
        await _reload()
        client = logging_client_factory(ResponseLog())(
            headers=bearer(ADMIN_KEY), timeout=httpx.Timeout(REQUEST_TIMEOUT_S)
        )
        async with (
            asyncio.timeout(REQUEST_TIMEOUT_S),
            client,
            streamable_http_client(f"{server.base_url}/mcp", http_client=client) as (read, write, session_id),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            assert session_id() is None, "the reloaded endpoint minted a session: it serves stateful HTTP"
    assert TOOL_NAME in [tool.name for tool in listed.tools]
