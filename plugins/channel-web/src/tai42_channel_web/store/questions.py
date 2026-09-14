"""Pending-question records: the one-shot claim that forwards a web answer."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from tai42_contract.channels import ChannelDeliveryError

from tai42_channel_web.store.connection import _redis

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuestionRecord:
    """A pending web question: the callback door to forward the answer to, and the
    transcript pair the ``chat.answered`` frame is appended to on success.

    ``restores`` counts how often a failed forward has already put this record back.
    It rides the record itself so the count expires exactly with the question, and it
    is what bounds a visitor looping an answer the callback door keeps refusing."""

    callback_url: str
    identity: str
    address: str
    timeout_at: datetime
    restores: int = 0


def _question_key(interaction_id: str) -> str:
    return f"channel:web:question:{interaction_id}"


def _encode_record(record: QuestionRecord) -> str:
    return json.dumps(
        {
            "callback_url": record.callback_url,
            "identity": record.identity,
            "address": record.address,
            "timeout_at": record.timeout_at.astimezone(UTC).isoformat(),
            "restores": record.restores,
        }
    )


def _decode_record(raw: str | bytes) -> QuestionRecord:
    data = json.loads(raw)
    return QuestionRecord(
        callback_url=data["callback_url"],
        identity=data["identity"],
        address=data["address"],
        timeout_at=datetime.fromisoformat(data["timeout_at"]),
        restores=data["restores"],
    )


def _remaining_seconds(timeout_at: datetime) -> int:
    """Whole seconds until the deadline, raising when it already passed."""
    remaining = math.ceil((timeout_at.astimezone(UTC) - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        raise ChannelDeliveryError(f"question deadline {timeout_at.isoformat()} has already passed")
    return remaining


async def reserve_question(interaction_id: str, record: QuestionRecord) -> None:
    """Store the pending-question record with a TTL of the remaining answer budget."""
    ttl = _remaining_seconds(record.timeout_at)
    async with _redis() as redis:
        await redis.set(_question_key(interaction_id), _encode_record(record), ex=ttl)


async def release_question(interaction_id: str) -> None:
    """Drop a reservation (the transcript append failed — the human never saw it)."""
    async with _redis() as redis:
        await redis.delete(_question_key(interaction_id))


async def peek_question(interaction_id: str) -> QuestionRecord | None:
    """Read the pending record WITHOUT claiming it, so the answer door can refuse a
    question belonging to another conversation before the destructive ``GETDEL``."""
    async with _redis() as redis:
        raw = await redis.get(_question_key(interaction_id))
    if raw is None:
        return None
    return _decode_record(raw)


async def claim_question(interaction_id: str) -> QuestionRecord | None:
    """Atomically claim-and-remove the pending record; ``None`` when there is none
    (``GETDEL`` — a concurrent duplicate answer POST gets ``None``)."""
    async with _redis() as redis:
        raw = await redis.getdel(_question_key(interaction_id))
    if raw is None:
        return None
    return _decode_record(raw)


async def restore_question(interaction_id: str, record: QuestionRecord, max_restores: int) -> bool:
    """Put a claimed record back with its remaining TTL after a failed forward, and
    report whether it is answerable again.

    A ``SET NX``: if a NEW question reserved the id in the gap it is not clobbered
    (refused with a loud log); a record past its deadline is not restored.

    ``max_restores`` bounds the loop: every restore is one more forward a visitor can
    make the server pay for, and the callback door's own rate limit is keyed on this
    server's egress IP and shared with every other channel. Past the cap the record
    stays dropped — loudly — and the interaction resolves by its own timeout."""
    if record.restores >= max_restores:
        logger.error(
            "not restoring the pending question %s: its answer has already been refused %d times "
            "(cap %d); the question is dropped and will resolve by its own timeout",
            interaction_id,
            record.restores,
            max_restores,
        )
        return False
    remaining = math.ceil((record.timeout_at.astimezone(UTC) - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        return False
    restored = replace(record, restores=record.restores + 1)
    async with _redis() as redis:
        stored = await redis.set(_question_key(interaction_id), _encode_record(restored), nx=True, ex=remaining)
    if not stored:
        logger.error(
            "could not restore the pending question %s: a new reservation has since taken the id; "
            "the old ask will resolve by its own timeout",
            interaction_id,
        )
        return False
    return True
