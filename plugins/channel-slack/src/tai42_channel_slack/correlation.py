"""Plugin-owned correlation stores over kit's pooled ``RedisClient``.

Two correlation surfaces, each a contract :class:`~tai42_contract.channels.CorrelationStore`
so the shared inbound-answer ladder reads both through the same minimal port, plus the
Events-API dedupe claim:

* ``channel:slack:corr:<ts>`` -> a JSON ``{callback_url, interaction_id, timeout_at}``
  record. Written after a successful ``chat.postMessage`` (the posted ``ts`` is the
  thread anchor every in-thread reply carries as ``thread_ts``). TTL = the question's
  remaining budget. :class:`SlackThreadCorrelationStore` is the port over it — the
  Events door hands it to the ladder keyed by ``thread_ts``.
* ``channel:slack:form:<interaction_id>`` -> a JSON
  ``{callback_url, schema, question, timeout_at}`` record. Written BEFORE a ``form``
  question's ``chat.postMessage`` (reserve-before-send: released if the send fails),
  holding the state the interactivity door needs to open the modal.
  :class:`SlackFormCorrelationStore` is the port over it (projecting the record down to
  the three :class:`Correlation` fields the ladder needs — the interaction id IS the
  key), while the channel keeps the rich record through :func:`get_form_record` for the
  modal open and submission decode. TTL = the question's remaining budget; keyed by the
  interaction id.
* ``channel:slack:event:<event_id>`` -> ``"1"``. Events API dedupe claim (SET NX EX)
  whose TTL outlives Slack's retry ladder. A handler that fails AFTER claiming releases
  the claim before re-raising, so the retry reprocesses the event.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any, cast

from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError, Correlation
from tai42_kit.clients.impl.redis import RedisClient

from tai42_channel_slack.settings import SlackRedisSettings, slack_redis_settings

_CORR_KEY = "channel:slack:corr:{key}"
_DEDUPE_KEY = "channel:slack:event:{event_id}"
_FORM_KEY = "channel:slack:form:{key}"

# Outlives Slack's full retry ladder (immediate, ~1 min, ~5 min) with margin.
DEDUPE_TTL_SECONDS = 900


def _redis_settings() -> SlackRedisSettings:
    """The correlation-store connection, raising a clear config error when unset."""
    settings = slack_redis_settings()
    if not settings.redis_url:
        raise ValueError("Slack channel correlation store is not configured: set CHANNEL_SLACK_REDIS_URL.")
    return settings


def remaining_seconds(timeout_at: datetime) -> int:
    """Whole seconds left in the question budget (ceil; <= 0 means expired)."""
    return math.ceil((timeout_at - datetime.now(UTC)).total_seconds())


class SlackThreadCorrelationStore:
    """Satisfies :class:`~tai42_contract.channels.CorrelationStore` over the
    ``channel:slack:corr:<ts>`` keys — the thread-reply correlation surface."""

    async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
        """Reserve ``key`` (the thread ``ts``) for ``entry`` NX with a ``ttl_seconds``
        expiry. A ``ts`` is unique per posted message, so the NX just makes the
        one-pending guarantee explicit."""
        value = json.dumps(
            {
                "callback_url": entry.callback_url,
                "interaction_id": entry.interaction_id,
                "timeout_at": entry.ttl_deadline.isoformat(),
            }
        )
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            stored = await redis.set(_CORR_KEY.format(key=key), value, ex=ttl_seconds, nx=True)
        return bool(stored)

    async def get_correlation(self, key: str) -> Correlation | None:
        """The pending question's record under ``key``, or ``None`` — a non-destructive
        peek the ladder forwards from."""
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            raw = cast("str | None", await redis.get(_CORR_KEY.format(key=key)))
        if raw is None:
            return None
        data = json.loads(raw)
        return Correlation(
            callback_url=data["callback_url"],
            interaction_id=data.get("interaction_id", ""),
            ttl_deadline=datetime.fromisoformat(data["timeout_at"]),
        )

    async def release_correlation(self, key: str) -> None:
        """Drop any reservation under ``key``, idempotently (a no-op when free)."""
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            await redis.delete(_CORR_KEY.format(key=key))


class SlackFormCorrelationStore:
    """Satisfies :class:`~tai42_contract.channels.CorrelationStore` over the
    ``channel:slack:form:<interaction_id>`` keys — the modal-submission surface.

    The port projects the rich form record down to the three :class:`Correlation`
    fields the ladder needs (the interaction id IS the key). The channel keeps the rich
    record — ``schema``/``question`` — through :func:`get_form_record` for the modal
    open and submission decode; ``set_correlation`` writes a minimal record (used for
    conformance and by any port-only caller), while the rich deliver path uses
    :func:`store_form_record`.
    """

    async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
        value = json.dumps(
            {
                "callback_url": entry.callback_url,
                "schema": None,
                "question": "",
                "timeout_at": entry.ttl_deadline.isoformat(),
            }
        )
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            stored = await redis.set(_FORM_KEY.format(key=key), value, ex=ttl_seconds, nx=True)
        return bool(stored)

    async def get_correlation(self, key: str) -> Correlation | None:
        record = await get_form_record(key)
        if record is None:
            return None
        return Correlation(
            callback_url=record["callback_url"],
            interaction_id=key,
            ttl_deadline=datetime.fromisoformat(record["timeout_at"]),
        )

    async def release_correlation(self, key: str) -> None:
        await delete_form_record(key)


# Module-level singletons: the stores are stateless, so one instance of each serves the
# deliver and inbound paths (the channel hands the matching one to the shared ladder).
slack_thread_correlation_store = SlackThreadCorrelationStore()
slack_form_correlation_store = SlackFormCorrelationStore()


async def store_correlation(ts: str, callback_url: str, interaction_id: str, timeout_at: datetime) -> None:
    """Map the posted message's ``ts`` to its :class:`Correlation` record, TTL = budget.

    The deliver path's front door onto :meth:`SlackThreadCorrelationStore.set_correlation`.
    Raises :class:`ChannelDeliveryError` when the budget is already spent — never a
    non-positive-TTL write.
    """
    ttl = remaining_seconds(timeout_at)
    if ttl <= 0:
        raise ChannelDeliveryError(f"question budget already expired (timeout_at={timeout_at.isoformat()})")
    entry = Correlation(callback_url=callback_url, interaction_id=interaction_id, ttl_deadline=timeout_at)
    await slack_thread_correlation_store.set_correlation(ts, entry, ttl_seconds=ttl)


async def delete_correlation(thread_ts: str) -> None:
    """Drop the ``ts`` mapping (the callback door's single-use claim is the real
    idempotency guard; this just stops later thread chatter re-forwarding)."""
    await slack_thread_correlation_store.release_correlation(thread_ts)


async def store_form_record(
    interaction_id: str, callback_url: str, schema: dict[str, Any], question: str, timeout_at: datetime
) -> None:
    """Reserve the form's rich state before its message is sent, TTL = budget.

    Raises :class:`ChannelDeliveryError` when the budget is already spent — never a
    non-positive-TTL write.
    """
    ttl = remaining_seconds(timeout_at)
    if ttl <= 0:
        raise ChannelDeliveryError(f"question budget already expired (timeout_at={timeout_at.isoformat()})")
    record = json.dumps(
        {"callback_url": callback_url, "schema": schema, "question": question, "timeout_at": timeout_at.isoformat()}
    )
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_FORM_KEY.format(key=interaction_id), record, ex=ttl)


async def get_form_record(interaction_id: str) -> dict[str, Any] | None:
    """The pending form's ``{callback_url, schema, question, timeout_at}`` record, or
    ``None`` when unknown/expired (the button outlived its question). The adapter-private
    rich read the modal open + submission decode use, distinct from the port's projection."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        raw = cast("str | None", await redis.get(_FORM_KEY.format(key=interaction_id)))
    return json.loads(raw) if raw is not None else None


async def delete_form_record(interaction_id: str) -> None:
    """Drop a form record once its submission is terminally settled (forwarded or the
    ticket is gone)."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.delete(_FORM_KEY.format(key=interaction_id))


async def claim_dedupe(event_id: str) -> bool:
    """Atomically claim ``event_id`` (SET NX EX). ``False`` = already processed or
    currently in flight — the caller acks the retry without reprocessing."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        return bool(await redis.set(_DEDUPE_KEY.format(event_id=event_id), "1", ex=DEDUPE_TTL_SECONDS, nx=True))


async def release_dedupe(event_id: str) -> None:
    """Release a claim whose processing failed, so Slack's retry reprocesses the event
    instead of hitting the duplicate ack."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.delete(_DEDUPE_KEY.format(event_id=event_id))
