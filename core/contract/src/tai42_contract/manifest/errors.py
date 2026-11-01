"""The error a read of a worker's MCP tool listing raises when the worker holds none.

A worker keeps, per manifest MCP title, the full tool listing its last bind of the server
consumed; the advertised-tools read (``TaiApp.admin.advertised_mcp_tools``) answers from it and
never starts a server. With no listing held for a title, :class:`McpListingUnavailableError`
names why, so a door can tell a start in progress, a failed start and a server the worker has
not loaded apart.
"""

from __future__ import annotations

from typing import Literal

# Why a worker holds no tool listing for an MCP server: its start is still in progress, its last
# start or bind failed, or the worker has not loaded the server.
McpListingUnavailableCause = Literal["starting", "failed", "not_loaded"]


class McpListingUnavailableError(Exception):
    """The answering worker holds no tool listing for the MCP server ``title``.

    ``cause`` says why (:data:`McpListingUnavailableCause`). The message is credential-free: it
    is the recorded cause of a start in progress or a failure, else that the worker has not
    loaded the server.
    """

    def __init__(self, title: str, cause: McpListingUnavailableCause, message: str) -> None:
        """Name the server ``title``, why it has no listing (``cause``) and the credential-free ``message``."""
        self.title = title
        self.cause: McpListingUnavailableCause = cause
        super().__init__(message)
