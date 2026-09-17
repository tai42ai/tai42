"""The pending-message seam: the thread's participant messages accepted after a given one.

A body running inside a conversation turn asks this — through ``tai42_app.conversations`` — to
learn whether a newer message is waiting, so it can yield before an irreversible step. It reads
the same thread index the turn engine batches over (``accepted_after``), so the seam and the
batch gather share ONE implementation and can never disagree on what is pending.
"""

from __future__ import annotations

from tai42_contract.app import PendingMessage

from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings


async def pending_messages(thread_id: str, *, after: str) -> list[PendingMessage]:
    """The thread's ``accepted`` participant messages accepted after ``after``, in acceptance order.

    ``after`` is a message_id — the asking turn's own lead. Its record fixes the thread's route and
    the acceptance moment to read past; a strictly-later ``accepted`` participant ``message`` record
    on ``thread_id`` becomes a :class:`PendingMessage` carrying its verbatim ``inbound_text`` and its
    thread-index score (the epoch seconds it was accepted at). An ``after`` that names no record — an
    unknown thread, or a lead that already left ``accepted`` — reads as an empty list.
    """
    store = ConversationRecordStore(ConversationsSettings())
    after_record = await store.get_record(after)
    if after_record is None:
        return []
    records = await store.accepted_after(after_record.route_name, thread_id, after_record.created_at)
    return [
        PendingMessage(message_id=record.message_id, text=record.inbound_text, accepted_at=record.created_at)
        for record in records
    ]
