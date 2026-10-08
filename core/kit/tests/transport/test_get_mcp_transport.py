"""get_mcp_transport: a URL server gets fastmcp's HTTP or SSE transport built on the
kit's MCP HTTP client; a UDS server gets the transport for the selected wire protocol;
a command server gets fastmcp's dict config. Every kit-built MCP HTTP client keeps its
idle connections for ``MCP_CLIENT_KEEPALIVE_EXPIRY_SECONDS``."""

from typing import Any

import httpx
import pytest
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.client.transports.sse import SSETransport
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT
from pydantic import ValidationError
from tai42_contract.manifest import MCPConfig, TaiMCPConfig
from tai42_contract.transport import Transport

from tai42_kit.clients.settings import MCPClientSettings
from tai42_kit.settings import reset_all_settings
from tai42_kit.transport import HTTPUDSTransport, SSEUDSTransport, get_mcp_transport
from tai42_kit.transport.mcp_http import mcp_http_client, mcp_http_limits


def _pool_of(client: httpx.AsyncClient) -> Any:
    """The httpcore connection pool the client's transport sends through."""
    return client._transport._pool  # type: ignore[attr-defined]


def test_uds_transports_satisfy_contract_protocol():
    # The UDS transports implement the contract ``Transport`` protocol — the
    # kit→contract link the module claims.
    assert isinstance(HTTPUDSTransport(socket_path="/tmp/s.sock"), Transport)
    assert isinstance(SSEUDSTransport(socket_path="/tmp/s.sock"), Transport)


def test_url_server_gets_streamable_http_on_the_kit_client():
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(url="http://host/mcp", headers={"X-Key": "v"}))

    transport = get_mcp_transport(cfg)

    assert isinstance(transport, StreamableHttpTransport)
    assert transport.url == "http://host/mcp"
    assert transport.headers == {"X-Key": "v"}
    assert transport.httpx_client_factory is mcp_http_client
    assert transport.auth is None


def test_sse_url_gets_the_sse_transport_on_the_kit_client():
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(url="http://host/sse"))

    transport = get_mcp_transport(cfg)

    assert isinstance(transport, SSETransport)
    assert transport.url == "http://host/sse"
    assert transport.headers == {}
    assert transport.httpx_client_factory is mcp_http_client


def test_the_transport_follows_the_url_not_the_type_field():
    # fastmcp's own URL inference picks the wire protocol, as its dict config did.
    sse_typed = TaiMCPConfig(title="srv", config=MCPConfig(url="http://host/mcp", type="sse"))
    http_typed = TaiMCPConfig(title="srv", config=MCPConfig(url="http://host/sse", type="http"))

    assert isinstance(get_mcp_transport(sse_typed), StreamableHttpTransport)
    assert isinstance(get_mcp_transport(http_typed), SSETransport)


def test_url_server_ignores_uds_protocol():
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(url="http://host/mcp"))
    assert isinstance(get_mcp_transport(cfg, uds_protocol="sse"), StreamableHttpTransport)


def test_an_invalid_url_raises():
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(url="ftp://host/mcp"))
    with pytest.raises(ValueError, match="Invalid URL"):
        get_mcp_transport(cfg)


def test_command_server_returns_dict_config():
    cfg = TaiMCPConfig(title="srv", config=MCPConfig(command="run-server", args=["--stdio"]))
    transport = get_mcp_transport(cfg)
    assert transport == {"mcpServers": {"srv": cfg.config.model_dump(exclude_none=True)}}


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


async def test_the_kit_client_keeps_idle_connections_for_the_setting():
    async with mcp_http_client() as client:
        pool = _pool_of(client)
        assert pool._keepalive_expiry == 30
        assert pool._max_connections == 100
        assert pool._max_keepalive_connections == 20
        assert client.follow_redirects is True
        assert client.timeout == httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)


async def test_the_keepalive_expiry_follows_the_setting(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_CLIENT_KEEPALIVE_EXPIRY_SECONDS", "12.5")
    reset_all_settings()

    assert mcp_http_limits().keepalive_expiry == 12.5
    async with mcp_http_client() as client:
        assert _pool_of(client)._keepalive_expiry == 12.5


async def test_the_kit_client_carries_what_the_transport_passes():
    auth = httpx.BasicAuth("alice", "pw")
    timeout = httpx.Timeout(30.0, read=7.0)

    async with mcp_http_client(headers={"X-Key": "v"}, auth=auth, follow_redirects=True, timeout=timeout) as client:
        assert client.headers["X-Key"] == "v"
        assert client.auth is auth
        assert client.timeout == timeout


def test_the_kit_client_refuses_an_unknown_keyword():
    with pytest.raises(TypeError):
        mcp_http_client(verify=False)  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_non_positive_keepalive_expiry_is_refused(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("MCP_CLIENT_KEEPALIVE_EXPIRY_SECONDS", value)
    with pytest.raises(ValidationError, match="keepalive_expiry_seconds"):
        MCPClientSettings()


async def test_the_uds_client_carries_the_same_limits():
    client = HTTPUDSTransport(socket_path="/tmp/s.sock")._socket_factory()
    async with client:
        pool = _pool_of(client)
        assert pool._keepalive_expiry == mcp_http_limits().keepalive_expiry
        assert pool._max_connections == mcp_http_limits().max_connections
        assert pool._max_keepalive_connections == mcp_http_limits().max_keepalive_connections
