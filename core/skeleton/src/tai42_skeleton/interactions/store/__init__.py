"""Redis store for the interactions capability.

Holds every key shape and the read/write operations behind one class so the
producer (the ``ask_user`` helper in this package) and the consumer (the API SSE
+ answer endpoint) share the exact key contract. Operations take the redis
client as an argument: each caller opens it from the interactions settings via
``client_ctx(RedisClient, settings.redis)``.

Requires Redis server >= 7.0: ``add`` sets-or-extends the group stream and
``count_key`` TTLs with ``EXPIRE ... NX`` + ``EXPIRE ... GT`` (Redis 7.0+) and
extends the pending-deadline index with ``ZADD ... GT`` (Redis 6.2+). Against an
older server these commands error loudly (a visible break, never a silent degrade).

Assumes a single-node Redis (not Redis Cluster): the phantom-purge Lua drops
per-group index members read at runtime rather than from ``KEYS`` (the number of
expired groups is variable), which a Cluster would reject as an undeclared-key
access. The interactions keys share one prefix and are not hash-tag co-located,
so single-node is the operating assumption.

Loud by contract — no swallowed errors, no silent fallback.
"""

from __future__ import annotations

from . import scripts, ttl
from .events import ADD_EVENT, ANSWERED_EVENT, REMOVED_EVENT
from .reads import _StoreReads
from .records import CONTINUATION_DROPPED, ContinuationDue, ContinuationRetryDrop
from .serde import as_str
from .writes import PruneResult, _StoreWrites


class InteractionStore(_StoreWrites, _StoreReads):
    """The interactions store: the durable question lifecycle (writes) and the
    read/query/audit + reaper claims (reads), reassembled over the shared Redis key
    contract into the one class every ``store.<method>`` call site uses."""


__all__ = [
    "ADD_EVENT",
    "ANSWERED_EVENT",
    "CONTINUATION_DROPPED",
    "REMOVED_EVENT",
    "ContinuationDue",
    "ContinuationRetryDrop",
    "InteractionStore",
    "PruneResult",
    "as_str",
    "scripts",
    "ttl",
]
