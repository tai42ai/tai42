"""Every WhatsApp tool declares its native ``whatsapp``/``provisioning`` tags at registration."""

from __future__ import annotations

import pytest

# Importing the tool modules runs their registration through the null app bound in conftest, which
# records the declared tags.
from tai42_tools_whatsapp.tools import (  # noqa: F401
    delete_whatsapp_template,
    list_whatsapp_templates,
    register_whatsapp_template,
    subscribe_whatsapp_app,
)

_WHATSAPP_TOOLS = [
    "delete_whatsapp_template",
    "list_whatsapp_templates",
    "register_whatsapp_template",
    "subscribe_whatsapp_app",
]


@pytest.mark.parametrize("name", _WHATSAPP_TOOLS)
def test_whatsapp_tool_tagged(name: str, null_app) -> None:
    assert null_app.tools.tags[name] == {"whatsapp", "provisioning"}
