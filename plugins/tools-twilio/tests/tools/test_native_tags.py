"""Every Twilio tool declares its native ``twilio``/``provisioning`` tags at registration."""

from __future__ import annotations

import pytest

# Importing the tool modules runs their registration through the null app bound in
# conftest, which records the declared tags.
from tai42_tools_twilio.tools import (  # noqa: F401
    get_twilio_number_webhook,
    list_twilio_numbers,
    set_twilio_number_webhook,
)

_TWILIO_TOOLS = [
    "get_twilio_number_webhook",
    "list_twilio_numbers",
    "set_twilio_number_webhook",
]


@pytest.mark.parametrize("name", _TWILIO_TOOLS)
def test_twilio_tool_tagged(name: str, null_app) -> None:
    assert null_app.tools.tags[name] == {"twilio", "provisioning"}
