"""MCP client transports over a unix-domain socket + the transport selector.

The UDS transports implement the contract ``Transport`` Protocol; ``get_mcp_transport``
picks between them (or a plain dict-config for non-UDS servers).
"""

from tai42_kit.transport.base_uds_transport import BaseUDSTransport
from tai42_kit.transport.http_uds_transport import HTTPUDSTransport
from tai42_kit.transport.sse_uds_transport import SSEUDSTransport
from tai42_kit.transport.utils import get_mcp_transport

__all__ = [
    "BaseUDSTransport",
    "HTTPUDSTransport",
    "SSEUDSTransport",
    "get_mcp_transport",
]
