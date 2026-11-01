"""The MCP server a sub-service runs on: stdio launch-spec synthesis and its transport config.

A ``SubServiceDescriptor`` declares exactly one launch path: either a fully
built ``mcp_server``, or a pkg-launched stdio service via ``entry_point`` (with
``mcp_server`` left unset). :func:`resolve_mcp_server` collapses both into a
concrete :class:`McpServerDescriptor` for every read site (runtime resolver,
probe, manifest writer), so a pure-data plugin never pre-declares a half-built
command and the ``McpServerDescriptor`` stdio-requires-command invariant stays
total.

For the pkg-launched case the command is synthesized from the provider-level
``pkg_manager`` (``uvx`` / ``npx``) + ``pkg_version`` and the sub-service
``entry_point``. Every interpolated value passes the argv-injection guard
(:func:`reject_leading_dash`) so a value can't be parsed as a launcher flag.

:func:`mcp_config_for` turns the resolved server into the manifest ``MCPConfig``
every connector-built entry (managed entry, liveness probe) dispatches on.
"""

from __future__ import annotations

from tai42_contract.connectors.providers import (
    McpServerDescriptor,
    ProviderDescriptor,
)
from tai42_contract.manifest import MCPConfig

from tai42_skeleton.connectors.stdio.launcher import reject_leading_dash


def resolve_mcp_server(descriptor: ProviderDescriptor, sub_service_id: str) -> McpServerDescriptor:
    """Return the concrete MCP server endpoint for ``sub_service_id``.

    - ``mcp_server`` set → returned as-is.
    - ``entry_point`` set → a stdio ``McpServerDescriptor`` synthesized from the
      provider's ``pkg_manager`` + ``pkg_version`` and the entry point.
    - neither → loud raise (the contract XOR validator guarantees one is set;
      this is a belt-and-braces guard).

    Raises ``ValueError`` on an unknown/missing ``pkg_manager`` or a value that
    fails the argv-injection guard.
    """
    sub_service = descriptor.sub_services[sub_service_id]
    if sub_service.mcp_server is not None:
        return sub_service.mcp_server

    entry_point = sub_service.entry_point
    if entry_point is None:
        raise ValueError(
            f"sub-service {sub_service_id!r} of provider {descriptor.id!r} sets neither mcp_server nor entry_point"
        )

    reject_leading_dash(entry_point, field="entry_point")
    pkg_version = descriptor.pkg_version
    if pkg_version is not None:
        reject_leading_dash(pkg_version, field="pkg_version")

    pkg_manager = descriptor.pkg_manager
    if pkg_manager == "uvx":
        args = ["--from", f"{entry_point}=={pkg_version}", entry_point] if pkg_version is not None else [entry_point]
        command = "uvx"
    elif pkg_manager == "npx":
        args = ["-y", f"{entry_point}@{pkg_version}"] if pkg_version is not None else ["-y", entry_point]
        command = "npx"
    else:
        raise ValueError(
            f"provider {descriptor.id!r} sub-service {sub_service_id!r} is "
            f"pkg-launched (entry_point={entry_point!r}) but pkg_manager is "
            f"{pkg_manager!r} (expected 'uvx' or 'npx')"
        )

    return McpServerDescriptor(type="stdio", command=command, args=args)


def mcp_config_for(server: McpServerDescriptor) -> MCPConfig:
    """Return the manifest ``MCPConfig`` for ``server``: ``command`` for stdio, ``url`` for http."""
    if server.type == "stdio":
        # env carries static transport config only; the access token is injected
        # per-call via JSON-RPC _meta, never via env.
        return MCPConfig(type="stdio", command=server.command, args=list(server.args), env=dict(server.env))
    return MCPConfig(type="http", url=server.url, headers=dict(server.extra_headers))
