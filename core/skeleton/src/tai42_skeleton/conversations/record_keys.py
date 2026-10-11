"""Framework-free member decoding, thread-id parsing and route-resume helpers the record store shares.

Shared by the record store's reads and prune passes — no Redis client, no
settings, pure functions.
"""

from __future__ import annotations

from tai42_skeleton.conversations.models import ConversationRecord
from tai42_skeleton.conversations.thread_ids import RouteThreadId, ThreadIdError, parse_thread_id


def _member(value: str | bytes) -> str:
    """A sorted-set member as text, whichever form the client is decoding into."""
    return value.decode() if isinstance(value, bytes) else value


def _thread_address_suffix(thread_id: str, route_name: str) -> str | None:
    """The client-address suffix a thread id of ``route_name`` carries, or ``None``.

    A ``bridge:{route_name}:{client_address}`` id yields ``client_address``; a person thread, a
    thread of another route and an id outside the grammar carry no address of this route to match.
    """
    try:
        parsed = parse_thread_id(thread_id)
    except ThreadIdError:
        return None
    if isinstance(parsed, RouteThreadId) and parsed.route_name == route_name:
        return parsed.client_address
    return None


def _record_matches(record: ConversationRecord, needle: str) -> bool:
    """Whether ``needle`` (already lower-cased) occurs in the record's inbound text or its answer.

    The two fields a conversation text search reads.
    """
    if needle in record.inbound_text.lower():
        return True
    return record.answer is not None and needle in record.answer.lower()


def _resume_at(route_names: list[str], resume: str | None) -> list[str]:
    """``route_names`` rotated so the route the last pass stopped in comes first.

    The routes it already walked are re-reached at the end of this one. An
    unknown (deleted) resume point falls back to the given order.
    """
    if resume is None or resume not in route_names:
        return route_names
    at = route_names.index(resume)
    return route_names[at:] + route_names[:at]
