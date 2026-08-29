"""The Telegram correlation store — the contract :class:`CorrelationStore` port.

One Redis string key per delivered question::

    channel:telegram:corr:{chat_id}:{message_id} -> a JSON :class:`Correlation` record

The correlation key is the ForceReply anchor scoped by its chat:
``{chat_id}:{message_id}`` (see :func:`scoped_correlation_key`). A Telegram
``message_id`` is minted by ``sendMessage`` and is unique only PER CHAT — the SAME
integer is reused across different chats — so on a multi-recipient bot a reply
carrying ``message_id=N`` in chat B would collide with a pending ask anchored on
``message_id=N`` in chat A if the key were the bare id. Scoping by the chat id the
message belongs to keeps each chat's anchors in their own namespace: the writer
passes the chat it delivered to, the reader derives it from the incoming update's
chat, so a cross-chat answer can never resolve another chat's ask. The value is the
delivery's :class:`~tai42_contract.channels.Correlation` (callback_url +
interaction_id + ttl_deadline). Written by ``TelegramChannel.deliver`` after
``sendMessage`` returns, read by the shared inbound-answer ladder to route the
ForceReply answer, expired by Redis at the question's deadline (TTL = remaining
budget). Connection from :class:`TelegramCorrelationSettings`
(``CHANNEL_TELEGRAM_REDIS_URL``).

The reservation is ``SET NX``: one pending ask per ``{chat_id}:{message_id}`` anchor,
so a delivery never silently overwrites a live reservation on the same anchor. The NX
makes the one-pending guarantee explicit at the port.

The chat-scoped keyspace is a clean break from the earlier bare-``{message_id}`` keys:
those records are short-TTL ephemeral correlations, so no migration is offered — any
in-flight bare-key reservation simply expires (its reply bridges as a fresh turn), and
new sends write only the scoped shape.
"""

from __future__ import annotations

import json
import logging
from typing import cast

from tai42_contract.app import tai42_app
from tai42_contract.channels import Correlation
from tai42_kit.clients.impl.redis import RedisClient

from tai42_channel_telegram.settings import TelegramCorrelationSettings, telegram_correlation_settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "channel:telegram:corr:"
# A parallel per-anchor record holding the tappable option list for a message that
# carries an inline keyboard (a select / suggested-reply ask, or a notify with
# options). The contract :class:`Correlation` has no options field, so the index a
# button tap carries in its ``callback_data`` is mapped back to the exact option text
# through this side record — keyed by the SAME chat-scoped anchor the callback query
# reports (a Telegram ``message_id`` is unique only per chat, so the chat id scopes it
# exactly as the correlation key). It is independent of the correlation record (a
# notify carries options but no correlation), so it is set and read on its own key.
_OPTIONS_KEY_PREFIX = "channel:telegram:opts:"


def scoped_correlation_key(chat_id: str, message_id: str) -> str:
    """The chat-scoped correlation key for a message: ``{chat_id}:{message_id}``.

    A Telegram ``message_id`` is unique only PER CHAT, so the chat the message belongs
    to must scope it — otherwise a reply/tap carrying ``message_id=N`` in one chat could
    resolve a pending ask anchored on ``message_id=N`` in another. The writer passes the
    chat it delivered to; the reader passes the incoming update's chat.
    """
    return f"{chat_id}:{message_id}"


def _key(correlation_key: str) -> str:
    return f"{_KEY_PREFIX}{correlation_key}"


def _options_key(chat_id: str, message_id: str) -> str:
    return f"{_OPTIONS_KEY_PREFIX}{scoped_correlation_key(chat_id, message_id)}"


async def set_options(chat_id: str, message_id: str, options: list[str], *, ttl_seconds: int) -> None:
    """Store the tappable ``options`` for an inline-keyboard anchor under its
    chat-scoped ``{chat_id}:{message_id}`` with a ``ttl_seconds`` expiry.

    A button tap reports only its ``callback_data`` (the option's index) and the
    anchor ``message_id`` within its chat; this record resolves that index back to the
    exact option text. Scoping by ``chat_id`` matches the correlation key: a Telegram
    ``message_id`` is unique only per chat. ``ttl_seconds`` is the ask's remaining
    budget (a select/suggested-reply ask) or the operator-set notify tap window, and
    must be positive so the record always carries an expiry.
    """
    if ttl_seconds <= 0:
        raise ValueError(f"options record TTL must be positive, got {ttl_seconds}")
    async with _redis_ctx() as r:
        await r.set(_options_key(chat_id, message_id), json.dumps(options), ex=ttl_seconds)


