"""B8 — tai42-mcp-dynamic-postgres mounted as a product-level external MCP.

The manifest's single ``mcp`` entry launches the released package's ``tai42-mcp-dynamic-postgres``
console script as a stdio child pointed at the harness postgres (see
``manifests.build_postgres_mcp_stack``). This is the harness's FIRST manifest-``mcp`` mount —
NOT the ``/api/sub-mcp`` composition router, which only re-exposes already-registered tools.
The app's boot-time MCP loader discovers the child's tools and binds them onto THIS server's
MCP surface under the ``postgres`` title prefix.

The child is a DYNAMIC/codegen MCP: at startup it introspects the connected schema and
generates one tool per relation per verb (``<verb>_<schema>_<table>``) — there is no raw
``execute_sql`` tool. The ``postgres_mcp_stack`` fixture seeds a known probe table into the
stack's isolated postgres clone BEFORE boot, so the child generates its CRUD tool set and the
mount binds it as ``postgres_<verb>_<schema>_<table>``.

Asserts (1) tool listing — the mounted server contributed the probe table's generated CRUD
tools under the title prefix — and (2) one query round trip: the seeded row comes back through
the generated ``select`` tool dispatched over the product's own ``/mcp``.
"""

from __future__ import annotations

from tai42_e2e.manifests import POSTGRES_MCP_PROBE_ROW_NAME, POSTGRES_MCP_TITLE, postgres_mcp_tool_name
from tai42_e2e.stack import TaiStack


async def test_postgres_mcp_tools_are_mounted(postgres_mcp_stack: TaiStack) -> None:
    async with postgres_mcp_stack.mcp(port=postgres_mcp_stack.port_a) as mcp:
        names = await mcp.tool_names()
    prefixed = sorted(n for n in names if n.startswith(f"{POSTGRES_MCP_TITLE}_"))
    assert prefixed, f"no {POSTGRES_MCP_TITLE!r}-prefixed tools mounted from the external MCP: {sorted(names)}"
    # The dynamic MCP generated the full CRUD set for the seeded probe relation; each is bound
    # under the title prefix as postgres_<verb>_<schema>_<table>. Asserting all four (write verbs
    # included) proves the mounted surface is the codegen surface, not a fixed tool list.
    for verb in ("select", "insert", "update", "delete"):
        expected = postgres_mcp_tool_name(verb)
        assert expected in names, f"generated {verb} tool {expected!r} not mounted: {prefixed}"


async def test_postgres_mcp_query_round_trip(postgres_mcp_stack: TaiStack) -> None:
    # The generated select tool takes only optional filters, so an empty argument set selects
    # every row of the probe relation — including the row the fixture seeded before boot, which
    # the mounted server reads back through its OWN connection to the stack's postgres clone.
    #
    # Dispatched through the ordinary validated fastmcp-client path (same as every other mounted
    # tool test): the mcp SDK jsonschema-validates the mounted tool's ``structuredContent``
    # against its advertised single-wrap output schema on the way back. A list-returning mounted
    # tool round trips here only because the mount now forwards a single, consistent ``result``
    # wrap — the double wrap that once forced a raw ``tools/call`` around this validator is gone.
    async with postgres_mcp_stack.mcp(port=postgres_mcp_stack.port_a) as mcp:
        result = await mcp.call_tool(postgres_mcp_tool_name("select"), {})
    assert not result.is_error, f"select tool returned an error result: {result.content!r}"
    # Shape-tolerant: the seeded value must appear whatever envelope the mounted server
    # serializes it as (structured data and/or text content parts).
    blob = str(result.structured_content) + " " + " ".join(getattr(part, "text", "") for part in result.content)
    assert POSTGRES_MCP_PROBE_ROW_NAME in blob, f"seeded row not in select result: {blob!r}"
