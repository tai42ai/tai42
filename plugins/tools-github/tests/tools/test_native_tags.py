"""Every GitHub tool declares its native ``github``/``provisioning`` tags at registration."""

from __future__ import annotations

import pytest

# Importing the tool modules runs their registration through the null app bound in
# conftest, which records the declared tags.
from tai42_tools_github.tools import (  # noqa: F401
    create_github_webhook,
    delete_github_webhook,
    list_github_webhooks,
)

_GITHUB_TOOLS = [
    "create_github_webhook",
    "delete_github_webhook",
    "list_github_webhooks",
]


@pytest.mark.parametrize("name", _GITHUB_TOOLS)
def test_github_tool_tagged(name: str, null_app) -> None:
    assert null_app.tools.tags[name] == {"github", "provisioning"}
