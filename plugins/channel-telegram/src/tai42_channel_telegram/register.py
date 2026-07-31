"""Self-register the Telegram channel, its inbound route, and the setWebhook hook.

Loaded when the manifest's ``channel_modules`` lists ``tai42_channel_telegram``.
Importing this module registers :class:`TelegramChannel` under ``"telegram"``,
imports :mod:`tai42_channel_telegram.inbound` (registering the
``POST /api/channels/telegram/inbound`` route), and hooks ``setWebhook`` at
startup. Importing the package ``__init__`` alone does NOT register (library use).
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

import tai42_channel_telegram.inbound  # noqa: F401  (import registers the inbound route)
from tai42_channel_telegram.channel import TelegramChannel
from tai42_channel_telegram.client import telegram_http
from tai42_channel_telegram.settings import require, require_secret, telegram_settings

tai42_app.channels.register("telegram", TelegramChannel())


@tai42_app.lifecycle.on_startup
async def _register_telegram_webhook() -> None:
    """Point the bot's webhook at this deployment's inbound door.

    Raises loudly on any failure — a manifest-named channel that cannot receive
    replies must abort startup. ``setWebhook`` is idempotent, and it disables
    ``getUpdates`` polling for this token (mutually exclusive by API design).
    """
    settings = telegram_settings()
    token = require_secret(settings.bot_token, "CHANNEL_TELEGRAM_BOT_TOKEN")
    secret = require_secret(settings.webhook_secret, "CHANNEL_TELEGRAM_WEBHOOK_SECRET")
    base = require(settings.public_base_url, "CHANNEL_TELEGRAM_PUBLIC_BASE_URL")
    # No default recipient AND an empty allowlist can never deliver — abort startup.
    if settings.default_recipient is None and not settings.allowed_recipients:
        raise ValueError(
            "the telegram channel is not configured: set CHANNEL_TELEGRAM_DEFAULT_RECIPIENT "
            "and/or CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS"
        )

    payload = {
        "url": f"{base.rstrip('/')}/api/channels/telegram/inbound",
        "secret_token": secret,
        # Only message updates matter (ForceReply answers arrive as messages).
        "allowed_updates": ["message"],
    }
    async with telegram_http() as client:
        response = await client.post(f"{settings.api_base_url}/bot{token}/setWebhook", json=payload)
    if response.status_code != 200:
        raise RuntimeError(f"telegram setWebhook returned HTTP {response.status_code}: {response.text[:200]}")
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(
            f"telegram setWebhook failed: error_code={data.get('error_code')} description={data.get('description')!r}"
        )
