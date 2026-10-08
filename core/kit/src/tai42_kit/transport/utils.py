"""Build the MCP transport for one server config."""

from typing import Any

from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.client.transports.sse import SSETransport
from fastmcp.mcp_config import infer_transport_type_from_url
from tai42_contract.manifest import TaiMCPConfig

from tai42_kit.transport.http_uds_transport import HTTPUDSTransport
from tai42_kit.transport.mcp_http import mcp_http_client
from tai42_kit.transport.sse_uds_transport import SSEUDSTransport


def get_mcp_transport(
    config: TaiMCPConfig, *, uds_protocol: str = "http"
) -> HTTPUDSTransport | SSEUDSTransport | StreamableHttpTransport | SSETransport | dict[str, Any]:
    """Build the fastmcp transport for one MCP server config.

    A UDS server connects over a unix socket; ``uds_protocol`` selects the MCP
    wire protocol carried on it (``http`` = streamable-http, ``sse`` = SSE) — the
    caller (which owns the runtime/CLI) supplies it, so it arrives unvalidated and
    any other value raises. A URL server gets fastmcp's SSE or streamable-http
    transport, chosen by fastmcp's own URL inference, carrying only the config's
    headers and built on :func:`mcp_http_client`; it ignores ``uds_protocol``. A
    command server returns a plain fastmcp dict-config.
    """
    socket_path = config.config.uds
    if socket_path:  # a truthy ``uds`` marks a UDS server, and this test narrows its type
        if uds_protocol == "sse":
            return SSEUDSTransport(socket_path=socket_path)
        if uds_protocol == "http":
            return HTTPUDSTransport(socket_path=socket_path)
        raise ValueError(f"unsupported uds_protocol {uds_protocol!r}; expected 'http' or 'sse'")
    url = config.config.url
    if url:
        headers = dict(config.config.headers)
        if infer_transport_type_from_url(url) == "sse":
            return SSETransport(url, headers=headers, httpx_client_factory=mcp_http_client)
        return StreamableHttpTransport(url, headers=headers, httpx_client_factory=mcp_http_client)
    return {"mcpServers": {config.title: config.config.model_dump(exclude_none=True)}}
