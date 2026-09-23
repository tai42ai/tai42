"""A deliver-and-notify channel that CAPTURES the form a ``notify_user`` send carries.

Registered SUT-side on import via a manifest ``channel_modules`` entry. It advertises
``supports_form_notifications`` so ``notify_user`` accepts a ``schema`` + ``data`` + ``pages``
send on it, and its ``notify`` RECORDS the delivered form (its prefilled values and pages) onto
``e2e:rec:notify_form:{recipient}`` — so a spec proves the delivered form carries the values and
pages the send named, without a real bot medium. A send that fails validation (a bad prefill / a
bad page) is refused before ``notify`` runs, so nothing is recorded.
"""

from __future__ import annotations

import json

from pydantic_settings import SettingsConfigDict
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDelivery, ChannelNotification
from tai42_kit.clients import RedisConnectionSettings

STUB_FORM_CHANNEL_NAME = "stub_form"


class _ProbeRedisSettings(RedisConnectionSettings):
    """Points the capture client at the harness probe channel via ``E2E_PROBE_REDIS_URL`` — inlined
    so importing this channel module never pulls in the probe TOOLS package (whose tool
    registrations are not in this stack's manifest)."""

    model_config = SettingsConfigDict(env_prefix="E2E_PROBE_")


class _StubFormChannel:
    """Satisfies the ``Channel`` protocol and captures a delivered form notification."""

    supports_form_notifications = True

    async def deliver(self, delivery: ChannelDelivery) -> None:
        return None

    async def notify(self, notification: ChannelNotification) -> list[str]:
        from collections.abc import Awaitable
        from typing import cast

        from tai42_kit.clients import client_ctx
        from tai42_kit.clients.impl.redis import RedisClient

        record = json.dumps(
            {
                "message": notification.message,
                "schema": notification.schema,
                "data": notification.data.model_dump(mode="json") if notification.data is not None else None,
                "pages": [p.model_dump(mode="json") for p in notification.pages] if notification.pages else None,
            }
        )
        key = f"e2e:rec:notify_form:{notification.recipient}"
        async with client_ctx(RedisClient, _ProbeRedisSettings()) as client:
            await cast(Awaitable[int], client.rpush(key, record))
        return ["stub-form-msg-1"]


tai42_app.channels.register(STUB_FORM_CHANNEL_NAME, _StubFormChannel())
