"""The MCP-listing half of a fake reload admin whose writes change no MCP selection.

The config service checks every write that changes an ``mcp`` entry's ``include`` or
``exclude`` against the server's listing through the admin's ``served_mcp_names``. A fake
admin for writes that never change a selection carries :func:`no_mcp_listing`, which
fails the test if the service lists a server anyway.
"""

from __future__ import annotations

from typing import Any


async def no_mcp_listing(config: Any) -> list[str]:
    """Fail loudly: the write under test was not expected to list ``config``'s server."""
    raise AssertionError(f"no write here changes an MCP selection, yet {config.title!r} was listed")
