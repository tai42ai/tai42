"""The eta fixture's minimal stdio MCP server.

The fixture provides a single ``mcp-server`` item whose ``mcp.command`` launches
this server over stdio (the ``tai-e2e-market-eta-mcp`` console script). Installing
the fixture writes the manifest ``mcp`` entry, and the skeleton's MCP loader spawns
this process and binds its one tool onto the serving ``/mcp`` under the entry title
— so a test proves an installed mcp-server item really mounts and answers.

It exposes exactly one trivial tool (``ping``) and depends only on ``fastmcp``,
already present in the serving environment the fixture installs into — the fixture
package itself declares no dependencies (an mcp-server package carries no
tai42-contract)."""

from __future__ import annotations

import os

from fastmcp import FastMCP

_server = FastMCP("tai-e2e-market-eta")


@_server.tool
def ping() -> dict[str, object]:
    """Return a fixed marker payload identifying the eta fixture MCP server."""
    return {"eta": "pong", "pid": os.getpid()}


def main() -> None:
    """Run the server over stdio (the console-script entry point)."""
    _server.run()


if __name__ == "__main__":
    main()
