"""The beta fixture's probe tool.

Registers at import via ``@tai42_app.tools.tool``; returns a fixed marker payload
so a test can identify the beta distribution once it is installed and live."""

from __future__ import annotations

import os

from tai42_contract.app import tai42_app


@tai42_app.tools.tool
def e2e_market_beta_probe() -> dict:
    """Return a fixed marker payload identifying the beta fixture."""
    return {"marker": "e2e-market-beta", "pid": os.getpid()}
