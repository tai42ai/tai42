"""MCP client transports over a unix-domain socket + the transport selector.

The UDS transports implement the contract ``Transport`` Protocol; ``get_mcp_transport``
picks between them, fastmcp's remote transports for URL servers, or fastmcp's stdio
transport for command servers, whose bare command ``resolve_stdio_command`` finds on a
``PATH`` or among the commands the declared :data:`StdioLaunch` provider exposes, and
whose child's stderr is this process's standard error descriptor (see
:mod:`tai42_kit.transport.stdio_launch`).
"""

from tai42_kit.transport.base_uds_transport import BaseUDSTransport
from tai42_kit.transport.http_uds_transport import HTTPUDSTransport
from tai42_kit.transport.sse_uds_transport import SSEUDSTransport
from tai42_kit.transport.stdio_launch import (
    StdioLaunch,
    declare_stdio_launch,
    declared_stdio_launch,
    resolve_stdio_command,
)
from tai42_kit.transport.utils import get_mcp_transport

__all__ = [
    "BaseUDSTransport",
    "HTTPUDSTransport",
    "SSEUDSTransport",
    "StdioLaunch",
    "declare_stdio_launch",
    "declared_stdio_launch",
    "get_mcp_transport",
    "resolve_stdio_command",
]
