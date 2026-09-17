"""The answer/record store — keyspaces 1, 2, 3, 6, 7, 8 and 9 of the conversation bridge.

All transient runtime state (NOT a backup section) and all Redis-backed:

1. Inbound dedupe: ``conversations:dedupe:{channel}:{provider_message_id}`` → the
   ``message_id`` that first claimed the pair, plus its event sibling
   ``conversations:event-dedupe:{route_name}:{event_id}`` → the ``message_id`` that first
   claimed an event turn. A distinct family, so a channel provider id and an event id
   sharing a value never collide on one marker. Neither claim has a release path, so each
   is taken only once a durable record already stands behind it.
2. Answer record: ``conversations:record:{message_id}`` — intake, produced answer and
   delivery state, split into a content blob plus the delivery-control fields the atomic
   transitions mutate. The retention TTL is applied ONLY on reaching a terminal state.
3. Outbound-id reverse index: ``conversations:outbound:{channel}:{outbound_id}`` →
   ``message_id``, so an out-of-band receipt resolves back to its record.
6. Per-status record index: ``conversations:status:{delivery_status}`` → the ``message_id``s
   in that state, moved by the same atomic step that moves the record. Every listing reads
   it, so a sweep costs the work outstanding and not the whole retained keyspace.
7. Per-thread transcript index: ``conversations:thread:{route_name}:{thread_id}`` → the
   thread's ``message_id``s scored by ``created_at``, written by the same atomic step that
   creates the record, so a transcript reads in the order the messages were sent.
8. Per-route thread index: ``conversations:route_threads:{route_name}`` → the route's
   ``thread_id``s scored by the moment each was last active, so the monitoring listing
   reads the newest first.
9. Owed first-contact greeting: ``conversations:overlap:greeting:{thread_id}`` → the rendered
   greeting a thread still owes, parked when it is minted and burned by the first turn that
   delivers a reply, so a greeting whose minting turn was superseded rides the successor turn.

Indexes 7 and 8 name rows that expire under the retention TTL. Reclaiming those members is
:meth:`ConversationRecordStore.prune_expired_terminal_indexes`'s job ALONE: a read logs the
orphan it walks over and returns the page it was asked for, because a read that mutated the
index it is paging would drop rows out from under its own offsets and compute ``next_page``
from a total it had already changed.

That pass walks LIVE routes only, so nothing may leave either index standing for a route
that no longer routes: a create writes them only while the routing row stands (keyspace 4,
the one key here read and never written), a completion re-stamps index 8 only while index 7
still holds members, and a route's delete reclaims both — resumably, index 8 being the
durable marker of a reclamation that was interrupted.

Every exactly-once transition is a single Lua step guarded on the record's current
``delivery_status``, so racing writers produce ONE outcome, never two.

The ~1140-line store is composed from one persistence-concern mixin per keyspace; the
concerns share only :attr:`ConversationRecordStore.settings` and the tunable scan/prune
budgets homed here, which they read THROUGH this module so a test's ``setattr`` on the budget
or on ``client_ctx`` reaches every concern.
"""

from __future__ import annotations

from tai42_kit.clients import client_ctx as client_ctx

from tai42_skeleton.conversations.record_dedupe import RecordDedupeMixin
from tai42_skeleton.conversations.record_delivery_state import RecordDeliveryMixin
from tai42_skeleton.conversations.record_greeting import RecordGreetingMixin
from tai42_skeleton.conversations.record_index import RecordIndexMixin
from tai42_skeleton.conversations.record_pages import PendingWork, ThreadPage, ThreadSummary, TranscriptPage
from tai42_skeleton.conversations.record_prune import PRUNE_START, PruneCursor, RecordPruneMixin
from tai42_skeleton.conversations.record_query import RecordQueryMixin
from tai42_skeleton.conversations.record_write import RecordWriteMixin
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations.errors import NotSupportedError

_NO_BACKEND = "conversation answer records require the redis conversations backend"

#: Newest members of a thread the listing will read for a summary before giving up on it.
#: A thread whose newest members are all rowless is being reclaimed, not displayed.
_THREAD_SUMMARY_SCAN = 20

#: Candidate threads a FILTERED route-thread listing examines before it reports the page
#: LOUDLY truncated. There is no per-route+status index, so a status/address filter is an
#: app-side post-scan and each candidate costs a summary read; this bounds it to a fixed page.
_FILTER_THREAD_SCAN = 500

#: Candidate records a transcript ``q`` search — or a route-scoped message search — examines
#: before it reports the page LOUDLY truncated. Each candidate costs one record read (a full
#: HGETALL + parse, since the searched text lives inside the JSON content blob).
_FILTER_RECORD_SCAN = 1_000

#: Threads or records a filter scan reads in ONE ranged command, so no single reply carries a
#: whole route or thread.
_FILTER_SCAN_WINDOW = 200

#: Members of one thread's index, and threads of one route's index, a prune pass reads in
#: ONE command. Both bound a single reply, so no pass ever asks Redis for a whole index.
_PRUNE_MEMBERS_PER_BATCH = 200
_PRUNE_THREADS_PER_BATCH = 200

#: Units of work one prune pass spends. A thread costs one unit to EXAMINE plus one per
#: candidate member it offers, so a route of a hundred thousand HEALTHY threads is bounded
#: too; what is left over drains on the passes that follow.
_PRUNE_WORK_PER_PASS = 5_000


class ConversationRecordStore(
    RecordDedupeMixin,
    RecordWriteMixin,
    RecordDeliveryMixin,
    RecordIndexMixin,
    RecordQueryMixin,
    RecordPruneMixin,
    RecordGreetingMixin,
):
    """The Redis-backed answer/record store (keyspaces 1, 2, 3, 6, 7, 8 and 9), one persistence concern per mixin.

    Construction refuses with a loud 501 without the redis conversations backend — nothing here may be
    persisted to state that vanishes with the process.
    """

    def __init__(self, settings: ConversationsSettings) -> None:
        """Bind the conversations ``settings``, refusing a loud 501 when no Redis backend is configured."""
        if settings.in_memory:
            raise NotSupportedError(_NO_BACKEND)
        self.settings = settings


__all__ = [
    "PRUNE_START",
    "ConversationRecordStore",
    "PendingWork",
    "PruneCursor",
    "ThreadPage",
    "ThreadSummary",
    "TranscriptPage",
]
