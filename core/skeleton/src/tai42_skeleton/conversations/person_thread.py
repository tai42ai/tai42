"""Reads over a LINKED person's aggregated thread: the routes it spans and its newest record."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_contract.conversations import Person

    from tai42_skeleton.conversations.models import ConversationRecord
    from tai42_skeleton.conversations.records import ConversationRecordStore


def person_routes(person: Person) -> set[str]:
    """Every route name the person has written under, straight off its address rows.

    The routes whose indexes the aggregated transcript spans and the authz checks read.
    """
    return {route for address in person.addresses for route in address.routes}


async def newest_person_thread_record(
    store: ConversationRecordStore, person: Person, thread_id: str
) -> ConversationRecord | None:
    """The newest record of ``person``'s aggregated thread ``thread_id``, or ``None`` when it holds none.

    Each caller raises its own error on ``None``.
    """
    newest, _unreadable = await store.list_person_thread_records(
        sorted(person_routes(person)), thread_id, offset=0, limit=1, newest_first=True
    )
    return newest.records[0] if newest.records else None
