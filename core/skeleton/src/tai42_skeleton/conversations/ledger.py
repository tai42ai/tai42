"""The channel send ledger — keyspace 5 of the conversation bridge.

``conversations:progress:{message_id}`` → an append-only list, one entry per answer chunk
the provider has already ACCEPTED, in send order. The medium offers no idempotency key, so
re-sending an accepted chunk duplicates a message to a human; appending BEFORE the next
chunk goes out is what lets a re-drive send only the remainder.

An entry records the chunk's ANSWER-PART index and its CHARACTER COUNT, not its chunk
index, so the resume point survives a change to the channel's ``max_message_chars``
between attempts. A single-message answer is part 0; a multi-message answer chunks each
part independently, so the ``part`` field is what tells one part's chunks from the next's.
An entry missing ``part`` (written before this field existed) reads as part 0 — a
pre-upgrade record was single-part by construction, so that is correct. It also carries the
provider ids that chunk produced.

Send-time scaffolding, not durable state: deleted at a provisional or terminal outcome, and
carrying the answer-retention TTL so any other ending leaves nothing behind.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations.errors import NotSupportedError
from tai42_skeleton.utils.redis_typing import awaited

_NO_BACKEND = "channel send progress requires the redis conversations backend"


class LedgerInconsistentError(RuntimeError):
    """A ledger that cannot describe the answer — an unparseable entry, or totals that
    contradict the answer. Corrupt state a re-drive can never resume from, distinct from a
    transient store fault, which propagates untouched."""


@dataclass(frozen=True)
class SentChunk:
    """One chunk a channel provider has already accepted: how many characters it carried, the
    outbound ids the provider assigned it, and which answer PART it belongs to. ``part``
    defaults to 0 — a single-part answer, and the value a pre-upgrade entry (which predates
    multi-part answers) reads as."""

    chars: int
    outbound_ids: list[str]
    part: int = 0


def _decode(entry: bytes | str) -> SentChunk:
    """Parse one stored ledger entry. A malformed entry raises
    :class:`LedgerInconsistentError` rather than reading as "nothing sent", which would
    re-send chunks the provider already accepted. A ``part`` absent from the entry reads as
    0 — a pre-upgrade entry predates multi-part answers and named a single-part answer."""
    try:
        payload = json.loads(entry)
        return SentChunk(
            chars=int(payload["chars"]),
            outbound_ids=[str(i) for i in payload["outbound_ids"]],
            part=int(payload.get("part", 0)),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise LedgerInconsistentError(f"channel send ledger entry {entry!r} is unparseable") from exc


class ChannelSendLedger:
    """The append-only per-record channel send ledger (keyspace 5). Construction refuses
    without the redis conversations backend — progress lost with the process could only be
    resumed by re-sending from the first chunk."""

    def __init__(self, settings: ConversationsSettings) -> None:
        if settings.in_memory:
            raise NotSupportedError(_NO_BACKEND)
        self.settings = settings

    async def sent_chunks(self, message_id: str) -> list[SentChunk]:
        """Every chunk already accepted for ``message_id``, in send order. Empty for a
        send that has not started, and for one whose ledger has been cleared."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            entries = await awaited(r.lrange(self.settings.chunk_ledger_key(message_id), 0, -1))
        return [_decode(entry) for entry in entries]

    async def append(self, message_id: str, chars: int, outbound_ids: list[str], part: int = 0) -> None:
        """Record that a chunk of ``chars`` characters of answer ``part`` was accepted,
        producing ``outbound_ids``. MUST be called before the next chunk is sent, so at most
        the one chunk in flight can ever be re-sent. ``part`` defaults to 0 — a single-part
        answer's only part."""
        key = self.settings.chunk_ledger_key(message_id)
        entry = json.dumps({"part": part, "chars": chars, "outbound_ids": outbound_ids})
        async with client_ctx(RedisClient, self.settings.redis) as r:
            await awaited(r.rpush(key, entry))
            await awaited(r.expire(key, self.settings.answer_retention_ttl_seconds))

    async def clear(self, message_id: str) -> None:
        """Drop the ledger for a record whose send is over; the record itself now carries
        the full outbound id list."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            await awaited(r.delete(self.settings.chunk_ledger_key(message_id)))


__all__ = ["ChannelSendLedger", "LedgerInconsistentError", "SentChunk"]
