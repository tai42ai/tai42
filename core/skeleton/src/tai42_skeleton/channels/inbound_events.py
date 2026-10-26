"""The inbound seam's platform-event emits, and its media-ingestion telemetry.

:func:`emit_event` is the one best-effort emit every inbound emitter shares. The two
media-ingestion topics and their emitters are the served-media ingestion chokepoint's
(``AppMedia.ingest_media``) operator record of one ingest attempt.
"""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.conversations import InboundRejectionReason

from tai42_skeleton.hooks import cache as hooks_cache

logger = logging.getLogger(__name__)

__all__ = [
    "INBOUND_MEDIA_INGESTED_EVENT_TOPIC",
    "INBOUND_MEDIA_REJECTED_EVENT_TOPIC",
    "emit_event",
    "emit_inbound_media_ingested",
    "emit_inbound_media_rejected",
]

# The seam's INGESTION-telemetry topics, emitted by the served-media ingestion chokepoint
# (``AppMedia.ingest_media``): one on success, one before a raise. DISTINCT from the
# participant-notice topic in ``inbound_rejection.py`` — these are the operator/ops record of an
# ingest ATTEMPT (its outcome, size and hashes), never the record that a participant was told.
# The payload NEVER carries the ingested bytes or a vendor credential.
INBOUND_MEDIA_INGESTED_EVENT_TOPIC = "conversations_inbound_media_ingested"
INBOUND_MEDIA_REJECTED_EVENT_TOPIC = "conversations_inbound_media_rejected"


async def emit_event(topic: str, payload: dict[str, Any], *, failure_desc: str) -> None:
    """Emit ``topic`` with ``payload`` on the hooks manager, best-effort.

    The one seam the inbound emitters share: the hooks-manager lookup and the
    log-then-swallow of a hooks-manager fault, so an operator-telemetry emit never fails the
    inbound webhook. ``failure_desc`` completes the log line ``inbound: failed to emit
    <topic> <failure_desc>``.
    """
    try:
        await hooks_cache.get_hooks_manager().on_event(topic=topic, payload=payload)
    except Exception:
        logger.warning("inbound: failed to emit %r %s", topic, failure_desc, exc_info=True)


async def emit_inbound_media_ingested(
    *,
    channel_id: str,
    participant_identity: str,
    message_id: str | None,
    media_id: str,
    kind: str,
    size: int,
    sha256: str,
    mime: str,
    pending: bool,
) -> None:
    """Emit the ``conversations_inbound_media_ingested`` event on a successful media ingest.

    The seam's operator/ops telemetry of one ingest attempt's outcome. The payload names the
    origin, the served ``media_id``, the derived ``kind``, the size, the content hash and the
    sniffed ``mime`` — never the ingested bytes or a vendor credential. Best-effort: a
    hooks-manager failure is logged and swallowed so it never fails the inbound webhook.
    """
    payload = {
        "channel": channel_id,
        "client_address": participant_identity,
        "message_id": message_id,
        "media_id": media_id,
        "kind": kind,
        "size": size,
        "sha256": sha256,
        "mime": mime,
        "pending": pending,
    }
    await emit_event(
        INBOUND_MEDIA_INGESTED_EVENT_TOPIC,
        payload,
        failure_desc=f"for an ingested media on channel {channel_id!r}",
    )


async def emit_inbound_media_rejected(
    *,
    channel_id: str,
    participant_identity: str,
    message_id: str | None,
    kind: str,
    reason: InboundRejectionReason,
) -> None:
    """Emit the ``conversations_inbound_media_rejected`` event before the seam raises.

    The seam's operator/ops telemetry of one FAILED ingest attempt. The payload names the origin,
    the generic ``kind`` and the mapped ``reason`` — never the ingested bytes or a credential.
    Emitting then raising is not a swallow: the seam still propagates the typed error. Best-effort:
    a hooks-manager failure is logged and swallowed.
    """
    payload = {
        "channel": channel_id,
        "client_address": participant_identity,
        "message_id": message_id,
        "kind": kind,
        "reason": reason.value,
    }
    await emit_event(
        INBOUND_MEDIA_REJECTED_EVENT_TOPIC,
        payload,
        failure_desc=f"for a rejected media on channel {channel_id!r}",
    )
