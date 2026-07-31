"""Telegram channel plugin.

A :class:`~tai42_contract.channels.Channel` that delivers ``ask_user`` questions
to a configured Telegram chat (ForceReply for typed text/select, a URL button to
the callback door for confirm/external) and bridges typed replies back through
its own webhook route. Importing this package does NOT register anything (library
use); the runtime imports :mod:`tai42_channel_telegram.register` to register the
``"telegram"`` channel, its inbound route, and the setWebhook startup hook.
"""

from tai42_channel_telegram.channel import TelegramChannel
from tai42_channel_telegram.settings import TelegramSettings, telegram_settings

__all__ = ["TelegramChannel", "TelegramSettings", "telegram_settings"]
