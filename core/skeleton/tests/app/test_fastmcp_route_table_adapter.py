"""Vendor pin: the route-table adapter returns the live list FastMCP's ``custom_route`` appends to.

FastMCP exposes no public accessor for its additional-HTTP-route list; the HTTP surface edits
that list in place (raw-path upgrade, SPA fallback, module savepoint/rollback) through one
adapter. A FastMCP release that moves or copies the list fails here.
"""

from __future__ import annotations

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from tai42_skeleton.app.http import _fastmcp_route_table


async def _handler(request: Request) -> Response:
    return PlainTextResponse("ok")


def test_custom_route_appends_to_the_list_the_adapter_returns() -> None:
    server = FastMCP("route-table-pin")
    table = _fastmcp_route_table(server)
    assert table is not None
    before = len(table)

    server.custom_route("/pinned", methods=["GET"])(_handler)

    assert len(table) == before + 1
    route = table[-1]
    assert isinstance(route, Route)
    assert route.path == "/pinned"
    assert _fastmcp_route_table(server) is table


def test_an_object_without_a_route_table_yields_none() -> None:
    assert _fastmcp_route_table(object()) is None
