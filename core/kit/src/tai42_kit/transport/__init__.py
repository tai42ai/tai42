"""MCP client transports over a unix-domain socket + the transport selector.

The UDS transports implement the contract ``Transport`` Protocol; ``get_mcp_transport``
picks between them, fastmcp's remote transports for URL servers, or a plain
dict-config for command servers, launched as the declared :class:`StdioLaunch` says:
``resolve_stdio_command`` finds the command and the install location it is in, and
``stdio_child_env`` composes the child's ``env`` with that location's import directories
(see :mod:`tai42_kit.transport.stdio_launch`).
"""

from tai42_kit.transport.base_uds_transport import BaseUDSTransport
from tai42_kit.transport.http_uds_transport import HTTPUDSTransport
from tai42_kit.transport.sse_uds_transport import SSEUDSTransport
from tai42_kit.transport.stdio_launch import (
    ResolvedStdioCommand,
    ScriptLocation,
    StdioLaunch,
    declare_stdio_launch,
    declared_stdio_launch,
    resolve_stdio_command,
    stdio_child_env,
)
from tai42_kit.transport.utils import get_mcp_transport

__all__ = [
    "BaseUDSTransport",
    "HTTPUDSTransport",
    "ResolvedStdioCommand",
    "SSEUDSTransport",
    "ScriptLocation",
    "StdioLaunch",
    "declare_stdio_launch",
    "declared_stdio_launch",
    "get_mcp_transport",
    "resolve_stdio_command",
    "stdio_child_env",
]
