"""The failure dump masks every secret-shaped setting of a stack's environment."""

from __future__ import annotations

import pytest

from tai42_e2e.diagnostics import _mask


@pytest.mark.parametrize(
    "key",
    [
        "CHANNEL_WHATSAPP_FLOW_PRIVATE_KEY",
        "CHANNEL_WHATSAPP_ACCESS_TOKEN",
        "CHANNEL_WHATSAPP_APP_SECRET",
        "TAI_DB_PASSWORD",
    ],
)
def test_a_secret_shaped_setting_is_masked(key: str) -> None:
    assert _mask(key, "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----") == "***"


def test_a_plain_setting_is_shown() -> None:
    assert _mask("CHANNEL_WHATSAPP_WABA_ID", "waba000111") == "waba000111"
