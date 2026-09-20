"""The ``list_whatsapp_templates`` tool: list every message template on the account.

Reads all message templates from the configured WhatsApp Business Account through the Graph client,
following Graph's paging cursors to exhaustion. The Graph call and the pinned API base live in
:mod:`tai42_tools_whatsapp._internal.tools.whatsapp_client`.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

from tai42_tools_whatsapp._internal.tools.whatsapp_client import list_templates


@tai42_app.tools.tool(tags={"whatsapp", "provisioning"})
async def list_whatsapp_templates() -> list[dict[str, Any]]:
    """List every WhatsApp message template on the account, following Graph's paging to exhaustion.

    Returns:
        The concatenated ``data`` entries across every page Graph returns.
    """
    return await list_templates()
