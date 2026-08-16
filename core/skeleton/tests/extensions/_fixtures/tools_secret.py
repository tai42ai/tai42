"""Fixture tool that both receives and returns a ``SecretValue``, for exercising
the ``monitor`` extension's masking: the span records the placeholder, never the
real value, on both its input and its output."""

from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.secrets import SecretValue


@tai42_app.tools.tool
def secret_roundtrip(payload: Any) -> dict:
    """Echo the payload back and add a fresh secret to the result."""
    return {"echo": payload, "token": SecretValue("tok-4242-xyzzy")}
