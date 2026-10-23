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
    """The newest record of ``person``'s aggregated thread ``thread_id`` that is not a send record.

    A send record (``origin`` ``"send"``) marks where the deployment sent to, not where the person
    last wrote from, so it is skipped. The merged index is read newest first in batches of
    ``thread_queue_depth`` until such a record is found or the index ends. ``None`` when none is
    left; each caller raises its own error on ``None``.
    """
    routes = sorted(person_routes(person))
    depth = store.settings.thread_queue_depth
    offset = 0
    while True:
        page, _unreadable = await store.list_person_thread_records(
            routes, thread_id, offset=offset, limit=depth, newest_first=True
        )
        for record in page.records:
            if record.origin != "send":
                return record
        offset += depth
        if offset >= page.total:
            return None
