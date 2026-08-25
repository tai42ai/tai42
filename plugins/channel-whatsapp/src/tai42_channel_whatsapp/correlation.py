"""Pending-question correlation store (plugin-owned Redis).

A WhatsApp reply carries no thread, so the only correlation key is the pair
(<phone_number_id>, <wa_id>). AT MOST ONE pending question per pair — an atomic
``SET NX`` reservation; a second concurrent question is rejected loudly with
``PendingQuestionExistsError``. The value holds the ``callback_url`` and deadline;
the key TTL is the remaining answer budget, so an expired question cannot capture
a later reply.

The record is read NON-DESTRUCTIVELY: ``peek_pending`` returns the rich record
without claiming it, and the shared ladder (through the ``CorrelationStore``
port's ``get_correlation``/``release_correlation``) decides when the reservation
is released — a kept rejection leaves the record untouched, and only a terminal
outcome deletes it. ``bump_rejections`` increments the re-ask counter in place on
the still-held record. Exclusivity lives entirely on the ``SET NX`` reserve.

A select ask also carries its ``options`` and ``interaction_id`` so an inbound
interactive tap (whose id is ``{interaction_id}:{index}``) maps back to the exact
option text under the exact ask it was sent for.

A published-Flow cache maps ``(waba_id, schema_hash)`` to a Meta flow id with NO
TTL — a published Flow persists on Meta, so the id is reused for every form ask
sharing that answer schema; a cache miss triggers a create + publish + store.

A handled-``wamid`` set is the replay guard (a redelivered webhook repeats the id).

A KNOWN-CONTACT marker records that a ``(phone_number_id, wa_id)`` pair sent an
inbound message within a rolling window (TTL refreshed per inbound); the
template-send recipient policy reads it to admit a pair the operator has not
allowlisted but who opened the conversation window.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError, Correlation
from tai42_kit.clients.impl.redis import RedisClient

from tai42_channel_whatsapp.settings import (
    WhatsAppRedisSettings,
    whatsapp_redis_settings,
    whatsapp_settings,
)

logger = logging.getLogger(__name__)


class PendingQuestionExistsError(ChannelDeliveryError):
    """A question is already pending for this pair (one at a time)."""


@dataclass(frozen=True)
class PendingQuestion:
    callback_url: str
    timeout_at: datetime
    # A select ask carries its option list and interaction id so an interactive
    # tap (id ``{interaction_id}:{index}``) resolves to the exact option text
    # under the exact ask; a text ask leaves both None.
    options: list[str] | None = None
    interaction_id: str | None = None
    # A form ask carries its answer schema so an inbound Flow response (nfm_reply)
    # is coerced to the schema's types before it is forwarded; non-form asks None.
    schema: dict[str, Any] | None = None
    # A form ask carries its question text so a door-rejected answer can be re-asked
    # with a fresh Flow whose body repeats the question; non-form asks None.
    question: str | None = None
    # Door-400 rejections already recovered by re-sending a fresh Flow. Bounds the
    # re-send loop (see the inbound handler's cap); starts at 0.
    rejections: int = 0


def correlation_key(phone_number_id: str, wa_id: str) -> str:
    """The opaque correlation key for a ``(phone_number_id, wa_id)`` pair — the
    contract store's ``key``. WhatsApp replies carry no thread, so the pair is the key."""
    return f"{phone_number_id}:{wa_id}"


def _pending_key(phone_number_id: str, wa_id: str) -> str:
    return f"channel:whatsapp:pending:{correlation_key(phone_number_id, wa_id)}"


def _flow_key(waba_id: str, schema_hash: str) -> str:
    return f"channel:whatsapp:flow:{waba_id}:{schema_hash}"


def _seen_key(wamid: str) -> str:
    return f"channel:whatsapp:seen:{wamid}"


def _contact_key(phone_number_id: str, wa_id: str) -> str:
    return f"channel:whatsapp:known-contact:{phone_number_id}:{wa_id}"


