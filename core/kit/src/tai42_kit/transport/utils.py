"""Build the MCP transport for one server config."""

from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.client.transports.sse import SSETransport
from fastmcp.client.transports.stdio import StdioTransport
from fastmcp.mcp_config import infer_transport_type_from_url
from tai42_contract.manifest import TaiMCPConfig

from tai42_kit.transport.http_uds_transport import HTTPUDSTransport
from tai42_kit.transport.mcp_http import mcp_http_client
from tai42_kit.transport.sse_uds_transport import SSEUDSTransport
from tai42_kit.transport.stdio_launch import resolve_stdio_command, stdio_child_env, stdio_child_stderr


def get_mcp_transport(
    config: TaiMCPConfig, *, uds_protocol: str = "http"
) -> HTTPUDSTransport | SSEUDSTransport | StreamableHttpTransport | SSETransport | StdioTransport:
    """Build the fastmcp transport for one MCP server config.

    A UDS server connects over a unix socket; ``uds_protocol`` selects the MCP
    wire protocol carried on it (``http`` = streamable-http, ``sse`` = SSE) — the
    caller (which owns the runtime/CLI) supplies it, so it arrives unvalidated and
    any other value raises. A URL server gets fastmcp's SSE or streamable-http
    transport, chosen by fastmcp's own URL inference, carrying only the config's
    headers and built on :func:`mcp_http_client`; it ignores ``uds_protocol``. A
    command server gets fastmcp's stdio transport running the executable
    :func:`~tai42_kit.transport.stdio_launch.resolve_stdio_command` finds for it (a bare
    name with no executable match raises) with the config's ``args``, and an ``env`` that
    is the config's, carrying the import directories of the install location the command
    was found in, when that location declares any
    (:func:`~tai42_kit.transport.stdio_launch.stdio_child_env`); the child's stderr is
    this process's standard error descriptor
    (:func:`~tai42_kit.transport.stdio_launch.stdio_child_stderr`). A config that sets
    none of ``uds``, ``url`` and ``command`` raises :class:`ValueError` naming the server.
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
    command = config.config.command
    if not command:
        raise ValueError(f"MCP server {config.title!r} declares none of url, uds or command")
    resolved = resolve_stdio_command(command, config.config.env)
    return StdioTransport(
        command=resolved.executable,
        args=list(config.config.args),
        env=stdio_child_env(config.config.env, resolved.import_dirs),
        log_file=stdio_child_stderr(),
    )
