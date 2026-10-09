"""Pull-based reachability check for a managed sub-service's MCP server.

:func:`probe` — bool liveness for the API status path (never the tool-call hot
path). Best-effort: it never raises and logs at DEBUG.

It opens the same MCP transport the runtime uses for the sub-service and runs the
MCP handshake plus a ``tools/list`` round-trip under a short timeout. The
credential is applied by :func:`prepare_managed_request`, the preparation every
managed dispatch uses: an OAuth token rides the ``authorization`` header on http,
a no-auth client config merges into the headers (http) or the launch env (stdio).
A stdio OAuth token travels per call in ``_meta``, which ``initialize`` and
``tools/list`` do not carry, so a stdio child answers the probe without it.
Liveness is "the server initialised and answered".

The client is the app-pooled ``FastMCPClient`` taken ``fresh=True`` — a one-shot
client built outside the shared pool and closed on exit, so a probe (which may
spawn a stdio child) never holds or leaks a shared-pool connection.
"""

from __future__ import annotations

import asyncio
import logging

from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.manifest import MCPConfig, TaiMCPConfig
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.mcp import FastMCPClient

from tai42_skeleton.connectors.runtime.launch import resolve_mcp_server
from tai42_skeleton.connectors.runtime.resolver import ManagedAuth
from tai42_skeleton.connectors.token_injection import prepare_managed_request

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 5.0


def _build_probe_config(descriptor: ProviderDescriptor, sub_service: str, auth: ManagedAuth | None) -> TaiMCPConfig:
    """Build the MCP transport config for a liveness probe of ``sub_service``.

    The base entry comes from the descriptor's server alone; ``auth`` is applied by the managed
    request preparation. Read-only: no refresh, no retry.
    """
    server = resolve_mcp_server(descriptor, sub_service)
    if server.type == "stdio":
        inner = MCPConfig(type="stdio", command=server.command, args=list(server.args), env=dict(server.env))
    else:
        inner = MCPConfig(type=server.type, url=server.url, headers=dict(server.extra_headers))
    base = TaiMCPConfig(title=f"probe_{descriptor.id}_{sub_service}", config=inner)
    return prepare_managed_request(base, auth, server.type)[0]


def _probe_client(config: TaiMCPConfig):
    """A one-shot pooled MCP client for ``config`` (built off-pool, closed on exit)."""
    return client_ctx(FastMCPClient, config=config.model_dump(mode="json"), fresh=True)


async def probe(
    descriptor: ProviderDescriptor,
    sub_service: str,
    *,
    auth: ManagedAuth | None = None,
) -> bool:
    """Return whether the MCP server for ``sub_service`` is currently live.

    Opens the MCP transport, completes ``initialize`` + ``tools/list`` under a
    short timeout, and returns whether the server answered. Works for both stdio
    and http sub-services; ``auth`` is the resolved managed credential, or ``None``
    to inject nothing.

    Never raises: any error (unknown sub-service, transport failure, spawn
    failure, timeout) is logged at DEBUG and reported as unreachable.
    """
    if sub_service not in descriptor.sub_services:
        logger.debug(
            "connectors: probe — unknown sub_service %r for provider %s",
            sub_service,
            descriptor.id,
        )
        return False

    try:
        config = _build_probe_config(descriptor, sub_service, auth)
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            async with _probe_client(config) as client:
                await client.list_tools()
    except Exception:
        logger.debug(
            "connectors: probe — %s sub_service %s unreachable",
            descriptor.id,
            sub_service,
            exc_info=True,
        )
        return False
    return True
