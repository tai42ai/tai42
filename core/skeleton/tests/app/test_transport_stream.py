"""The transport-stream middleware exempts exactly the transport's server-to-client stream from the drain."""

from __future__ import annotations

from typing import Any

import pytest

from tai42_skeleton.app import transport_stream
from tai42_skeleton.app.transport_stream import TransportStreamDrainExemptMiddleware


@pytest.fixture
def exemptions(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """One entry per exemption the middleware asks for."""
    calls: list[None] = []
    monkeypatch.setattr(transport_stream, "mark_current_request_drain_exempt", lambda: calls.append(None))
    return calls


async def _pass_through(stream_path: str, scope: dict[str, Any]) -> None:
    seen: list[dict[str, Any]] = []

    async def _inner(inner_scope, receive, send) -> None:
        seen.append(inner_scope)

    await TransportStreamDrainExemptMiddleware(_inner, stream_path=stream_path)(scope, None, None)  # type: ignore[arg-type]
    assert seen == [scope], "the middleware did not pass the request through"


@pytest.mark.parametrize(
    ("stream_path", "scope"),
    [
        ("/mcp", {"type": "http", "method": "GET", "path": "/mcp"}),
        ("/sse", {"type": "http", "method": "GET", "path": "/host/sse", "root_path": "/host"}),
        ("/", {"type": "http", "method": "GET", "path": "/", "root_path": "/app/slug"}),
        ("/app/slug/sse", {"type": "http", "method": "GET", "path": "/app/slug/sse", "root_path": ""}),
    ],
    ids=["main", "under-a-host-mount", "streamable-sub-app", "sse-sub-app"],
)
async def test_a_get_on_the_stream_path_is_exempted(
    stream_path: str, scope: dict[str, Any], exemptions: list[None]
) -> None:
    await _pass_through(stream_path, scope)
    assert len(exemptions) == 1


@pytest.mark.parametrize(
    "scope",
    [
        {"type": "http", "method": "POST", "path": "/mcp"},
        {"type": "http", "method": "DELETE", "path": "/mcp"},
        {"type": "http", "method": "GET", "path": "/mcp/other"},
        {"type": "http", "method": "GET", "path": "/api/health"},
        {"type": "lifespan"},
        {"type": "websocket", "path": "/mcp"},
    ],
    ids=["post", "delete", "get-below", "get-elsewhere", "lifespan", "websocket"],
)
async def test_every_other_request_stays_counted(scope: dict[str, Any], exemptions: list[None]) -> None:
    await _pass_through("/mcp", scope)
    assert exemptions == []
