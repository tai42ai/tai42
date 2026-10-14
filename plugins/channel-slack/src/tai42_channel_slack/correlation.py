"""Plugin-owned correlation stores over kit's pooled ``RedisClient``.

Two correlation surfaces, each a contract :class:`~tai42_contract.channels.CorrelationStore`
so the shared inbound-answer ladder reads both through the same minimal port, plus the
Events-API dedupe claim:

* ``channel:slack:corr:<ts>`` -> a JSON ``{callback_url, interaction_id, timeout_at,
  on_mismatch, wording}`` record. Written after a successful ``chat.postMessage`` (the posted ``ts`` is the
  thread anchor every in-thread reply carries as ``thread_ts``). TTL = the question's
  remaining budget. :class:`SlackThreadCorrelationStore` is the port over it — the
  Events door hands it to the ladder keyed by ``thread_ts``.
* ``channel:slack:form:<interaction_id>`` -> a JSON
  ``{callback_url, schema, question, timeout_at, on_mismatch, wording?, ...}`` record. Written BEFORE a ``form``
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
from tai42_contract.interactions.models import AnswerMismatchPolicy, Wording
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
    """Satisfies :class:`~tai42_contract.channels.CorrelationStore` over the ``channel:slack:corr:<ts>`` keys.

    The thread-reply correlation surface.
    """

    async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
        """Reserve ``key`` (the thread ``ts``) for ``entry`` NX with a ``ttl_seconds`` expiry.

        A ``ts`` is unique per posted message, so the NX just makes the one-pending guarantee
        explicit. The record carries the ask's digression policy (``on_mismatch``) and per-send
        ``wording`` catalogue alongside the callback/budget so the shared answer ladder, reading
        this record on a threaded reply, honours the policy and draws the caller's refusal sentence.
        """
        value = json.dumps(
            {
                "callback_url": entry.callback_url,
                "interaction_id": entry.interaction_id,
                "timeout_at": entry.ttl_deadline.isoformat(),
                "on_mismatch": entry.on_mismatch.value,
                "wording": entry.wording.model_dump(mode="json", exclude_none=True)
                if entry.wording is not None
                else None,
            }
        )
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            stored = await redis.set(_CORR_KEY.format(key=key), value, ex=ttl_seconds, nx=True)
        return bool(stored)

    async def get_correlation(self, key: str) -> Correlation | None:
        """The pending question's record under ``key``, or ``None`` — a non-destructive peek to forward from."""
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            raw = cast("str | None", await redis.get(_CORR_KEY.format(key=key)))
        if raw is None:
            return None
        data = json.loads(raw)
        raw_wording = data["wording"]
        return Correlation(
            callback_url=data["callback_url"],
            interaction_id=data["interaction_id"],
            ttl_deadline=datetime.fromisoformat(data["timeout_at"]),
            on_mismatch=AnswerMismatchPolicy(data["on_mismatch"]),
            wording=Wording.model_validate(raw_wording) if raw_wording is not None else None,
        )

    async def release_correlation(self, key: str) -> None:
        """Drop any reservation under ``key``, idempotently (a no-op when free)."""
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            await redis.delete(_CORR_KEY.format(key=key))


class SlackFormCorrelationStore:
    """Satisfies :class:`~tai42_contract.channels.CorrelationStore` over the ``channel:slack:form:<id>`` keys.

    The modal-submission surface.

    The port projects the rich form record down to the three :class:`Correlation`
    fields the ladder needs (the interaction id IS the key). The channel keeps the rich
    record — ``schema``/``question`` — through :func:`get_form_record` for the modal
    open and submission decode; ``set_correlation`` writes a minimal record (used for
    conformance and by any port-only caller), while the rich deliver path uses
    :func:`store_form_record`.
    """

    async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
        """Reserve ``key`` (the interaction id) for ``entry`` NX with a ``ttl_seconds`` expiry, minimally.

        The minimal record carries the ask's digression policy (``on_mismatch``) and per-send
        ``wording`` alongside the callback/budget, so :meth:`get_correlation` projects the same
        :class:`Correlation` fields the shared answer ladder reads as the rich deliver path does.
        """
        value = json.dumps(
            {
                "callback_url": entry.callback_url,
                "schema": None,
                "question": "",
                "timeout_at": entry.ttl_deadline.isoformat(),
                "on_mismatch": entry.on_mismatch.value,
                "wording": entry.wording.model_dump(mode="json", exclude_none=True)
                if entry.wording is not None
                else None,
            }
        )
        async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
            stored = await redis.set(_FORM_KEY.format(key=key), value, ex=ttl_seconds, nx=True)
        return bool(stored)

    async def get_correlation(self, key: str) -> Correlation | None:
        """The pending form question's :class:`Correlation` under ``key``, or ``None``.

        Projects the rich record down to the port fields the ladder reads, carrying the ask's
        ``on_mismatch`` policy and per-send ``wording`` so a rejected modal submission honours the
        policy and the caller's refusal sentence.
        """
        record = await get_form_record(key)
        if record is None:
            return None
        raw_wording = record.get("wording")
        return Correlation(
            callback_url=record["callback_url"],
            interaction_id=key,
            ttl_deadline=datetime.fromisoformat(record["timeout_at"]),
            on_mismatch=AnswerMismatchPolicy(record["on_mismatch"]),
            wording=Wording.model_validate(raw_wording) if raw_wording is not None else None,
        )

    async def release_correlation(self, key: str) -> None:
        """Drop any form reservation under ``key``, idempotently."""
        await delete_form_record(key)