def _encode_pending(question: PendingQuestion) -> str:
    return json.dumps(
        {
            "callback_url": question.callback_url,
            "timeout_at": question.timeout_at.isoformat(),
            "options": question.options,
            "interaction_id": question.interaction_id,
            "schema": question.schema,
            "question": question.question,
            "rejections": question.rejections,
        }
    )


def _redis_settings() -> WhatsAppRedisSettings:
    """The correlation-store connection, raising a clear config error when unset."""
    settings = whatsapp_redis_settings()
    if not settings.redis_url:
        raise ValueError("WhatsApp channel correlation store is not configured: set CHANNEL_WHATSAPP_REDIS_URL.")
    return settings


def _remaining_seconds(timeout_at: datetime) -> int:
    """Whole seconds until the deadline, raising when it already passed."""
    remaining = math.ceil((timeout_at - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        raise ChannelDeliveryError(f"question deadline {timeout_at.isoformat()} has already passed")
    return remaining


async def reserve_pending(
    phone_number_id: str,
    wa_id: str,
    callback_url: str,
    timeout_at: datetime,
    options: list[str] | None = None,
    interaction_id: str | None = None,
    schema: dict[str, Any] | None = None,
    question: str | None = None,
) -> None:
    """Atomically reserve the pair for one question, or raise ``PendingQuestionExistsError``."""
    value = _encode_pending(
        PendingQuestion(
            callback_url=callback_url,
            timeout_at=timeout_at,
            options=options,
            interaction_id=interaction_id,
            schema=schema,
            question=question,
        )
    )
    ttl = _remaining_seconds(timeout_at)
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        stored = await redis.set(_pending_key(phone_number_id, wa_id), value, nx=True, ex=ttl)
    if not stored:
        raise PendingQuestionExistsError(
            f"a question is already pending for the pair ({phone_number_id}, {wa_id}); "
            "one pending question per pair — answer or let it time out first"
        )


async def release_pending(phone_number_id: str, wa_id: str) -> None:
    """Drop the reservation (the send failed — the human never received the question)."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.delete(_pending_key(phone_number_id, wa_id))


def _decode_pending(raw: str | bytes) -> PendingQuestion:
    data = json.loads(raw)
    return PendingQuestion(
        callback_url=data["callback_url"],
        timeout_at=datetime.fromisoformat(data["timeout_at"]),
        options=data.get("options"),
        interaction_id=data.get("interaction_id"),
        schema=data.get("schema"),
        question=data["question"],
        rejections=data["rejections"],
    )


async def peek_pending(phone_number_id: str, wa_id: str) -> PendingQuestion | None:
    """Read the FULL pending record WITHOUT claiming it (``GET``, no ``DEL``);
    ``None`` when there is none.

    The adapter-private decode surface: the shared ladder reads only the port fields
    (via :meth:`WhatsAppCorrelationStore.get_correlation`), but the channel needs the
    rich record — ``options`` to map an interactive tap to its answer, ``schema`` to
    coerce a Flow response and to know the ask is form-shaped, ``question`` +
    ``rejections`` to re-send a fresh Flow on a rejection. Non-destructive, so a
    stale/malformed reply never removes a live ask a concurrent genuine reply could
    still answer.
    """
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        raw = await redis.get(_pending_key(phone_number_id, wa_id))
    if raw is None:
        return None
    return _decode_pending(raw)


async def bump_rejections(phone_number_id: str, wa_id: str, question: PendingQuestion) -> None:
    """Record one more door rejection on the STILL-HELD pending record (rejections+1),
    preserving its remaining budget as the TTL.

    Called after the shared ladder returned RETRY_KEPT for a form ask and the channel
    re-sent a fresh Flow: the ladder kept the reservation, so this is an in-place
    overwrite (no NX guard needed — the one-pending NX means no other reservation can
    take the pair while ours is held). A record past its deadline is left to expire.
    """
    remaining = math.ceil((question.timeout_at - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        return
    value = _encode_pending(replace(question, rejections=question.rejections + 1))
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_pending_key(phone_number_id, wa_id), value, ex=remaining)


class WhatsAppCorrelationStore:
    """Satisfies the contract :class:`~tai42_contract.channels.CorrelationStore` over
    the plugin-owned ``channel:whatsapp:pending:{pnid}:{wa_id}`` keys.

    The shared inbound-answer ladder uses ONLY this port surface (a non-destructive
    ``get`` peek and an idempotent ``release``). The channel keeps its richer decode
    state — ``options``/``schema``/``question``/``rejections`` — in the SAME stored
    record and reads it through the adapter-private :func:`peek_pending`; this port
    projects that record down to the three :class:`Correlation` fields the ladder
    needs. ``set_correlation`` (used for conformance and by any port-only caller)
    writes a minimal text-ask-shaped record — the rich deliver path uses
    :func:`reserve_pending` instead.
    """

    async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
        """Reserve ``key`` for ``entry`` NX with a ``ttl_seconds`` expiry; True when it
        was free and is now held, False when a question is already pending for the pair."""
        value = _encode_pending(
            PendingQuestion(
                callback_url=entry.callback_url,
                timeout_at=entry.ttl_deadline,
                interaction_id=entry.interaction_id,
            )
        )
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            stored = await redis.set(_pending_key_from_opaque(key), value, nx=True, ex=ttl_seconds)
        return bool(stored)

    async def get_correlation(self, key: str) -> Correlation | None:
        """The pending record's port fields under ``key``, or ``None`` — a
        non-destructive peek the ladder forwards from."""
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            raw = await redis.get(_pending_key_from_opaque(key))
        if raw is None:
            return None
        record = _decode_pending(raw)
        return Correlation(
            callback_url=record.callback_url,
            interaction_id=record.interaction_id or "",
            ttl_deadline=record.timeout_at,
        )

    async def release_correlation(self, key: str) -> None:
        """Drop any reservation under ``key``, idempotently (a no-op when free)."""
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            await redis.delete(_pending_key_from_opaque(key))


def _pending_key_from_opaque(key: str) -> str:
    return f"channel:whatsapp:pending:{key}"


# Module-level singleton: the store is stateless, so one instance serves the deliver
# and inbound paths (the channel hands it to the shared ladder explicitly).
whatsapp_correlation_store = WhatsAppCorrelationStore()


async def get_cached_flow_id(waba_id: str, schema_hash: str) -> str | None:
    """The published flow id for this ``(waba_id, schema_hash)``, or ``None``."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        raw = await redis.get(_flow_key(waba_id, schema_hash))
    if raw is None:
        return None
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw


async def cache_flow_id(waba_id: str, schema_hash: str, flow_id: str) -> None:
    """Store the published flow id with NO TTL (a published Flow persists on Meta)."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_flow_key(waba_id, schema_hash), flow_id)


async def already_seen(wamid: str) -> bool:
    """Whether this ``wamid`` was already handled (webhook retry or replay)."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        return bool(await redis.exists(_seen_key(wamid)))


async def mark_seen(wamid: str) -> None:
    """Remember a handled ``wamid`` for the configured dedupe window."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_seen_key(wamid), "1", ex=whatsapp_settings().dedupe_ttl)


async def mark_known_contact(phone_number_id: str, wa_id: str) -> None:
    """Record that this pair sent an inbound message, with a rolling window TTL.

    The TTL is ``CHANNEL_WHATSAPP_TEMPLATE_CONTACT_WINDOW_DAYS`` days and is
    refreshed on every inbound (a rolling "seen within N days"). A window of 0
    disables tracking (allowlist-only template sends) — nothing is written.
    """
    window_days = whatsapp_settings().template_contact_window_days
    if window_days <= 0:
        return
    ttl = window_days * 86_400
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_contact_key(phone_number_id, wa_id), "1", ex=ttl)


async def is_known_contact(phone_number_id: str, wa_id: str) -> bool:
    """Whether this pair sent an inbound message within the current window."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        return bool(await redis.exists(_contact_key(phone_number_id, wa_id)))
