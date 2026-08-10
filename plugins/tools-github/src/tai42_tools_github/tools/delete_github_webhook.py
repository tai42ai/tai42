"""The ``delete_github_webhook`` tool: remove a repository webhook on GitHub.

Deletes the webhook ``hook_id`` from ``owner/name`` through the shared REST client. The GitHub REST
call lives in :mod:`tai42_tools_github._internal.tools.github_client`.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app

from tai42_tools_github._internal.tools.github_client import delete_webhook


@tai42_app.tools.tool(tags={"github", "provisioning"})
async def delete_github_webhook(repo: str, hook_id: int) -> dict[str, Any]:
    """Delete a repository webhook by id.

    Succeeds only on GitHub's ``204``; an unknown hook (404) or any other non-2xx raises, never a
    silent success.

    Args:
        repo: The repository as ``"owner/name"``. Any other shape raises.
        hook_id: The webhook id to delete. Must be a positive integer.

    Returns:
        ``{"repo": <repo>, "hook_id": <hook id>, "deleted": True}``.
    """
    # ``bool`` is an ``int`` subclass; exclude it so ``True`` cannot pass as an id.
    if isinstance(hook_id, bool) or not isinstance(hook_id, int) or hook_id <= 0:
        raise ValueError(f"hook_id must be a positive integer; got {hook_id!r}")

    return await delete_webhook(repo, hook_id)
