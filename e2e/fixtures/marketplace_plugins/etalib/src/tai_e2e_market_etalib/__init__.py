"""The eta fixture server's one tool, shipped as a library the eta package depends on.

It is a plain dependency, never a plugin: installing eta pulls it into eta's own
server environment, so a test proves that environment resolves eta's dependency
closure while neither the plugin layer nor the image holds it."""

from __future__ import annotations

import os


def ping() -> dict[str, object]:
    """Return a fixed marker payload identifying the eta fixture MCP server."""
    return {"eta": "pong", "pid": os.getpid()}