async def get_options(chat_id: str, message_id: str) -> list[str] | None:
    """The tappable option list stored for ``{chat_id}:{message_id}``, or ``None``
    (unknown / expired / a message that carried no options).

    A non-destructive peek: a resolved tap leaves the record to expire (its anchor is
    single-use and the correlation record is the source of truth the ladder releases),
    so a redelivered callback still resolves the same text rather than erroring.
    """
    async with _redis_ctx() as r:
        # decode_responses=True on this connection, so a hit is always ``str``.
        raw = cast("str | None", await r.get(_options_key(chat_id, message_id)))
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        logger.warning(
            "telegram: options record for message %r in chat %r is not valid JSON; treating as no options",
            message_id,
            chat_id,
        )
        return None
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        logger.warning(
            "telegram: options record for message %r in chat %r is not a list of strings; ignoring",
            message_id,
            chat_id,
        )
        return None
    return decoded


def _redis_settings() -> TelegramCorrelationSettings:
    """The correlation-store connection, raising a clear config error when unset."""
    settings = telegram_correlation_settings()
    if not settings.redis_url:
        raise ValueError("Telegram channel correlation store is not configured: set CHANNEL_TELEGRAM_REDIS_URL.")
    return settings


def _redis_ctx():
    return tai42_app.clients.client_ctx(RedisClient, _redis_settings())


class TelegramCorrelationStore:
    """Satisfies :class:`~tai42_contract.channels.CorrelationStore` over the
    plugin-owned ``channel:telegram:corr:{chat_id}:{message_id}`` string keys (the
    ``key`` argument is the chat-scoped :func:`scoped_correlation_key`).

    Stateless — the Redis connection is read at each call so a live-reload picks up a
    rotated URL with no stale per-instance snapshot."""

    async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
        """Reserve ``key`` for ``entry`` NX with a ``ttl_seconds`` expiry.

        ``key`` is the chat-scoped ``{chat_id}:{message_id}`` anchor. Returns True when
        the anchor was free and is now held; False when it is already held.
        ``ttl_seconds`` (the question's remaining budget) must be positive — a
        non-positive TTL would mint a key without an expiry.
        """
        if ttl_seconds <= 0:
            raise ValueError(f"correlation TTL must be positive, got {ttl_seconds}")
        async with _redis_ctx() as r:
            stored = await r.set(_key(key), entry.model_dump_json(), nx=True, ex=ttl_seconds)
        return bool(stored)

    async def get_correlation(self, key: str) -> Correlation | None:
        """The pending question's record under ``key``, or ``None`` (unknown/expired).

        A non-destructive peek — it neither drops nor refreshes the reservation."""
        async with _redis_ctx() as r:
            # decode_responses=True on this connection, so a hit is always ``str``.
            raw = cast("str | None", await r.get(_key(key)))
        if raw is None:
            return None
        try:
            return Correlation.model_validate_json(raw)
        except ValueError:
            # A record written by the pre-migration code (a bare callback URL string, not
            # a JSON Correlation) does not parse. Tolerate it as a graceful miss so the
            # reply bridges, never a 500; never log the value (it is a callback URL).
            logger.warning(
                "telegram: correlation for key %r is not the current Correlation shape "
                "(pre-migration bare-URL record?); treating as no correlation",
                key,
            )
            return None

    async def release_correlation(self, key: str) -> None:
        """Drop any reservation under ``key``, idempotently (a no-op when free)."""
        async with _redis_ctx() as r:
            await r.delete(_key(key))


# Module-level singleton: the store is stateless, so one instance serves the deliver
# and inbound paths (the channel hands it to the shared ladder explicitly).
telegram_correlation_store = TelegramCorrelationStore()
