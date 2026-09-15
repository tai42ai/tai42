"""The four atomic server-side Lua scripts the store's writes and reads run.

Each folds a read-and-act sequence into one round trip so a concurrent caller can never observe or corrupt
a torn intermediate state.
"""

from __future__ import annotations

# Atomic phantom self-heal for the pending-deadline index. A waiter killed
# mid-flight (SIGKILL/OOM) never runs cleanup, so its group lingers in
# ``pending_key``. This script — run on every ``add`` — reads the groups whose
# furthest question deadline has passed and, per expired group, drops it from BOTH
# the pending index and the parallel deadline index. It leaves ``count_key``
# untouched: the count is set-or-extended to the group's TTL (see ``add``), so a
# surviving state always keeps a live count and a genuinely-dead group's count
# expires on that same basis — death and revival stay symmetric, and a group that
# revives after a purge cannot re-seed a torn count. Reading the expired set
# INSIDE the script makes the correlated multi-index delete atomic: a concurrent
# ``add`` that revives a group (later deadline via ``ZADD GT``, re-added to
# ``pending_key``) between a would-be read and delete cannot be wrongly purged,
# because the script re-reads the current deadline index rather than acting on a
# stale snapshot.
#
# The group of the ``add`` running this purge is SKIPPED: this call is about to
# make that group live (its future deadline is not recorded via ``ZADD GT`` until
# the pipeline that follows the purge), so scanning it as "expired" and purging it
# would drop a group that is gaining a live question — invariant (b). The
# phantom self-heal of a genuinely dead group still fires, driven by any UNRELATED
# ``add``.
#   KEYS[1] = pending_deadline_key,  KEYS[2] = pending_key  # noqa: ERA001 (Lua param docs)
#   ARGV[1] = now_ms (purge cutoff),  ARGV[2] = group to skip (the current add's group)
_PENDING_PURGE_LUA = """
-- interactions:pending-deadline-purge
local current = ARGV[2]
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], 0, ARGV[1])
local purged = 0
for _, group in ipairs(expired) do
    if group ~= current then
        redis.call('ZREM', KEYS[2], group)
        redis.call('ZREM', KEYS[1], group)
        purged = purged + 1
    end
end
return purged
"""

# Atomic reserve-and-check for the ``max_concurrent`` cap. The open index carries
# no TTL, so a SIGKILLed waiter's member lingers; this script first purges every
# member whose deadline has passed, then admits the caller — adding its open-index
# member — ONLY while the live count is below ``limit``. ZCARD and the ZADD run in
# one server round trip, so a concurrent burst can never overshoot the cap the way
# a separate count-then-add pair can (the check-then-act gap between two commands).
#   KEYS[1] = open_key  # noqa: ERA001 (Lua param docs)
#   ARGV[1] = now_ms (stale-member cutoff),  ARGV[2] = limit,
#   ARGV[3] = timeout_at_ms (member score),  ARGV[4] = interaction_id (member)
_OPEN_RESERVE_LUA = """
-- interactions:open-slot-reserve
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
if tonumber(redis.call('ZCARD', KEYS[1])) >= tonumber(ARGV[2]) then
    return 0
end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
return 1
"""


# Set-or-extend the group media index and every member media key to the group's TTL,
# queued into the add's write pipeline so it commits atomically with the question. Reads
# the current index members, SADDs the add's new ids, then — over the union — sets the
# index and each ``media:{id}`` key to the horizon via
# ``EXPIRE ... NX`` (set when a key has none yet) + ``EXPIRE ... GT`` (raise only when
# longer), the same SET-OR-EXTEND-TO-GREATER discipline the group stream and count use.
# So a co-grouped long park keeps the whole group's media alive and a later short add
# never shrinks it. Like the phantom purge, it constructs the per-member ``media:{id}``
# keys from members read at runtime — a single-node access (undeclared for Cluster),
# consistent with this store's single-node assumption.
#   KEYS[1] = media_index_key  # noqa: ERA001 (Lua param docs)
#   ARGV[1] = ttl seconds,  ARGV[2] = media_key prefix,  ARGV[3..] = the add's new ids
_MEDIA_SET_OR_EXTEND_LUA = """
-- interactions:media-set-or-extend
local ttl = tonumber(ARGV[1])
local prefix = ARGV[2]
local members = redis.call('SMEMBERS', KEYS[1])
local seen = {}
for _, id in ipairs(members) do seen[id] = true end
for i = 3, #ARGV do
    local id = ARGV[i]
    if not seen[id] then
        redis.call('SADD', KEYS[1], id)
        members[#members + 1] = id
        seen[id] = true
    end
end
if #members > 0 then
    redis.call('EXPIRE', KEYS[1], ttl, 'NX')
    redis.call('EXPIRE', KEYS[1], ttl, 'GT')
    for _, id in ipairs(members) do
        local mk = prefix .. id
        redis.call('EXPIRE', mk, ttl, 'NX')
        redis.call('EXPIRE', mk, ttl, 'GT')
    end
end
return #members
"""


# Atomic retry-claim for a durable continuation-due record. The reaper's
# redelivery pass runs this per due member: it advances the record's next-attempt
# score by an exponential backoff BEFORE returning the record to fire, so a
# concurrent reaper pass reading the SAME member finds the score already pushed
# past ``now`` and declines (returns nil) — one pass re-fires per backoff window.
# At-least-once, not exactly-once: the fired continuation's consumer must be
# idempotent (an at-least-once redelivery may fire the same continuation more than
# once), so even a rare double-fire across passes is harmless. A member whose record
# hash has vanished (TTL-expired past the retention horizon) is an orphan index
# entry — reconciled off the index (ZREM) rather than re-firing a record that no
# longer exists, and returns the ``'dropped'`` marker so the caller can surface a
# LOUD terminal give-up (no further redelivery will ever fire that resume).
#   KEYS[1] = continuation_due_index_key,  KEYS[2] = continuation_due_record_key  # noqa: ERA001 (Lua param docs)
#   ARGV[1] = interaction_id (member),  ARGV[2] = now_ms (due cutoff),
#   ARGV[3] = backoff_base_ms,  ARGV[4] = backoff_cap_ms  # noqa: ERA001 (Lua param docs)
_CONTINUATION_RETRY_CLAIM_LUA = """
-- interactions:continuation-retry-claim
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score then return nil end
if tonumber(score) > tonumber(ARGV[2]) then return nil end
if redis.call('EXISTS', KEYS[2]) == 0 then
    redis.call('ZREM', KEYS[1], ARGV[1])
    return 'dropped'
end
local attempts = redis.call('HINCRBY', KEYS[2], 'attempts', 1)
local delay = tonumber(ARGV[3]) * (2 ^ (attempts - 1))
if delay > tonumber(ARGV[4]) then delay = tonumber(ARGV[4]) end
redis.call('ZADD', KEYS[1], tonumber(ARGV[2]) + delay, ARGV[1])
return redis.call('HGETALL', KEYS[2])
"""
