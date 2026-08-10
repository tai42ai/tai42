"""Lock the MCP outbound-header security posture.

FastMCPClient relies on fastmcp transports defaulting to
``forward_incoming_headers=False`` so this server's own inbound headers (incl.
auth) are never leaked to a downstream MCP — only headers declared in the
server's own config are sent. These tests fail loudly if a future fastmcp
default-change would start forwarding ambient headers, instead of letting the
leak happen silently.
"""

from typing import Any, cast

from fastmcp import Client
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.client.transports.sse import SSETransport
from tai42_contract.manifest import MCPConfig, TaiMCPConfig

from tai42_kit.transport import get_mcp_transport


def _resolved_transport(cfg: TaiMCPConfig) -> Any:
    """The per-server transport fastmcp resolves from our non-UDS dict config
    (Client -> MCPConfigTransport -> single-server transport)."""
    return cast(Any, Client(get_mcp_transport(cfg)).transport).transport


def test_http_transport_does_not_forward_incoming_headers_by_default():
    t = StreamableHttpTransport(url="http://host/mcp", headers={"Authorization": "Bearer downstream"})
    # No ambient/incoming header forwarding — our auth is not leaked downstream.
    assert t.forward_incoming_headers is False
    # The downstream's own explicit auth IS sent.
    assert t.headers["Authorization"] == "Bearer downstream"


def test_sse_transport_does_not_forward_incoming_headers_by_default():
    t = SSETransport(url="http://host/sse", headers={"Authorization": "Bearer downstream"})
    assert t.forward_incoming_headers is False
    assert t.headers["Authorization"] == "Bearer downstream"


def test_non_uds_config_carries_explicit_auth_header():
    # The downstream server's declared auth header reaches the outbound config.
    cfg = TaiMCPConfig(
        title="srv",
        config=MCPConfig(url="http://host/mcp", headers={"Authorization": "Bearer downstream"}),
    )
    transport = get_mcp_transport(cfg)
    assert isinstance(transport, dict)
    assert transport["mcpServers"]["srv"]["headers"] == {"Authorization": "Bearer downstream"}


def test_non_uds_config_without_auth_sends_no_headers():
    # No declared headers → none are sent (no ambient/auth leak path).
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(url="http://host/mcp"))
    transport = get_mcp_transport(cfg)
    assert isinstance(transport, dict)
    assert transport["mcpServers"]["srv"]["headers"] == {}


def test_non_uds_client_end_to_end_is_header_isolated():
    # The full path FastMCPClient uses (dict config -> Client -> resolved
    # transport): the outbound transport never forwards ambient/inbound headers
    # and carries only the config's own auth. With forward_incoming_headers=False
    # the outbound headers are exactly dict(self.headers) — the config's own
    # headers, no ambient forwarding.
    cfg = TaiMCPConfig(
        title="srv",
        config=MCPConfig(url="http://host/mcp", headers={"Authorization": "Bearer downstream"}),
    )
    inner = _resolved_transport(cfg)
    assert inner.forward_incoming_headers is False
    assert inner.headers["Authorization"] == "Bearer downstream"


def test_non_uds_client_end_to_end_without_auth_sends_no_headers():
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(url="http://host/mcp"))
    inner = _resolved_transport(cfg)
    assert inner.forward_incoming_headers is False
    assert dict(inner.headers) == {}
