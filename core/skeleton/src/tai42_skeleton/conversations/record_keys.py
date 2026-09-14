"""Framework-free member decoding, thread-id parsing and route-resume helpers the record
store's reads and prune passes share — no Redis client, no settings, pure functions."""

from __future__ import annotations

from tai42_skeleton.agent.thread_reservation import BRIDGE_THREAD_PREFIX, PERSON_THREAD_PREFIX
from tai42_skeleton.conversations.models import ConversationRecord


def _member(value: str | bytes) -> str:
    """A sorted-set member as text, whichever form the client is decoding into."""
    return value.decode() if isinstance(value, bytes) else value


def _thread_address_suffix(thread_id: str, route_name: str) -> str | None:
    """The client-address suffix a route-keyed thread id carries
    (``bridge:{route_name}:{client_address}`` → ``client_address``), or ``None`` for a person
    thread (``bridge:@person:{id}`` carries no address to match)."""
    if thread_id.startswith(PERSON_THREAD_PREFIX):
        return None
    prefix = f"{BRIDGE_THREAD_PREFIX}{route_name}:"
    if thread_id.startswith(prefix):
        return thread_id[len(prefix) :]
    return None


def _record_matches(record: ConversationRecord, needle: str) -> bool:
    """Whether ``needle`` (already lower-cased) occurs in the record's inbound text or its
    answer — the two fields a conversation text search reads."""
    if needle in record.inbound_text.lower():
        return True
    return record.answer is not None and needle in record.answer.lower()


def _resume_at(route_names: list[str], resume: str | None) -> list[str]:
    """``route_names`` rotated so the route the last pass stopped in comes first, and the
    routes it already walked are re-reached at the end of this one. An unknown (deleted)
    resume point falls back to the given order."""
    if resume is None or resume not in route_names:
        return route_names
    at = route_names.index(resume)
    return route_names[at:] + route_names[:at]
