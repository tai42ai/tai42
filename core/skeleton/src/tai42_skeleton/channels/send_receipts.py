"""Tier 2 of the send-outcome monitoring layer: delivered-vs-accepted receipts
reaching the originating trace.

A flow send (``notify_user`` on a named channel) returns the provider message ids the
medium ACCEPTED — but whether the medium later DELIVERED it arrives out of band, on the
channel's delivery-status webhook, long after the flow trace has closed. This module
carries that one hop:

- :func:`index_flow_send` writes a TTL'd ``provider_message_id -> {trace_id, span_id}``
  entry at the send seam when a trace is ambient, on the interactions Redis (the store
  the flow-send surface already uses), scoped by the interactions ``key_prefix`` for
  per-deployment isolation exactly as the notifications sink is.
- :func:`record_flow_send_receipt` is what the channel delivery-status webhooks call
  when the conversation bridge does not own an outbound id (its ``record_delivery_status``
  raised ``LookupError``): it resolves the id through this index and, on a hit, emits a
  ``delivery_receipt`` monitoring event onto the recorded trace/span with an EXPLICIT
  ``TraceContext`` — never an ambient emit, since the webhook runs in a detached context
  with no active trace. It returns whether the id was a known flow send, so the webhook
  keeps its genuinely-unknown-id log only on a miss.

This index covers exactly what the conversation bridge's own ledger/receipt path does
NOT — flow sends, which run no ConversationRecord. The bridge path stays untouched and
authoritative for bridge messages.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from tai42_contract.conversations import DeliveryReceipt
from tai42_contract.monitoring import MonitoringLevel, TraceContext
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured
from tai42_skeleton.monitoring import get_monitoring
from tai42_skeleton.utils.redis_typing import awaited

logger = logging.getLogger(__name__)

# The index's suffix within the shared interactions key namespace:
# ``{key_prefix}send-receipt:{channel}:{provider_message_id}``. The channel is a
# registered ``:``-free identifier (the qualifier), so the provider id sits LAST and
# may carry ``:`` of its own without bleeding across a segment boundary.
_INDEX_SUFFIX = "send-receipt:"

# The monitoring event name a resolved out-of-band receipt posts onto the flow trace.
_RECEIPT_EVENT_NAME = "delivery_receipt"


def _index_key(key_prefix: str, channel: str, provider_message_id: str) -> str:
    return f"{key_prefix}{_INDEX_SUFFIX}{channel}:{provider_message_id}"


async def index_flow_send(channel: str, provider_message_ids: list[str], *, trace_id: str, span_id: str) -> None:
    """Map each accepted provider message id of a flow send to its originating
    ``{trace_id, span_id}``, TTL'd, so a later out-of-band delivery receipt for that id
    can be posted back onto the flow trace.

    A no-op when the id list is empty (a channel that exposes no correlatable id) or the
    interactions store is unconfigured. The TTL is the receipt-relevance window
    (``INTERACTIONS_SEND_RECEIPT_INDEX_TTL_SECONDS``): long enough for a delayed carrier
    receipt, bounded so the index cannot accumulate."""
    if not provider_message_ids or not interactions_store_configured():
        return
    settings = interactions_settings()
    ttl = settings.send_receipt_index_ttl_seconds
    payload = json.dumps({"trace_id": trace_id, "span_id": span_id})
    async with client_ctx(RedisClient, settings.redis) as r:
        for provider_message_id in provider_message_ids:
            await awaited(r.set(_index_key(settings.key_prefix, channel, provider_message_id), payload, ex=ttl))


async def record_flow_send_receipt(
    channel: str, provider_message_id: str, receipt: DeliveryReceipt, *, errors: Any = None
) -> bool:
    """Post an out-of-band delivery receipt for a FLOW send back onto its originating trace.

    Resolves ``provider_message_id`` through the flow-send index; on a hit, emits a
    ``delivery_receipt`` event into the recorded trace, nested under the send span, with an
    EXPLICIT ``TraceContext`` (the webhook context is detached — an ambient emit would
    attach to nothing) — level ``ERROR`` for a FAILED receipt, default otherwise. The
    ``input`` carries the provider id, the receipt status, and any provider ``errors``.

    Returns ``True`` when the id was a known flow send (event emitted), ``False`` when it
    is not — the caller (the webhook) keeps its genuinely-unknown-id log only on a miss. A
    no-op returning ``False`` when the interactions store is unconfigured. Fail-safe
    end to end: the index READ is caught-and-logged and resolves to a benign miss
    (``False``) on any monitoring-store outage — this seam runs in the webhook's
    ``except LookupError`` fallback, so a raised Redis error would 500 an otherwise-healthy
    delivery-status webhook and break receipt ingestion — and the event WRITE is fail-safe by
    the writer's own contract (``create_event`` catches and logs its own backend errors)."""
    if not interactions_store_configured():
        return False
    settings = interactions_settings()
    try:
        async with client_ctx(RedisClient, settings.redis) as r:
            raw = await awaited(r.get(_index_key(settings.key_prefix, channel, provider_message_id)))
    except Exception:
        # A monitoring-store (interactions Redis) outage must never break receipt ingestion:
        # this resolves in the webhook's ``except LookupError`` fallback, so a raised error
        # would 500 the delivery-status webhook. Log and treat the id as an unknown flow send
        # (a benign miss) — the receipt correlation is lost, the webhook is not.
        logger.warning(
            "flow-send receipt index read failed for %s on channel %r; treating as a miss",
            provider_message_id,
            channel,
        )
        return False
    if raw is None:
        return False
    entry = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    failed = receipt is DeliveryReceipt.FAILED
    get_monitoring().writer.create_event(
        name=_RECEIPT_EVENT_NAME,
        level=MonitoringLevel.ERROR if failed else MonitoringLevel.DEFAULT,
        trace_context=TraceContext(trace_id=entry["trace_id"], parent_span_id=entry.get("span_id")),
        input={"provider_message_id": provider_message_id, "status": receipt.value, "errors": errors},
    )
    if failed:
        logger.info("flow send %s on channel %r reported FAILED by the provider", provider_message_id, channel)
    return True


__all__ = ["index_flow_send", "record_flow_send_receipt"]
