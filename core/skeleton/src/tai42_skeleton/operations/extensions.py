"""Extension operations — the authed catalog read for the UI's picker.

``list_extensions`` returns the flat ``{"name", "kind", "multiple", "config_schema"}``
list of every registered extension (kind carried as its lowercase enum value;
``multiple`` says whether one tool may stack several extensions of that kind, so a
picker single-selects a non-stackable kind; ``config_schema`` is the JSON Schema of
the settings the extension binds as ``config``, ``None`` when it takes none). A thin
skin over the ``tai42_app.extensions`` facet — no extension logic lives here.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

from tai42_skeleton.operations import operation
from tai42_skeleton.operations.response_models.extensions import ExtensionListing


@operation(summary="List every registered extension", tags=["extensions"], response_model=ExtensionListing)
async def list_extensions() -> list[dict[str, Any]]:
    """Return the flat ``{"name", "kind", "multiple", "config_schema"}`` list of every registered extension."""
    return tai42_app.extensions.available_extensions()
