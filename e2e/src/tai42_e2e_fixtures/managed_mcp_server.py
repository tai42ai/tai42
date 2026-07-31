"""A self-contained managed MCP stdio server the connector fixtures launch.

The connector fixtures in ``connector_provider`` point a sub-service's ``mcp_server`` at
this module, so the connectors engine spawns it as a stdio child on connect and its tools
bind onto the fleet's MCP surface. Launched as ``python -m
tai42_e2e_fixtures.managed_mcp_server`` — no network, no package index, no ``uvx``.

A REAL FastMCP server, never a mock. It carries no connector auth glue: the injected OAuth
token (via ``_meta``) and no-auth ``config_values`` (via env) are handled engine-side of the
wire contract. ``reflect_env`` exposes the injected ``config_values`` so a scenario can read
one back."""

from __future__ import annotations

import os

from fastmcp import FastMCP

mcp = FastMCP("e2e-managed-mcp")


# Every tool returns an object (never a bare scalar): FastMCP wraps a scalar under
# ``{"result": ...}``, which the connector proxy's asymmetric schema-unwrap double-wraps
# and output-schema validation rejects. No key is named ``result`` (the proxy unwrap treats
# it specially).


@mcp.tool
def ping() -> dict:
    """Return the known object ``{"ok": "pong"}`` — the trivially-callable liveness
    tool a scenario calls to prove the managed server launched and serves."""
    return {"ok": "pong"}


@mcp.tool
def echo(payload: str) -> dict:
    """Return ``{"echoed": payload}`` — proves argument round-tripping across the
    managed stdio transport."""
    return {"echoed": payload}


@mcp.tool
def reflect_env(key: str) -> dict:
    """Return ``{"value": <env[key]>}`` (``""`` if unset).

    A no-auth connection's ``config_values`` are injected into the launch env on
    the stdio transport, so a scenario reads an injected config value back through
    this tool."""
    return {"value": os.environ.get(key, "")}


if __name__ == "__main__":
    # ``show_banner=False`` keeps the FastMCP banner off the child's stderr — it is
    # spawned as a managed connector child, never run interactively.
    mcp.run(transport="stdio", show_banner=False)
