"""The ``list_github_webhooks`` tool: list a repository's webhooks on GitHub.

Lists every webhook on ``owner/name`` through the shared REST client, following the ``Link`` header
to exhaustion, and returns each hook's id, delivery url, events and active flag. The GitHub REST
call lives in :mod:`tai42_tools_github._internal.tools.github_client`.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

from tai42_tools_github._internal.tools.github_client import list_webhooks


@tai42_app.tools.tool(tags={"github", "provisioning"})
async def list_github_webhooks(repo: str) -> list[dict[str, Any]]:
    """List a repository's webhooks, following Link-header pagination to exhaustion.

    GitHub never returns a hook's signing secret, so no returned dict carries one.

    Args:
        repo: The repository as ``"owner/name"``. Any other shape raises.

    Returns:
        One ``{"id": <hook id>, "url": <delivery url>, "events": [...], "active": <bool>}`` per hook.
    """
    return await list_webhooks(repo)
