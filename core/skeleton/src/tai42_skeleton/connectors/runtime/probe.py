"""Pull-based reachability check for a managed sub-service's MCP server.

:func:`probe` — bool liveness for the API status path (never the tool-call hot
path). Best-effort: it never raises and logs at DEBUG.

It opens the same MCP transport the runtime uses for the sub-service and runs the
MCP handshake plus a ``tools/list`` round-trip on the MCP start loop
(:func:`~tai42_skeleton.tools.mcp_start.start_listing`), waiting a short time for the
answer. A start still in progress when the wait ends reads as not live, but it is never
torn down for that: it goes on up to the start budget (``mcp_probe_timeout``), closes its
client itself, and its late outcome is logged at INFO. A probe of the same config while
such a start is in flight joins it instead of starting the server again. The
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

import logging
from typing import Any

from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.manifest import TaiMCPConfig
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.mcp import FastMCPClient

from tai42_skeleton.connectors.runtime.launch import mcp_config_for, resolve_mcp_server
from tai42_skeleton.connectors.runtime.resolver import ManagedAuth
from tai42_skeleton.connectors.token_injection import prepare_managed_request
from tai42_skeleton.settings.cache import mcp_probe_timeout
from tai42_skeleton.tools.mcp_start import StartId, outcome_text, start_listing

logger = logging.getLogger(__name__)

# How long a liveness probe waits for the server start; the start itself goes on up to the start budget.
_PROBE_WAIT_SECONDS = 5.0

# Names a liveness probe's start: a probe of a config whose probe is in flight joins that start.
_PROBE_KEY = "connector-liveness-probe"


def _build_probe_config(descriptor: ProviderDescriptor, sub_service: str, auth: ManagedAuth | None) -> TaiMCPConfig:
    """Build the MCP transport config for a liveness probe of ``sub_service``.

    The base entry comes from the descriptor's server alone; ``auth`` is applied by the managed
    request preparation. Read-only: no refresh, no retry.
    """
    server = resolve_mcp_server(descriptor, sub_service)
    base = TaiMCPConfig(title=f"probe_{descriptor.id}_{sub_service}", config=mcp_config_for(server))
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

    Opens the MCP transport, completes ``initialize`` + ``tools/list`` within a
    short wait, and returns whether the server answered. Works for both stdio
    and http sub-services; ``auth`` is the resolved managed credential, or ``None``
    to inject nothing.

    Never raises: any error (unknown sub-service, transport failure, spawn
    failure, a start still in progress when the wait ends) is logged at DEBUG and
    reported as unreachable.
    """
    if sub_service not in descriptor.sub_services:
        logger.debug(
            "connectors: probe — unknown sub_service %r for provider %s",
            sub_service,
            descriptor.id,
        )
        return False

    def late(_start: StartId, outcome: Any) -> None:
        logger.info(
            "connectors: probe of %s/%s finished after its wait: %s",
            descriptor.id,
            sub_service,
            outcome_text(outcome),
        )

    try:
        config = _build_probe_config(descriptor, sub_service, auth)

        async def run() -> None:
            async with _probe_client(config) as client:
                await client.list_tools()

        await start_listing(
            run,
            title=config.title,
            key=(_PROBE_KEY, config.model_dump_json()),
            wait=_PROBE_WAIT_SECONDS,
            lifetime=mcp_probe_timeout(),
            on_late_outcome=late,
        )
    except Exception:
        logger.debug(
            "connectors: probe — %s sub_service %s unreachable",
            descriptor.id,
            sub_service,
            exc_info=True,
        )
        return False
    return True
