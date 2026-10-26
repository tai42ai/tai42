"""A streamable-HTTP sub-MCP app serves in the stateless mode the worker booted with.

The assembled app (``create_app``) runs under uvicorn on a loopback port with the real
access-control stack. A stateless boot is the mode a multi-worker deployment runs, where
consecutive requests of one client land on different workers, so a sub-MCP app registered
on it must be stateless too: an ``initialize`` mints no session id, and the client's
follow-up ``notifications/initialized`` — which then carries no session id — is accepted.
A stateful boot's sub-MCP app keeps minting session ids.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest

from tai42_skeleton.app import applied_env
from tai42_skeleton.app.instance import app

from ._served_app import (
    ADMIN_KEY,
    INITIALIZE_BODY,
    MCP_POST_HEADERS,
    TOOL_NAME,
    bearer,
    install_access_control,
    manifest,
    raw_request,
    served,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning"),
    # The SDK answers a streamable-HTTP POST with an SSE response whose reader stream the
    # response iterates to its end and never closes; anyio reports that reader as unclosed
    # when it is freed. A third-party resource report, matched narrowly.
    pytest.mark.filterwarnings("ignore:Unclosed <MemoryObjectReceiveStream:ResourceWarning"),
]

SESSION_HEADER = "mcp-session-id"
SUB_MCP_SLUG = "app-svc"

INITIALIZED_NOTIFICATION_BODY = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()


@pytest.fixture(autouse=True)
def _gate(monkeypatch: pytest.MonkeyPatch) -> None:
    install_access_control(monkeypatch)


@pytest.fixture(autouse=True)
def _process_state_restored(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restore what a boot leaves on the process: the env applied and the registry's records."""
    from tai42_skeleton.app.route_registry import route_registry

    snapshot = dict(os.environ)
    monkeypatch.setattr(route_registry, "_routes", dict(route_registry._routes))
    monkeypatch.setattr(route_registry, "_control_plane_mount", route_registry._control_plane_mount)
    monkeypatch.setattr(app.config.config_manager, "read_env", dict)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    applied_env._loaded_env_keys = set()


async def _post(origin: str, body: bytes) -> tuple[int, dict[str, str], bytes]:
    return await raw_request(
        origin, "POST", f"/app/{SUB_MCP_SLUG}", body=body, headers={**MCP_POST_HEADERS, **bearer(ADMIN_KEY)}
    )


async def test_a_sub_mcp_app_on_a_stateless_boot_is_stateless(monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(monkeypatch, manifest("none"), stateless_http=True) as server:
        await app.sub_app.mcp_sub_app_router.register_sub_mcp_app(SUB_MCP_SLUG, [TOOL_NAME], transport="http")
        init_status, init_headers, _ = await _post(server.origin, INITIALIZE_BODY)
        notify_status, _, notify_payload = await _post(server.origin, INITIALIZED_NOTIFICATION_BODY)
    assert init_status == 200
    assert SESSION_HEADER not in init_headers, f"the stateless boot's sub-MCP app minted a session: {init_headers}"
    assert notify_status == 202, (
        f"the stateless boot's sub-MCP app refused a session-less notification: {notify_status} {notify_payload!r}"
    )


async def test_a_sub_mcp_app_on_a_stateful_boot_is_stateful(monkeypatch: pytest.MonkeyPatch) -> None:
    async with served(monkeypatch, manifest("none")) as server:
        await app.sub_app.mcp_sub_app_router.register_sub_mcp_app(SUB_MCP_SLUG, [TOOL_NAME], transport="http")
        status, headers, _ = await _post(server.origin, INITIALIZE_BODY)
    assert status == 200
    assert headers.get(SESSION_HEADER), f"the stateful boot's sub-MCP app minted no session: {headers}"
