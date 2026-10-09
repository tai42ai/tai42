"""A served boot refuses an unauthenticated caller on every MCP transport and under the sub-MCP mount.

The assembled app (``create_app``) runs under uvicorn on a loopback port with the real
access-control stack and no Studio routes (``default_routers: "none"``). The serving app
records the MCP transports and the sub-MCP mount after the startup audits built the
access-control route index, so these paths are recognised only if the index follows those
records. A request that carries no credential must be refused 401 by the outer guard; an
administrator's session on the same path is admitted.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app.instance import app

from ._served_app import (
    ADMIN_KEY,
    INITIALIZE_BODY,
    MCP_POST_HEADERS,
    TOOL_NAME,
    ResponseLog,
    bearer,
    install_access_control,
    manifest,
    raw_request,
    served,
    sse_session_tools,
    streamable_session_tools,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning"),
    # The SDK answers a streamable-HTTP POST, and opens a session's GET stream, with an SSE
    # response whose reader stream the SSE response iterates to its end and never closes
    # (``sse_starlette`` ``EventSourceResponse._stream_response``); anyio reports that reader
    # as unclosed when it is freed. A third-party resource report, matched narrowly.
    pytest.mark.filterwarnings("ignore:Unclosed <MemoryObjectReceiveStream:ResourceWarning"),
]

# The outer guard's refusal of a request that carries no credential.
AUTHENTICATION_REQUIRED = b'{"error":"Authentication required"}'


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


SSE_HEADERS = {"Accept": "text/event-stream"}
SUB_MCP_SLUG = "app-svc"


@pytest.mark.parametrize("stateless_http", [False, True], ids=["stateful", "stateless"])
async def test_an_unauthenticated_post_to_the_streamable_endpoint_is_refused_401(
    stateless_http: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with served(monkeypatch, manifest("none"), stateless_http=stateless_http) as server:
        status, _headers, payload = await raw_request(
            server.origin, "POST", "/mcp", body=INITIALIZE_BODY, headers=MCP_POST_HEADERS
        )
    assert (status, payload) == (401, AUTHENTICATION_REQUIRED)


@pytest.mark.parametrize("stateless_http", [False, True], ids=["stateful", "stateless"])
async def test_an_administrator_session_on_the_streamable_endpoint_is_admitted(
    stateless_http: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with served(monkeypatch, manifest("none"), stateless_http=stateless_http) as server:
        tools = await streamable_session_tools(f"{server.base_url}/mcp", bearer(ADMIN_KEY), ResponseLog())
    assert TOOL_NAME in tools


@pytest.mark.parametrize(
    ("method", "target", "body", "headers", "expected"),
    [
        ("GET", "/sse", b"", SSE_HEADERS, AUTHENTICATION_REQUIRED),
        ("HEAD", "/sse", b"", SSE_HEADERS, b""),
        ("POST", "/messages/?session_id=0", b"{}", {"Content-Type": "application/json"}, AUTHENTICATION_REQUIRED),
    ],
    ids=["GET-stream", "HEAD-stream", "POST-message"],
)
async def test_an_unauthenticated_request_on_the_sse_boot_is_refused_401(
    method: str, target: str, body: bytes, headers: dict[str, str], expected: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with served(monkeypatch, manifest("none"), transport="sse") as server:
        status, _headers, payload = await raw_request(server.origin, method, target, body=body, headers=headers)
    assert (status, payload) == (401, expected)


async def test_an_administrator_session_on_the_sse_boot_is_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(monkeypatch, manifest("none"), transport="sse") as server:
        tools = await sse_session_tools(f"{server.base_url}/sse", bearer(ADMIN_KEY), ResponseLog())
    assert TOOL_NAME in tools


@pytest.mark.parametrize(("method", "body"), [("GET", AUTHENTICATION_REQUIRED), ("HEAD", b"")], ids=["GET", "HEAD"])
async def test_an_unauthenticated_stream_under_the_sub_mcp_mount_is_refused_401(
    method: str, body: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with served(monkeypatch, manifest("none")) as server:
        await app.sub_app.mcp_sub_app_router.register_sub_mcp_app(SUB_MCP_SLUG, [TOOL_NAME], transport="sse")
        status, _headers, payload = await raw_request(
            server.origin, method, f"/app/{SUB_MCP_SLUG}/sse", headers=SSE_HEADERS
        )
    assert (status, payload) == (401, body)


async def test_an_unauthenticated_post_to_a_streamable_sub_mcp_app_is_refused_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with served(monkeypatch, manifest("none")) as server:
        await app.sub_app.mcp_sub_app_router.register_sub_mcp_app(SUB_MCP_SLUG, [TOOL_NAME], transport="http")
        status, _headers, payload = await raw_request(
            server.origin, "POST", f"/app/{SUB_MCP_SLUG}", body=INITIALIZE_BODY, headers=MCP_POST_HEADERS
        )
    assert (status, payload) == (401, AUTHENTICATION_REQUIRED)


async def test_an_administrator_session_on_a_streamable_sub_mcp_app_is_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with served(monkeypatch, manifest("none")) as server:
        await app.sub_app.mcp_sub_app_router.register_sub_mcp_app(SUB_MCP_SLUG, [TOOL_NAME], transport="http")
        tools = await streamable_session_tools(
            f"{server.base_url}/app/{SUB_MCP_SLUG}", bearer(ADMIN_KEY), ResponseLog()
        )
    assert tools == [TOOL_NAME]


async def test_an_unauthenticated_post_to_the_embedded_streamable_endpoint_is_refused_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with served(monkeypatch, manifest("none"), prefix="/host") as server:
        status, _headers, payload = await raw_request(
            server.origin, "POST", "/host/mcp", body=INITIALIZE_BODY, headers=MCP_POST_HEADERS
        )
    assert (status, payload) == (401, AUTHENTICATION_REQUIRED)


async def test_an_administrator_session_on_the_embedded_streamable_endpoint_is_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with served(monkeypatch, manifest("none"), prefix="/host") as server:
        tools = await streamable_session_tools(f"{server.base_url}/mcp", bearer(ADMIN_KEY), ResponseLog())
    assert TOOL_NAME in tools
