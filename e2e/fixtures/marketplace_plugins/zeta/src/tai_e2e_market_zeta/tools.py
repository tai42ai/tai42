"""The zeta fixture's probe tool.

Registers at import via ``@tai42_app.tools.tool`` so the skeleton installer's
manifest patch + reload makes it live in the serving process. The reported
``dist_version`` reads the installed distribution's metadata, so a test proves
exactly which wheel is live in the process that answered. Zeta is the
plugin-compat fixture: its wheels are forged per spec with a stamped contract
range, so the same tool proves which COMPAT posture the live wheel declares."""

from __future__ import annotations

import importlib.metadata
import os

from tai42_contract.app import tai42_app


@tai42_app.tools.tool
def e2e_zeta_probe() -> dict:
    """Report the installed distribution version and the serving pid."""
    return {
        "dist_version": importlib.metadata.version("tai-e2e-market-zeta"),
        "pid": os.getpid(),
    }
