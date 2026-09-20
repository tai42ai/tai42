"""The delta fixture's probe tool.

Registers at import via ``@tai42_app.tools.tool``; returns a fixed marker payload
so a test can confirm the delta distribution is live once installed. Delta is
the github-sourced fixture, ingested from a source tarball rather than a wheel."""

from __future__ import annotations

import os

from tai42_contract.app import tai42_app


@tai42_app.tools.tool
def e2e_market_delta_probe() -> dict:
    """Return a fixed marker payload identifying the delta fixture."""
    return {"marker": "e2e-market-delta", "pid": os.getpid()}
