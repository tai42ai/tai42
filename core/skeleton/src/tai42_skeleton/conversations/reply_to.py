"""Resolve the message an inbound reply quotes into a :class:`~tai42_contract.conversations.ReplyTo`.

The quote is read from the conversation record, never carried by a medium: a channel names the
quoted message by its medium id, the API door by the platform's record ``message_id``, and an
asker by the medium id the answering reply quoted. Only a record in the same thread is a quote,
so a thread's forget removes both the quoted message and every copy of its text.

Three entry points find the records; one core pairs each kept record with the text delivered or
received under the matched id and decides the outcome:

* any non-blank text is ``found``, the texts joined by a blank line (one medium message bridged
  as several records quotes as the whole message);
* else a reply to a sensitive question is ``sensitive`` (its body is never kept);
* else a held message with no text is ``no_text``;
* else ``not_found``, logged at DEBUG with the channel, the record ids the index returned and how
  many the thread check dropped, never an address or a text.

A store error propagates, so the door's own error handling applies.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import TYPE_CHECKING

from tai42_contract.conversations import ReplyTo, ReplyToStatus

from tai42_skeleton.conversations.cache import get_conversations_manager
from tai42_skeleton.conversations.settings import conversations_settings

if TYPE_CHECKING:
    from tai42_skeleton.conversations.models import ConversationRecord
    from tai42_skeleton.conversations.records import ConversationRecordStore

logger = logging.getLogger(__name__)


class _Mark(Enum):
    """What a held message stands for when it gives no text."""

    SENSITIVE = "sensitive"
    NO_TEXT = "no_text"


def _sent_text(record: ConversationRecord, part: int) -> str:
    """The text of answer part ``part`` the record sent: its ``answer`` when it has one part."""
    if record.answer_parts:
        return record.answer_parts[part].message
    return record.answer or ""


def _ask_reply_text(answer: object) -> str:
    """An ask reply's answer as text: a string as is, any other value as its compact JSON."""
    if isinstance(answer, str):
        return answer
    return json.dumps(answer, ensure_ascii=False)


def _paired(record: ConversationRecord, medium_message_id: str) -> list[str | _Mark]:
    """Every text ``record`` holds under ``medium_message_id``, or the mark that stands for one."""
    paired: list[str | _Mark] = []
    if record.inbound_message_id == medium_message_id:
        paired.append(record.inbound_text)
    reply = record.ask_reply
    if reply is not None and reply.medium_message_id == medium_message_id:
        if record.ask is not None and record.ask.sensitive:
            paired.append(_Mark.SENSITIVE)
        else:
            paired.append(_ask_reply_text(reply.answer))
    paired.extend(
        _sent_text(record, sent.part) for sent in record.sends if medium_message_id in sent.provider_message_ids
    )
    return [_Mark.NO_TEXT if isinstance(text, str) and not text.strip() else text for text in paired]


def _outcome(message_id: str, paired: list[str | _Mark]) -> ReplyTo | None:
    """The quote the paired texts and marks add up to, or ``None`` when nothing was paired."""
    texts = [text for text in paired if isinstance(text, str)]
    if texts:
        return ReplyTo(message_id=message_id, status=ReplyToStatus.FOUND, text="\n\n".join(texts))
    if _Mark.SENSITIVE in paired:
        return ReplyTo(message_id=message_id, status=ReplyToStatus.SENSITIVE, text=None)
    if _Mark.NO_TEXT in paired:
        return ReplyTo(message_id=message_id, status=ReplyToStatus.NO_TEXT, text=None)
    return None


def _not_found(message_id: str) -> ReplyTo:
    return ReplyTo(message_id=message_id, status=ReplyToStatus.NOT_FOUND, text=None)


async def resolve_reply_to(
    store: ConversationRecordStore,
    *,
    channel: str,
    our_identity: str,
    client_address: str,
    thread_id: str,
    medium_message_id: str,
) -> ReplyTo:
    """The message a channel reply quotes, by the medium's id, from the records of ``thread_id``."""
    ids = await store.resolve_message_ref(channel, our_identity, client_address, medium_message_id)
    paired: list[str | _Mark] = []
    dropped = 0
    for record_id in ids:
        record = await store.get_record(record_id)
        if record is None:
            continue
        if record.thread_id != thread_id:
            dropped += 1
            continue
        paired.extend(_paired(record, medium_message_id))
    outcome = _outcome(medium_message_id, paired)
    if outcome is not None:
        return outcome
    logger.debug(
        "reply on channel %s quotes a message this thread does not hold: index records %s, %d in another thread",
        channel,
        ids,
        dropped,
    )
    return _not_found(medium_message_id)


async def resolve_api_reply_to(store: ConversationRecordStore, *, thread_id: str, message_id: str) -> ReplyTo:
    """The message an API message replies to, by the platform's ``message_id``: the answer delivered under it.

    A record of another thread is ``not_found``. A record with no text delivered under it (no
    outcome yet, an answerless outcome, an answer of media only) is ``no_text``. The record's own
    inbound text is never the quote.
    """
    record = await store.get_record(message_id)
    if record is None or record.thread_id != thread_id:
        logger.debug("api message replies to record %s, which this thread does not hold", message_id)
        return _not_found(message_id)
    if record.answer is None or not record.answer.strip():
        return ReplyTo(message_id=message_id, status=ReplyToStatus.NO_TEXT, text=None)
    return ReplyTo(message_id=message_id, status=ReplyToStatus.FOUND, text=record.answer)


async def resolve_ask_reply_to(interaction_id: str, medium_message_id: str) -> ReplyTo:
    """The message the reply that answered ask ``interaction_id`` quotes, from the ask's thread.

    The ask's send record names the channel, identity, address and thread the quote is looked up
    in. With no conversations store, or no send record for the ask (sent outside any conversation,
    its thread forgotten, or its record expired), the quote is ``not_found``.
    """
    if conversations_settings().in_memory:
        logger.debug("ask %s: no conversations store holds the message its answer quotes", interaction_id)
        return _not_found(medium_message_id)
    store = get_conversations_manager().records
    send_id = await store.ask_send_message_id(interaction_id)
    record = None if send_id is None else await store.get_record(send_id)
    if record is None:
        logger.debug("ask %s: no send record holds the thread its answer quotes from", interaction_id)
        return _not_found(medium_message_id)
    if record.channel is None or record.our_identity is None:
        raise ValueError(f"send record {record.message_id} of ask {interaction_id} carries no channel and our_identity")
    return await resolve_reply_to(
        store,
        channel=record.channel,
        our_identity=record.our_identity,
        client_address=record.client_address,
        thread_id=record.thread_id,
        medium_message_id=medium_message_id,
    )


__all__ = ["resolve_api_reply_to", "resolve_ask_reply_to", "resolve_reply_to"]
