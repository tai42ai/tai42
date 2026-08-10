"""The ``delete_whatsapp_template`` tool: delete a message template by name.

Deletes a template on the configured WhatsApp Business Account through the Graph client and returns
Graph's response. The Graph call and the pinned API base live in
:mod:`tai42_tools_whatsapp._internal.tools.whatsapp_client`.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

from tai42_tools_whatsapp._internal.tools.whatsapp_client import delete_template


@tai42_app.tools.tool(tags={"whatsapp", "provisioning"})
async def delete_whatsapp_template(name: str) -> dict[str, Any]:
    """Delete a WhatsApp message template by name and return Graph's response.

    Args:
        name: The template name to delete. Required and non-empty; percent-encoded into the query.

    Returns:
        Graph's response JSON.
    """
    if not name:
        raise ValueError("name is required and must be non-empty")
    return await delete_template(name)
