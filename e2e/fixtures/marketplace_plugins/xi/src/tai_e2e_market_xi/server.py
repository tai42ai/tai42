"""The xi fixture's console-script entry point.

The package's install beside an installed eta is refused before any launch, so the
script serves no MCP protocol: run, it exits with an error naming the package.
"""

from __future__ import annotations


def main() -> None:
    """Exit non-zero: the xi fixture ships no MCP server."""
    raise SystemExit("tai-e2e-market-xi ships no MCP server; its console script only claims a name")
