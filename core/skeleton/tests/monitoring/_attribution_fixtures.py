"""Tools the run-attribution span test dispatches: one that answers and one that raises."""

from __future__ import annotations

from tai42_contract.app import tai42_app


@tai42_app.tools.tool
def attributed_leaf(city: str) -> dict:
    """Answer with the city it was given."""
    return {"city": city}


@tai42_app.tools.tool
def attributed_boom() -> dict:
    """Raise."""
    raise RuntimeError("boom")
