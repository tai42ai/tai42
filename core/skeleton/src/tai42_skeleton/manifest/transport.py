"""The transport an MCP entry's outbound request uses, decided by which transport field is set."""

from tai42_contract.manifest import MCP_TRANSPORT_BY_ADDRESS, MCPConfig, MCPTransport


def detect_transport(inner: MCPConfig) -> MCPTransport:
    """Return ``"stdio"`` | ``"http"`` | ``"uds"`` from the set transport field.

    The address field decides; a set ``type`` has already been checked to agree with it.
    Raises if no field or more than one is set.
    """
    flags = [
        k
        for k, v in (
            ("command", inner.command),
            ("url", inner.url),
            ("uds", inner.uds),
        )
        if v
    ]
    if not flags:
        raise RuntimeError("MCPConfig has no transport set (need command, url, or uds)")
    if len(flags) > 1:
        raise RuntimeError(f"MCPConfig has ambiguous transport: {flags!r}")
    return MCP_TRANSPORT_BY_ADDRESS[flags[0]]
