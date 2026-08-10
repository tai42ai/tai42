"""The ``create_github_webhook`` tool: provision a repository webhook on GitHub.

Creates a webhook on ``owner/name`` through the shared REST client and returns the created hook's
id, delivery url, events and active flag. The GitHub REST call and its version pin live in
:mod:`tai42_tools_github._internal.tools.github_client`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from tai42_contract.app import tai42_app

from tai42_tools_github._internal.tools.github_client import create_webhook


@tai42_app.tools.tool(tags={"github", "provisioning"})
async def create_github_webhook(repo: str, url: str, events: list[str], secret: str) -> dict[str, Any]:
    """Create a repository webhook and return its id, url, events and active flag.

    GitHub does not mint the signing secret: the caller supplies ``secret``, GitHub stores it, and
    the caller is responsible for keeping its own copy -- GitHub never returns it and this tool
    never echoes it. The returned dict carries nothing secret.

    Args:
        repo: The target repository as ``"owner/name"``. Any other shape raises.
        url: The ``https`` delivery URL GitHub POSTs events to. Required and non-empty.
        events: The event names to subscribe (e.g. ``["push"]``). Required and non-empty.
        secret: The webhook signing secret GitHub keys its ``X-Hub-Signature-256`` with. Required
            and non-empty; never returned or echoed into an error.

    Returns:
        ``{"id": <hook id>, "url": <delivery url>, "events": [...], "active": <bool>}``.
    """
    if not url:
        raise ValueError("url is required and must be non-empty")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"url must be an https URL with a host; got {url!r}")
    if not events:
        raise ValueError("events is required and must be non-empty")
    if not secret:
        raise ValueError("secret is required and must be non-empty")

    return await create_webhook(repo, url, events, secret)
