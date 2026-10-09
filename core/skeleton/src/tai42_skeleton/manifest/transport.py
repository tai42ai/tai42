"""The transport an MCP entry's outbound request uses, decided by which transport field is set."""

from tai42_contract.manifest import MCPConfig

_FIELD_TO_TRANSPORT = {"command": "stdio", "url": "http", "uds": "uds"}


def detect_transport(inner: MCPConfig) -> str:
    """Return ``"stdio"`` | ``"http"`` | ``"uds"`` from the set transport field.

    The entry's free-text ``type`` decides nothing. Raises if no field or more than one is set.
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
    return _FIELD_TO_TRANSPORT[flags[0]]
