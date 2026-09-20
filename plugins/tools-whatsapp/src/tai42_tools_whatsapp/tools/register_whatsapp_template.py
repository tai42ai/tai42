"""The ``register_whatsapp_template`` tool: register a WhatsApp message template.

Registers a template on the configured WhatsApp Business Account through the Graph client and
returns Graph's response (the new template's id and review status). The Graph call and the pinned
API base live in :mod:`tai42_tools_whatsapp._internal.tools.whatsapp_client`.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

from tai42_tools_whatsapp._internal.tools.whatsapp_client import register_template


@tai42_app.tools.tool(tags={"whatsapp", "provisioning"})
async def register_whatsapp_template(
    name: str,
    language: str,
    category: str,
    components: list[Any],
) -> dict[str, Any]:
    """Register a WhatsApp message template and return Graph's response (template id and status).

    ``components`` is the Graph-shaped component list (body, header, buttons, ...) passed through
    unchanged; Graph validates its shape and rejects a malformed one with its own error. All four
    parameters are required and non-empty.

    Args:
        name: The template name (unique on the account). Required and non-empty.
        language: The template's BCP-47 language/locale code (e.g. ``"en_US"``). Required and
            non-empty.
        category: Graph's template category (e.g. ``"MARKETING"``, ``"UTILITY"``,
            ``"AUTHENTICATION"``). Required and non-empty.
        components: The Graph-shaped list of template components. Required and non-empty.

    Returns:
        Graph's response JSON — the created template's id and review status.
    """
    if not name:
        raise ValueError("name is required and must be non-empty")
    if not language:
        raise ValueError("language is required and must be non-empty")
    if not category:
        raise ValueError("category is required and must be non-empty")
    if not components:
        raise ValueError("components is required and must be non-empty")
    return await register_template(name, language, category, components)
