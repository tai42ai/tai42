"""Read a served app's FastMCP route table through the HTTP surface's one adapter."""

from __future__ import annotations

from typing import Any

from starlette.routing import BaseRoute

from tai42_skeleton.app.http import _fastmcp_route_table


def route_table(app: Any) -> list[BaseRoute]:
    """The live additional-route list of ``app``'s served FastMCP server."""
    routes = _fastmcp_route_table(app._fast_mcp)
    assert routes is not None
    return routes
