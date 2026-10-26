"""On a served MCP transport, a tool body resolves the admission of the POST carrying its call.

The assembled app runs under uvicorn on a loopback port and the pinned SDK's own client
drives a stateful streamable-HTTP session: its ``initialize`` POST creates the session,
whose server task every later message is handled under, so the admission ContextVar a tool
body inherits names that long-finished ``initialize`` request. ``current_request_admission``
must name the ``tools/call`` POST instead — the request a reload driven by that call keeps
its transport open for — on the main ``/mcp`` endpoint and under a sub-MCP mount.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tai42_skeleton.app import applied_env
from tai42_skeleton.app.instance import app

from ._served_app import (
    ADMIN_KEY,
    REQUEST_TIMEOUT_S,
    ResponseLog,
    bearer,
    install_access_control,
    logging_client_factory,
    served,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning"),
    # The SDK answers a streamable-HTTP POST with an SSE response whose reader stream the
    # SSE response iterates to its end and never closes; anyio reports it when freed.
    pytest.mark.filterwarnings("ignore:Unclosed <MemoryObjectReceiveStream:ResourceWarning"),
]

PROBE_MANIFEST = {
    "default_routers": "none",
    "tools": [{"title": "probe", "module": "tests.app._fixtures.admission_probe", "include": ["admission_probe"]}],
}
SUB_MCP_SLUG = "app-probe"


def _observed() -> list:
    """The probe's observations, from the module the served manifest imported (a boot re-imports it)."""
    return sys.modules["tests.app._fixtures.admission_probe"].OBSERVED


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
    applied_env._loaded_env_keys = set()


async def _call_the_probe_twice(url: str) -> None:
    client = logging_client_factory(ResponseLog())(headers=bearer(ADMIN_KEY), timeout=httpx.Timeout(REQUEST_TIMEOUT_S))
    async with (
        asyncio.timeout(REQUEST_TIMEOUT_S),
        client,
        streamable_http_client(url, http_client=client) as (read, write, _session_id),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        for _ in range(2):
            result = await session.call_tool("admission_probe", {})
            assert not result.isError, result


def _assert_each_call_resolved_its_own_post() -> None:
    first, second = _observed()
    for seen in (first, second):
        # The resolved record is a live request: the POST carrying this very call.
        assert seen.resolved is not None
        assert seen.resolved_finished is False
        assert seen.resolved.finished.is_set(), "the call's POST has ended once its reply was read"
        # The context the session's server task copied names the request that created the
        # session, which ended before any call.
        assert seen.context_record is not None
        assert seen.context_record_finished is True
        assert seen.context_record is not seen.resolved
    assert first.resolved is not second.resolved, "two calls resolved one request"
    assert first.context_record is second.context_record


async def test_a_tool_call_on_the_streamable_endpoint_resolves_its_own_post(monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(monkeypatch, PROBE_MANIFEST) as server:
        await _call_the_probe_twice(f"{server.base_url}/mcp")
    _assert_each_call_resolved_its_own_post()


async def test_a_tool_call_under_a_sub_mcp_mount_resolves_its_own_post(monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(monkeypatch, PROBE_MANIFEST) as server:
        await app.sub_app.mcp_sub_app_router.register_sub_mcp_app(SUB_MCP_SLUG, ["admission_probe"], transport="http")
        await _call_the_probe_twice(f"{server.base_url}/app/{SUB_MCP_SLUG}")
    _assert_each_call_resolved_its_own_post()
