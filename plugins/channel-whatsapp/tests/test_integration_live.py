"""Live integration tests against the real WhatsApp API (outbound only).

Run with ``pytest -m integration``. Reads the operator's
``CHANNEL_WHATSAPP_*`` credentials from the environment and skips cleanly
when any is unset. Only the outbound send is live; the correlation store stays the
in-memory fake, so ``deliver()`` proves the real Cloud API accept while writing no
external state.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import reset_all_settings

from tai42_channel_whatsapp import WhatsAppChannel
from tai42_channel_whatsapp.client import send_message
from tai42_channel_whatsapp.settings import whatsapp_settings

from .conftest import FakeRedis, make_delivery

_ENV_KEYS = (
    "CHANNEL_WHATSAPP_ACCESS_TOKEN",
    "CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID",
    "CHANNEL_WHATSAPP_ALLOWED_RECIPIENTS",
)


def _missing_creds() -> bool:
    return not all(os.environ.get(key) for key in _ENV_KEYS)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_missing_creds(), reason="CHANNEL_WHATSAPP_* not set"),
]


class _LiveHttpx:
    """One-shot real ``httpx.AsyncClient`` per call — the live stand-in for the pooled client."""

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(trust_env=False, timeout=30.0) as client:
            return await client.post(url, **kwargs)


@pytest.fixture(autouse=True)
def live_clients(stub_app):
    # The operator's real env must win over any cached test settings, and the
    # send path must reach the real API (the correlation store stays in-memory).
    reset_all_settings()
    stub_app.clients.by_class[HttpxClient] = _LiveHttpx()
    stub_app.clients.by_class[RedisClient] = FakeRedis()
    yield
    reset_all_settings()


async def test_send_message_returns_a_wamid():
    settings = whatsapp_settings()
    assert settings.default_phone_number_id is not None
    recipient = settings.allowed_recipients[0]

    wamid = await send_message(
        phone_number_id=settings.default_phone_number_id,
        to=recipient,
        body="tai42-channel-whatsapp live smoke: send_message",
    )

    assert wamid.startswith("wamid.")


async def test_full_deliver_reaches_the_configured_human(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_WHATSAPP_REDIS_URL", "redis://in-memory-fake/0")
    reset_all_settings()

    settings = whatsapp_settings()
    await WhatsAppChannel().deliver(
        make_delivery(
            recipient=settings.allowed_recipients[0],
            question="tai42-channel-whatsapp live smoke: full deliver — reply is not expected.",
        )
    )
