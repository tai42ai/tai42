from typing import Any

from tai42_contract.manifest import TaiMCPConfig

from tai42_kit.transport.http_uds_transport import HTTPUDSTransport
from tai42_kit.transport.sse_uds_transport import SSEUDSTransport


def get_mcp_transport(
    config: TaiMCPConfig, *, uds_protocol: str = "http"
) -> HTTPUDSTransport | SSEUDSTransport | dict[str, Any]:
    """Build the fastmcp transport for one MCP server config.

    A UDS server connects over a unix socket; ``uds_protocol`` selects the MCP
    wire protocol carried on it (``http`` = streamable-http, ``sse`` = SSE) — the
    caller (which owns the runtime/CLI) supplies it, so it arrives unvalidated and
    any other value raises. A non-UDS server returns a plain fastmcp dict-config
    and ignores ``uds_protocol``.
    """
    socket_path = config.config.uds
    if socket_path:  # the ``TaiMCPConfig.is_uds`` rule, inline so it also narrows the type
        if uds_protocol == "sse":
            return SSEUDSTransport(socket_path=socket_path)
        if uds_protocol == "http":
            return HTTPUDSTransport(socket_path=socket_path)
        raise ValueError(f"unsupported uds_protocol {uds_protocol!r}; expected 'http' or 'sse'")
    return {"mcpServers": {config.title: config.config.model_dump(exclude_none=True)}}
