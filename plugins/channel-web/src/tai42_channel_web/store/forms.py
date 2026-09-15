"""Ask-less form submission records: read (never claimed) on every submission."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from tai42_channel_web.settings import web_settings
from tai42_channel_web.store.connection import _mint_id, _redis


@dataclass(frozen=True)
class FormRecord:
    """One ask-less form card's submission record.

    Holds the transcript pair the card was appended to (what binds a submission to the
    conversation it was sent into), the form's answer schema (the server-trusted source of the
    rendered labels), and the card's prompt message.

    Stored under ``channel:web:form:{token}`` with a TTL of the transcript TTL: the
    card lives in the replay buffer, so its answerability ages out with it. The
    record is only ever READ — a form is submittable again and again (each
    submission is its own participant message), unlike a question's one-shot claim.
    """

    identity: str
    address: str
    schema: dict[str, Any]
    message: str


def _form_key(token: str) -> str:
    return f"channel:web:form:{token}"


async def store_form_record(record: FormRecord) -> str:
    """Store one form card's submission record under a freshly minted token and return that token.

    The token is minted HERE, server-side (``uuid4().hex``) — never caller-supplied,
    so no sender can choose (or collide) a submission door's name. The TTL is the
    transcript TTL: the card sits in the replay buffer for at most that long, and a
    submission against a card that has aged out of every replay must refuse.
    """
    token = _mint_id()
    payload = json.dumps(
        {
            "identity": record.identity,
            "address": record.address,
            "schema": record.schema,
            "message": record.message,
        }
    )
    async with _redis() as redis:
        await redis.set(_form_key(token), payload, ex=web_settings().transcript_ttl_seconds)
    return token


async def read_form_record(token: str) -> FormRecord | None:
    """Return the record a form token stands for, or ``None`` when it is unknown or expired.

    Unknown and expired are indistinguishable on purpose (the door refuses both uniformly). A
    pure read: submission never claims the record (resubmission is allowed).
    """
    async with _redis() as redis:
        raw = await redis.get(_form_key(token))
    if raw is None:
        return None
    data = json.loads(raw)
    return FormRecord(
        identity=data["identity"], address=data["address"], schema=data["schema"], message=data["message"]
    )
