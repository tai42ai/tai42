"""Fixture tool module declared under postponed annotations: ``keep``'s hints are the
strings ``"dict[str, Any]"``, which only this module's globals can resolve."""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app


@tai42_app.tools.tool
def keep(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the payload."""
    return payload
