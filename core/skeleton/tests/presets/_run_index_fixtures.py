"""Base tools for the runs-index chokepoint integration test.

A ``leaf`` plain tool and a ``redispatch`` tool whose body re-enters ``run_tool`` on
another registered preset — the nested-dispatch shape the runs-index outermost guard
is proven against. Kept in its own module so no single module is imported by two
manifest sections (the preset-engine convention).
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app


@tai42_app.tools.tool
def leaf(city: str) -> dict:
    """A plain leaf tool a preset can bake over."""
    return {"city": city}


@tai42_app.tools.tool
async def redispatch(target: str) -> Any:
    """Dispatch another registered preset by name from inside a tool body — the nested
    ``run_tool`` re-entry an OUTER preset drives an INNER preset through."""
    return await tai42_app.tools.run_tool(target, {})
