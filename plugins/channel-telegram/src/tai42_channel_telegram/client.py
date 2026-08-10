"""Outbound HTTP for the Telegram channel.

One pooled ``httpx.AsyncClient`` (the kit's ``HttpxClient`` via
``tai42_app.clients.client_ctx``) serves every outbound call: ``sendMessage`` /
``setWebhook`` and the loopback answer forward. Pooled per event loop + timeout;
``trust_env=False`` ignores ambient proxy env vars.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

import httpx
from tai42_contract.app import tai42_app
from tai42_kit.clients.impl.http import HttpxClient

from tai42_channel_telegram.settings import telegram_settings


def telegram_http() -> AbstractAsyncContextManager[httpx.AsyncClient]:
    """A pooled outbound client budgeted by ``CHANNEL_TELEGRAM_HTTP_TIMEOUT_SECONDS``."""
    return tai42_app.clients.client_ctx(HttpxClient, timeout=telegram_settings().http_timeout_seconds)
