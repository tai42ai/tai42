"""The gamma fixture's probe tool.

Registers at import via ``@tai42_app.tools.tool``; returns a fixed marker payload
so a test can confirm the gamma distribution is live once installed."""

from __future__ import annotations

import os

from tai42_contract.app import tai42_app


@tai42_app.tools.tool
def e2e_market_gamma_probe() -> dict:
    """Return a fixed marker payload identifying the gamma fixture."""
    return {"marker": "e2e-market-gamma", "pid": os.getpid()}
