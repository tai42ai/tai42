"""The requiring fixture's probe tool, registered at import via ``@tai42_app.tools.tool``."""

from __future__ import annotations

from tai42_contract.app import tai42_app


@tai42_app.tools.tool
def e2e_requiresprobe_ping() -> str:
    """Answer a fixed reply."""
    return "requiresprobe-pong"
