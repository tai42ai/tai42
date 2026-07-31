"""get_mcp_transport: non-UDS servers get a plain dict-config; UDS servers get
the transport for the selected wire protocol."""

import pytest
from tai42_contract.manifest import MCPConfig, TaiMCPConfig
from tai42_contract.transport import Transport

from tai42_kit.transport import HTTPUDSTransport, SSEUDSTransport, get_mcp_transport


def test_uds_transports_satisfy_contract_protocol():
    # The UDS transports implement the contract ``Transport`` protocol — the
    # kit→contract link the module claims.
    assert isinstance(HTTPUDSTransport(socket_path="/tmp/s.sock"), Transport)
    assert isinstance(SSEUDSTransport(socket_path="/tmp/s.sock"), Transport)


def test_non_uds_returns_dict_config():
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(url="http://host/mcp"))
    transport = get_mcp_transport(cfg)
    assert transport == {"mcpServers": {"srv": cfg.config.model_dump(exclude_none=True)}}


def test_non_uds_ignores_uds_protocol():
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(url="http://host/mcp"))
    assert isinstance(get_mcp_transport(cfg, uds_protocol="sse"), dict)


def test_uds_defaults_to_http():
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(uds="/tmp/s.sock"))
    transport = get_mcp_transport(cfg)
    assert isinstance(transport, HTTPUDSTransport)
    assert transport.socket_path == "/tmp/s.sock"


def test_uds_sse_selected():
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(uds="/tmp/s.sock"))
    assert isinstance(get_mcp_transport(cfg, uds_protocol="sse"), SSEUDSTransport)


def test_uds_unknown_protocol_raises():
    # An unknown wire protocol must fail loudly rather than fall through to the
    # HTTP transport (and silently split the pool key).
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(uds="/tmp/s.sock"))
    with pytest.raises(ValueError, match="unsupported uds_protocol 'streamable-http'"):
        get_mcp_transport(cfg, uds_protocol="streamable-http")
