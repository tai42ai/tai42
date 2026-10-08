"""The HTTP client every kit-built MCP transport sends through."""

import httpx
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

from tai42_kit.clients.settings import mcp_client_settings

__all__ = ["mcp_http_client", "mcp_http_limits"]

# httpx's own pool sizes (``httpx._config.DEFAULT_LIMITS``).
_MAX_CONNECTIONS = 100
_MAX_KEEPALIVE_CONNECTIONS = 20


def mcp_http_limits() -> httpx.Limits:
    """The connection limits of every kit-built MCP HTTP client.

    httpx's default pool sizes, with idle connections kept for
    ``MCP_CLIENT_KEEPALIVE_EXPIRY_SECONDS``.
    """
    return httpx.Limits(
        max_connections=_MAX_CONNECTIONS,
        max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
        keepalive_expiry=mcp_client_settings().keepalive_expiry_seconds,
    )


def mcp_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    *,
    follow_redirects: bool = True,
) -> httpx.AsyncClient:
    """The ``httpx_client_factory`` of every kit-built remote MCP transport.

    The MCP SDK's own client construction (redirects followed, the MCP default
    timeout when none is passed) with :func:`mcp_http_limits`. The keywords are
    exactly those fastmcp and the MCP SDK pass; any other raises ``TypeError``.
    """
    return httpx.AsyncClient(
        follow_redirects=follow_redirects,
        timeout=timeout
        if timeout is not None
        else httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
        headers=headers,
        auth=auth,
        limits=mcp_http_limits(),
    )
