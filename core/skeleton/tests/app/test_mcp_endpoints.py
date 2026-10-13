"""``mcp_endpoint_for`` — the MCP endpoint a canonical request path addresses, read from the live records.

Every case records the transport surfaces the way the serving app does (the same record
functions), on a copy of the process registry restored afterwards, so the map is driven by
what is mounted and never by a path literal.
"""

from __future__ import annotations

import pytest

from tai42_skeleton.app import mcp_endpoints
from tai42_skeleton.app.http import record_sub_mcp_mount
from tai42_skeleton.app.mcp_endpoints import mcp_endpoint_for
from tai42_skeleton.app.route_registry import route_registry
from tai42_skeleton.app.serving_core import record_sse_surface, record_streamable_http_surface
from tai42_skeleton.app.sub_mcp_app import ROOT_PREFIX


@pytest.fixture(autouse=True)
def _registry_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from no mounted record; the process registry is restored afterwards."""
    monkeypatch.setattr(
        route_registry, "_routes", {key: meta for key, meta in route_registry._routes.items() if not meta.mounted}
    )
    monkeypatch.setattr(route_registry, "_control_plane_mount", None)
    monkeypatch.setattr(route_registry, "_version", route_registry._version + 1)
    # The derived table is memoised on the registry version, which this fixture rewinds.
    monkeypatch.setattr(mcp_endpoints, "_table", None)
    monkeypatch.setattr(mcp_endpoints, "_sse_stream_by_message_prefix", {})


@pytest.mark.parametrize("configured", ["/mcp", "/mcp/"])
def test_the_streamable_endpoint_at_the_default_path_maps_to_itself(configured: str) -> None:
    record_streamable_http_surface(configured, stateless=False)
    assert mcp_endpoint_for("/mcp") == "/mcp"


@pytest.mark.parametrize("configured", ["/custom/endpoint", "/custom/endpoint/"])
def test_the_streamable_endpoint_at_a_configured_path_maps_to_itself(configured: str) -> None:
    record_streamable_http_surface(configured, stateless=True)
    assert mcp_endpoint_for("/custom/endpoint") == "/custom/endpoint"
    assert mcp_endpoint_for("/mcp") is None


def test_the_sse_stream_and_its_message_endpoint_map_to_the_stream() -> None:
    record_sse_surface("/sse", "/messages/")
    assert mcp_endpoint_for("/sse") == "/sse"
    # The bare prefix is the canonical path of every real ``POST /messages/?session_id=…``.
    assert mcp_endpoint_for("/messages") == "/sse"
    assert mcp_endpoint_for("/messages/x") == "/sse"
    assert mcp_endpoint_for("/messagesx") is None


def test_a_custom_sse_pair_is_read_from_its_records() -> None:
    record_sse_surface("/events/stream", "/events/post")
    assert mcp_endpoint_for("/events/stream") == "/events/stream"
    assert mcp_endpoint_for("/events/post") == "/events/stream"


@pytest.fixture
def _sub_mcp_mounted() -> None:
    record_sub_mcp_mount(ROOT_PREFIX)


@pytest.mark.usefixtures("_sub_mcp_mounted")
@pytest.mark.parametrize(
    ("path", "endpoint"),
    [
        ("/app/svc", "/app/svc"),
        ("/app/svc/sse", "/app/svc/sse"),
        ("/app/svc/messages", "/app/svc/sse"),
        ("/app/svc/messages/x", "/app/svc/sse"),
        ("/app/svc/other", None),
        ("/app/svc/sse/x", None),
        ("/app/Bad_Slug", None),
        ("/app/-x", None),
        ("/app/a%2Fb", None),
        ("/app", None),
    ],
)
def test_the_sub_mcp_rule_is_by_path_shape(path: str, endpoint: str | None) -> None:
    assert mcp_endpoint_for(path) == endpoint


@pytest.mark.usefixtures("_sub_mcp_mounted")
def test_a_slug_no_router_knows_gets_the_same_answer_as_a_known_one() -> None:
    assert "never-registered" not in route_registry.routes()
    assert mcp_endpoint_for("/app/never-registered") == "/app/never-registered"
    assert mcp_endpoint_for("/app/never-registered/messages") == "/app/never-registered/sse"


@pytest.mark.usefixtures("_sub_mcp_mounted")
def test_an_api_path_and_the_root_map_to_nothing() -> None:
    record_streamable_http_surface("/mcp", stateless=False)
    assert mcp_endpoint_for("/api/tools") is None
    assert mcp_endpoint_for("/") is None
    assert mcp_endpoint_for("/mcpx") is None


def test_the_map_follows_a_rebuild_that_records_another_transport() -> None:
    record_streamable_http_surface("/mcp", stateless=False)
    assert mcp_endpoint_for("/mcp") == "/mcp"
    # A rebuild serving SSE records its surfaces; the map reads the new records at once.
    record_sse_surface("/sse", "/messages/")
    assert mcp_endpoint_for("/messages") == "/sse"
    record_streamable_http_surface("/next", stateless=False)
    assert mcp_endpoint_for("/next") == "/next"
