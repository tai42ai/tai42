"""The read-model data shapes the record store's listing and transcript doors return —
thread summaries, paged threads/records, and the pending-delivery view."""

from __future__ import annotations

from dataclasses import dataclass

from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus


@dataclass(frozen=True)
class ThreadSummary:
    """One thread as the route's thread listing shows it — its newest record's address,
    state and moment, plus how many records the thread still holds."""

    thread_id: str
    client_address: str
    last_activity_at: float
    message_count: int
    last_delivery_status: DeliveryStatus


@dataclass(frozen=True)
class ThreadPage:
    """One page of a route's threads, newest activity first, with the total the page was cut
    from — the route's indexed thread count for an unfiltered listing, or the number of
    matches a bounded filter scan FOUND. ``truncated`` is True when a filter scan spent its
    budget before the index was exhausted, so the page (and ``total``) is a bounded view and
    matches may lie beyond it — surfaced LOUDLY, never a silent cut."""

    threads: list[ThreadSummary]
    total: int
    truncated: bool = False


@dataclass(frozen=True)
class TranscriptPage:
    """One page of a thread's records in the direction the read asked for — oldest first by
    default, newest first under ``newest_first`` — with the total the page was cut from. For
    an unfiltered read that is the thread's indexed record count and a ``total`` of 0 is a
    thread the index no longer holds at all; for a ``q`` search it is the number of matches a
    bounded scan FOUND. ``truncated`` is True when a search spent its budget before the index
    was exhausted, so matches may lie beyond the page — surfaced LOUDLY, never a silent cut."""

    records: list[ConversationRecord]
    total: int
    truncated: bool = False


@dataclass(frozen=True)
class PendingWork:
    """A non-terminal record a delivery pass found, with the control fields it needs to
    decide the next move."""

    message_id: str
    delivery_status: DeliveryStatus
    attempts: int
    grace_deadline: float | None
