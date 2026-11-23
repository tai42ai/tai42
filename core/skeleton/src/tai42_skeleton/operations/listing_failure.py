"""The operation error an MCP server's tool listing answers with when it cannot be read."""

from __future__ import annotations

from tai42_contract.errors import OperationError, UnavailableError
from tai42_contract.manifest.errors import McpListingUnavailableError

from tai42_skeleton.operations.errors import UpstreamError
from tai42_skeleton.tools.mcp_health import redact_probe_error

# The ``code`` of the retriable 503 a listing answers while the server's start is still in progress.
MCP_SERVER_STARTING_CODE = "mcp-server-starting"
# The ``code`` of the 503 a listing answers while this worker has not loaded the server.
MCP_SERVER_NOT_LOADED_CODE = "mcp-server-not-loaded"


def listing_failure(context: str, exc: Exception) -> OperationError:
    """The error of a listing read that raised ``exc``: ``context``, then the cause.

    With no listing held (:class:`McpListingUnavailableError`), its cause decides: a start in
    progress is a retriable 503 whose body carries ``code`` :data:`MCP_SERVER_STARTING_CODE`; a
    failed start or bind is the upstream's 502 with the recorded (credential-free) cause; a
    server the worker has not loaded is a 503 whose body carries ``code``
    :data:`MCP_SERVER_NOT_LOADED_CODE`. Any other failure is the upstream's 502 with its
    redacted cause.
    """
    if isinstance(exc, McpListingUnavailableError):
        message = f"{context}: {exc}"
        if exc.cause == "not_loaded":
            return UnavailableError(message, extra={"code": MCP_SERVER_NOT_LOADED_CODE})
        if exc.cause == "starting":
            return UnavailableError(message, extra={"code": MCP_SERVER_STARTING_CODE})
        return UpstreamError(message)
    return UpstreamError(f"{context}: {redact_probe_error(str(exc))}")
