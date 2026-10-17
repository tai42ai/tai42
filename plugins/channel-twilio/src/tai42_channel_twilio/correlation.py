"""Pending-question correlation store (plugin-owned Redis) — the ``CorrelationStore`` contract port.

Implements the :class:`~tai42_contract.channels.CorrelationStore` port.

SMS has no threading, so the only correlation key is the number pair
(<Twilio number>, <human number>), collapsed to the opaque ``"{twilio}:{human}"``
string the port never interprets. AT MOST ONE pending question per pair — an atomic
``SET NX`` reservation (:meth:`TwilioCorrelationStore.set_correlation`); a second
concurrent question is refused (the deliver path raises
``PendingQuestionExistsError``). The value is the delivery's
:class:`~tai42_contract.channels.Correlation` (callback_url + interaction_id +
ttl_deadline); the key TTL is the remaining answer budget, so an expired question
cannot capture a later reply.

The shared inbound-answer ladder reads the record with a NON-destructive peek
(:meth:`get_correlation` -> ``GET``, no ``DEL``) and drops it only on a terminal
outcome (:meth:`release_correlation` -> ``DEL``, idempotent): a kept (retry-in-place)
rejection leaves the record untouched. When the platform takes a delivered question down,
:func:`release_pending_for` drops the pair's record atomically ONLY while it is still held for
that question's interaction id, so a newer ask's hold on the same pair is never released. Upstream ``MessageSid`` dedupe
(``already_seen``/``mark_seen``) is the replay guard that keeps a redelivered webhook
from re-forwarding a peeked-but-not-released reply.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Final, cast

from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError, Correlation
from tai42_contract.interactions.models import AnswerMismatchPolicy, Wording
from tai42_kit.clients.impl.redis import RedisClient

from tai42_channel_twilio.settings import TwilioRedisSettings, twilio_redis_settings, twilio_settings


class PendingQuestionExistsError(ChannelDeliveryError):
    """A question is already pending for this number pair (one at a time)."""


def correlation_key(twilio_number: str, human_number: str) -> str:
    """The opaque correlation key for a number pair — the store's ``key``."""
    return f"{twilio_number}:{human_number}"


def _pending_key(key: str) -> str:
    return f"channel:twilio:pending:{key}"


def _seen_key(message_sid: str) -> str:
    return f"channel:twilio:seen:{message_sid}"


def _redis_settings() -> TwilioRedisSettings:
    """The correlation-store connection, raising a clear config error when unset."""
    settings = twilio_redis_settings()
    if not settings.redis_url:
        raise ValueError("Twilio channel correlation store is not configured: set CHANNEL_TWILIO_REDIS_URL.")
    return settings


def _remaining_seconds(timeout_at: datetime) -> int:
    """Whole seconds until the deadline, raising when it already passed."""
    remaining = math.ceil((timeout_at - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        raise ChannelDeliveryError(f"question deadline {timeout_at.isoformat()} has already passed")
    return remaining


class TwilioCorrelationStore:
    """Satisfies :class:`~tai42_contract.channels.CorrelationStore` over the plugin-owned keys.

    Backed by the ``channel:twilio:pending:{twilio}:{human}`` keys.
    """

    async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
        """Reserve ``key`` for ``entry`` NX with a ``ttl_seconds`` expiry.

        Returns True when the pair was free and is now held; False when a question is
        already pending for the pair — the one-pending-per-pair guarantee.
        """
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            stored = await redis.set(_pending_key(key), entry.model_dump_json(), nx=True, ex=ttl_seconds)
        return bool(stored)

    async def get_correlation(self, key: str) -> Correlation | None:
        """The pending question's record under ``key``, or ``None``.

        A non-destructive ``GET`` peek: it neither claims nor refreshes the pair, so
        the ladder can decide the outcome before releasing.
        """
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            raw = cast("str | bytes | None", await redis.get(_pending_key(key)))
        if raw is None:
            return None
        return Correlation.model_validate_json(raw)

    async def release_correlation(self, key: str) -> None:
        """Drop any reservation under ``key``, idempotently (a no-op when free)."""
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            await redis.delete(_pending_key(key))


# Module-level singleton: the store is stateless, so one instance serves the deliver
# and inbound paths (the channel hands it to the shared ladder explicitly).
twilio_correlation_store = TwilioCorrelationStore()


async def reserve_pending(
    twilio_number: str,
    human_number: str,
    callback_url: str,
    interaction_id: str,
    timeout_at: datetime,
    *,
    on_mismatch: AnswerMismatchPolicy,
    wording: Wording | None,
) -> None:
    """Atomically reserve the pair for one question, or raise ``PendingQuestionExistsError``.

    The deliver path's front door onto :meth:`TwilioCorrelationStore.set_correlation`:
    it computes the remaining-budget TTL (raising ``ChannelDeliveryError`` on an
    already-spent deadline), builds the :class:`Correlation` record, and turns the
    NX-refused case into the typed one-pending error. The ask's ``on_mismatch``
    digression policy and its per-send ``wording`` catalogue are carried from the
    delivery onto the record, so the shared answer ladder reads the caller's refusal
    sentence and policy when a typed reply is rejected.
    """
    ttl = _remaining_seconds(timeout_at)
    entry = Correlation(
        callback_url=callback_url,
        interaction_id=interaction_id,
        ttl_deadline=timeout_at,
        on_mismatch=on_mismatch,
        wording=wording,
    )
    stored = await twilio_correlation_store.set_correlation(
        correlation_key(twilio_number, human_number), entry, ttl_seconds=ttl
    )
    if not stored:
        raise PendingQuestionExistsError(
            f"a question is already pending for the pair ({twilio_number}, {human_number}); "
            "one pending question per number pair — answer or let it time out first"
        )


async def release_pending(twilio_number: str, human_number: str) -> None:
    """Drop the reservation (the send failed — the human never received the question)."""
    await twilio_correlation_store.release_correlation(correlation_key(twilio_number, human_number))


# Compare-and-delete in one atomic step: the pair's record is dropped only while it is held for
# ARGV[1], so a withdrawal never releases a hold a newer ask has taken on the same pair.
_RELEASE_PENDING_FOR_LUA: Final[str] = (
    "local raw = redis.call('GET', KEYS[1]) "
    "if not raw then return 0 end "
    "local rec = cjson.decode(raw) "
    "if rec.interaction_id == ARGV[1] then return redis.call('DEL', KEYS[1]) end "
    "return 0"
)


async def release_pending_for(twilio_number: str, human_number: str, interaction_id: str) -> bool:
    """Drop the pair's reservation ONLY if it is held for ``interaction_id``; True when released.

    A different ask's record, or no record at all, is left untouched and returns False. redis-py's
    async ``eval`` stub types a non-awaitable return; it is awaitable at runtime.
    """
    key = _pending_key(correlation_key(twilio_number, human_number))
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        released = await cast("Awaitable[int]", redis.eval(_RELEASE_PENDING_FOR_LUA, 1, key, interaction_id))
    return bool(released)


async def already_seen(message_sid: str) -> bool:
    """Whether this ``MessageSid`` was already handled (webhook retry or replay)."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        return bool(await redis.exists(_seen_key(message_sid)))


async def mark_seen(message_sid: str) -> None:
    """Remember a handled ``MessageSid`` for the configured dedupe window."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_seen_key(message_sid), "1", ex=twilio_settings().dedupe_ttl)
