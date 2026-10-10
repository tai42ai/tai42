"""The error kinds of the transports the kit's clients own, and which of them are platform transport failures.

Imported by the MCP client module (:mod:`tai42_kit.clients.impl.mcp`), the transport every
tool dispatch leases through, so the registrations hold wherever that transport can raise.
An ``McpError`` carries its meaning in its code, not its class, so it is stamped per instance
at the pooled-client lease (:func:`stamp_mcp_error_kind`).
"""

from __future__ import annotations

from typing import Final

import anyio
import httpx
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, METHOD_NOT_FOUND, URL_ELICITATION_REQUIRED
from tai42_contract.errors import ERROR_KIND_ATTR, ERROR_TRANSPORT_ATTR, ErrorKind, register_error_kind

# The timeout subclass is the most-derived registration, so it wins the MRO walk.
register_error_kind(httpx.TimeoutException, ErrorKind.TIMED_OUT, transport=True)
register_error_kind(httpx.TransportError, ErrorKind.UNAVAILABLE, transport=True)
register_error_kind(anyio.ClosedResourceError, ErrorKind.UNAVAILABLE, transport=True)
register_error_kind(anyio.BrokenResourceError, ErrorKind.UNAVAILABLE, transport=True)

_REQUEST_TIMEOUT: Final = 408
"""The code the MCP client session raises when its own read of a response times out."""

_MCP_CODE_KINDS: Final[dict[int, ErrorKind]] = {
    _REQUEST_TIMEOUT: ErrorKind.TIMED_OUT,
    INVALID_PARAMS: ErrorKind.BAD_INPUT,
    METHOD_NOT_FOUND: ErrorKind.NOT_FOUND,
    URL_ELICITATION_REQUIRED: ErrorKind.UNAUTHORIZED,
}
_MCP_TRANSPORT_CODES: Final[frozenset[int]] = frozenset({_REQUEST_TIMEOUT})


def stamp_mcp_error_kind(exc: BaseException) -> None:
    """Stamp an ``McpError`` instance with the kind its code means.

    An unmapped code is ``UPSTREAM_ERROR``; the client's own read timeout also carries the
    transport marker. An instance that already carries a kind stamp, and any other exception,
    is left as it is.
    """
    if isinstance(exc, McpError) and getattr(exc, ERROR_KIND_ATTR, None) is None:
        setattr(exc, ERROR_KIND_ATTR, _MCP_CODE_KINDS.get(exc.error.code, ErrorKind.UPSTREAM_ERROR))
        setattr(exc, ERROR_TRANSPORT_ATTR, exc.error.code in _MCP_TRANSPORT_CODES)


__all__ = ["stamp_mcp_error_kind"]
