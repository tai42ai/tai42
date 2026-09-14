"""Api-door delivery: POST a produced answer to the row's ``callback_url`` under an HMAC
``X-Tai-Signature``, retried with backoff under the delivery lease, or — for a poll-only row
that declares no callback — drive the record terminal-readable for the poll door without a POST.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from tai42_skeleton.conversations.models import ConversationRecord
from tai42_skeleton.conversations.records import ConversationRecordStore

logger = logging.getLogger(__name__)

# Signed api-door callback header: ``HMAC-SHA256(callback_secret, raw_body)`` in hex.
_SIGNATURE_HEADER = "X-Tai-Signature"


async def _deliver_api(store: ConversationRecordStore, record: ConversationRecord, token: str) -> None:
    from tai42_skeleton.conversations import delivery as _pkg

    settings = store.settings
    if record.callback_url is None:
        # A poll-only api route declares no callback: its answer is served by the poll door
        # (GET /api/conversations/{route}/messages/{message_id}), so delivery is terminal the
        # moment the outcome is written — nothing is POSTed and no send attempt is spent.
        delivered = await store.mark_delivered(record.message_id, [], record.attempts, time.time(), token)
        if delivered != 1:
            logger.warning(
                "conversations: api record %s is poll-only; the delivered write returned %d; the record's "
                "outcome stands as another writer left it",
                record.message_id,
                delivered,
            )
        return
    route = await _pkg.get_conversations_manager().get_route(record.route_name)
    if route is None or route.callback_secret is None:
        await store.mark_failed(record.message_id, await store.bump_attempt(record.message_id), time.time(), token)
        logger.error(
            "conversations: api record %s cannot be delivered — route %r is gone or carries no callback secret; "
            "marked failed",
            record.message_id,
            record.route_name,
        )
        return

    # ``exclude_none`` omits the ``answer`` field entirely for a silent outcome, so the
    # callback body is ``{message_id, thread_id, status: "silent"}`` with no answer key.
    body = record.answer_payload().model_dump_json(exclude_none=True).encode()
    signature = _pkg._sign(route.callback_secret, body)
    while True:
        attempt = await store.bump_attempt(record.message_id)
        status = await _pkg._post_callback(
            record.callback_url, body, signature, settings.delivery_callback_timeout_seconds
        )
        if status is not None and 200 <= status < 300:
            delivered = await store.mark_delivered(record.message_id, [], attempt, time.time(), token)
            if delivered != 1:
                logger.warning(
                    "conversations: api callback for record %s succeeded but the delivered write returned %d; "
                    "the record's outcome stands as another writer left it",
                    record.message_id,
                    delivered,
                )
            return
        if attempt >= settings.delivery_max_attempts:
            failed = await store.mark_failed(record.message_id, attempt, time.time(), token)
            logger.error(
                "conversations: api callback for record %s to %s exhausted %d attempts (last status %s; failed write "
                "returned %d)",
                record.message_id,
                record.callback_url,
                attempt,
                status,
                failed,
            )
            return
        backoff = _pkg._backoff_seconds(settings, attempt)
        # Extend the lease over the upcoming backoff, or a re-drive reclaims a record this
        # worker is still retrying.
        held = await store.claim_delivery(
            record.message_id, time.time(), token, backoff + settings.delivery_claim_lease_seconds
        )
        if held != 1:
            # Lease lost: stop retrying rather than POST a second callback for a record
            # another worker now drives.
            logger.warning(
                "conversations: lost the delivery lease on record %s after attempt %d (claim returned %d); "
                "leaving the retry to the worker that holds it now",
                record.message_id,
                attempt,
                held,
            )
            return
        await asyncio.sleep(backoff)


async def _post_callback(url: str, body: bytes, signature: str, timeout_seconds: float) -> int | None:
    """POST the signed answer body, returning the HTTP status, or ``None`` when the request
    never completed — a transport error or a timeout is a retryable non-2xx, logged not raised.

    ``timeout_seconds`` is a hard total-request deadline (validated below the delivery lease), not
    httpx's per-phase timeout, so a slow receiver cannot keep the POST in flight past the lease.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
                response = await client.post(
                    url,
                    content=body,
                    headers={"Content-Type": "application/json", _SIGNATURE_HEADER: signature},
                )
            return response.status_code
    except (httpx.HTTPError, TimeoutError):
        logger.warning("conversations: callback POST to %s failed or timed out; will retry", url)
        return None