# Module-level singletons: the stores are stateless, so one instance of each serves the
# deliver and inbound paths (the channel hands the matching one to the shared ladder).
slack_thread_correlation_store = SlackThreadCorrelationStore()
slack_form_correlation_store = SlackFormCorrelationStore()


async def store_correlation(
    ts: str,
    callback_url: str,
    interaction_id: str,
    timeout_at: datetime,
    *,
    on_mismatch: AnswerMismatchPolicy = AnswerMismatchPolicy.RETRY,
    wording: Wording | None = None,
) -> None:
    """Map the posted message's ``ts`` to its :class:`Correlation` record, TTL = budget.

    The deliver path's front door onto :meth:`SlackThreadCorrelationStore.set_correlation`.
    ``on_mismatch`` and ``wording`` are the ask's digression policy and per-send catalogue copied
    from the ``ChannelDelivery``, so the shared answer ladder reads them off this record on a
    threaded reply. Raises :class:`ChannelDeliveryError` when the budget is already spent — never a
    non-positive-TTL write.
    """
    ttl = remaining_seconds(timeout_at)
    if ttl <= 0:
        raise ChannelDeliveryError(f"question budget already expired (timeout_at={timeout_at.isoformat()})")
    entry = Correlation(
        callback_url=callback_url,
        interaction_id=interaction_id,
        ttl_deadline=timeout_at,
        on_mismatch=on_mismatch,
        wording=wording,
    )
    await slack_thread_correlation_store.set_correlation(ts, entry, ttl_seconds=ttl)


async def store_form_record(
    interaction_id: str,
    callback_url: str,
    schema: dict[str, Any],
    question: str,
    timeout_at: datetime,
    data: dict[str, Any] | None = None,
    pages: list[dict[str, Any]] | None = None,
    reactions: dict[str, Any] | None = None,
    wording: dict[str, Any] | None = None,
    on_mismatch: AnswerMismatchPolicy = AnswerMismatchPolicy.RETRY,
) -> None:
    """Reserve the form's rich state before its message is sent, TTL = budget.

    ``data`` (``{values, options}``), ``pages`` (layout + display + review) and ``reactions``
    (the reaction triggers) are the per-send enrichment the modal is built from AT CLICK TIME
    (a modal opens on the button tap, not at delivery) and read again on each reacting /
    conditional re-render and the submitted check, so they ride the record; ``None``/absent
    renders the plain modal. ``wording`` (the per-send catalogue dump) is the form chrome the
    click-time open and every re-render read off the record — the modal's ``title``, ``submit``,
    ``close``, review line and per-field placeholders — and the refusal sentence the rejected
    modal submission draws; ``on_mismatch`` is the ask's digression policy the shared answer ladder
    reads through the form store's :meth:`SlackFormCorrelationStore.get_correlation`. Raises
    :class:`ChannelDeliveryError` when the budget is already spent — never a non-positive-TTL write.
    """
    ttl = remaining_seconds(timeout_at)
    if ttl <= 0:
        raise ChannelDeliveryError(f"question budget already expired (timeout_at={timeout_at.isoformat()})")
    payload: dict[str, Any] = {
        "callback_url": callback_url,
        "schema": schema,
        "question": question,
        "timeout_at": timeout_at.isoformat(),
        "on_mismatch": on_mismatch.value,
    }
    if data is not None:
        payload["data"] = data
    if pages is not None:
        payload["pages"] = pages
    if reactions is not None:
        payload["reactions"] = reactions
    if wording is not None:
        payload["wording"] = wording
    record = json.dumps(payload)
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_FORM_KEY.format(key=interaction_id), record, ex=ttl)


async def get_form_record(interaction_id: str) -> dict[str, Any] | None:
    """The pending form's record, or ``None`` when unknown/expired (the button outlived its question).

    The record is ``{callback_url, schema, question, timeout_at, on_mismatch, data?, pages?,
    reactions?, wording?}``. The adapter-private rich read the modal open, re-render and
    submission decode use, distinct from the port's projection.
    """
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        raw = cast("str | None", await redis.get(_FORM_KEY.format(key=interaction_id)))
    return json.loads(raw) if raw is not None else None


async def delete_form_record(interaction_id: str) -> None:
    """Drop a form record once its submission is terminally settled (forwarded or the ticket is gone)."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.delete(_FORM_KEY.format(key=interaction_id))


async def claim_dedupe(event_id: str) -> bool:
    """Atomically claim ``event_id`` (SET NX EX).

    ``False`` = already processed or currently in flight — the caller acks the retry without reprocessing.
    """
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        return bool(await redis.set(_DEDUPE_KEY.format(event_id=event_id), "1", ex=DEDUPE_TTL_SECONDS, nx=True))


async def release_dedupe(event_id: str) -> None:
    """Release a claim whose processing failed.

    Slack's retry then reprocesses the event instead of hitting the duplicate ack.
    """
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.delete(_DEDUPE_KEY.format(event_id=event_id))
