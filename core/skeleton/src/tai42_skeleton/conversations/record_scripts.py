"""The atomic Redis Lua steps for record transitions, their step builders, and the layout constants.

Each exactly-once transition is a single Lua step guarded on the record's current
``delivery_status``, so racing writers produce ONE outcome, never two.
"""

from __future__ import annotations

from tai42_skeleton.conversations.models import DeliveryStatus

# Hash field names on a record key: ``data`` is the content JSON, the rest are the
# delivery-control fields the atomic transitions mutate.
_F_DATA = "data"
_F_STATUS = "delivery_status"
_F_OUTBOUND = "outbound_ids"
_F_ATTEMPTS = "attempts"
_F_GRACE = "grace_deadline"
_F_UPDATED = "updated_at"
_F_INTAKE = "intake_claim"

# Every record-mutating script takes the same key layout: KEYS[1]=record key, then one
# key per DeliveryStatus (the per-status indexes), then the index the transition moves the
# record INTO. The status count is derived below, so the layout tracks the enum. A script
# that also touches the thread indexes carries them LAST — one slot earlier in the delete,
# which writes no target index — and the create carries the ROUTING ROW behind them, the
# key it decides against whether the route still routes.
_INDEXED_STATUSES = tuple(DeliveryStatus)
_TARGET_INDEX_KEY = f"KEYS[{2 + len(_INDEXED_STATUSES)}]"
_THREAD_INDEX_KEY = f"KEYS[{3 + len(_INDEXED_STATUSES)}]"
_ROUTE_THREADS_KEY = f"KEYS[{4 + len(_INDEXED_STATUSES)}]"
_ROUTE_ROW_KEY = f"KEYS[{5 + len(_INDEXED_STATUSES)}]"
_DELETE_THREAD_INDEX_KEY = f"KEYS[{2 + len(_INDEXED_STATUSES)}]"
_DELETE_ROUTE_THREADS_KEY = f"KEYS[{3 + len(_INDEXED_STATUSES)}]"

# A live record's index member outlives nothing; a terminal one expires with its row.
_NO_EXPIRY_SCORE = "+inf"


def _reindex(member_argv: str, score_argv: str) -> str:
    """Lua moving the record's id into the target status index and out of every other.

    So exactly one index names it and a listing never walks the record keyspace.
    """
    return f"""
for i = 2, {1 + len(_INDEXED_STATUSES)} do redis.call('ZREM', KEYS[i], {member_argv}) end
redis.call('ZADD', {_TARGET_INDEX_KEY}, {score_argv}, {member_argv})
"""


# Atomic get-or-set of the inbound-dedupe marker: returns the message_id owning the pair —
# the caller's on a fresh claim, the prior turn's on a redelivery.
# KEYS[1]=dedupe key; ARGV = message_id, ttl_seconds.
_CLAIM_INBOUND_LUA = """
-- conversations:dedupe:claim
local existing = redis.call('GET', KEYS[1])
if existing then return existing end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return ARGV[1]
"""

# Write a freshly minted record, applying the retention TTL in the SAME step when it is
# created already terminal so such a record can never be left without one. An ``accepted``
# record is created ALREADY holding its intake lease, so no window exists in which a live
# turn looks stranded. The thread indexes are written in the same step, so no record can
# stand outside the transcript it belongs to — but ONLY while the routing row still stands:
# a door resolves its route a round trip before it lands here, and a delete completing in
# that window has already reclaimed both indexes, so writing them would re-create a pair
# for a route that no longer routes. Returns 1 when the thread indexes were written, 0 when
# the route was gone. ARGV = content_json, delivery_status, outbound_json, attempts,
# updated_at, ttl_ms ('' for a record that must not expire yet), intake_claim ('' off the
# intake path), message_id, index_score, thread_id, created_at.
_CREATE_LUA = f"""
-- conversations:record:create
redis.call('HSET', KEYS[1], 'data', ARGV[1], 'delivery_status', ARGV[2], 'outbound_ids', ARGV[3],
  'attempts', ARGV[4], 'claim', '', 'grace_deadline', '', 'updated_at', ARGV[5], 'intake_claim', ARGV[7])
if ARGV[6] ~= '' then redis.call('PEXPIRE', KEYS[1], ARGV[6]) end
{_reindex("ARGV[8]", "ARGV[9]")}
if redis.call('EXISTS', {_ROUTE_ROW_KEY}) == 0 then return 0 end
redis.call('ZADD', {_THREAD_INDEX_KEY}, ARGV[11], ARGV[8])
redis.call('ZADD', {_ROUTE_THREADS_KEY}, ARGV[11], ARGV[10])
return 1
"""

# Re-stamp the thread's last-activity moment on the route index, but ONLY while the
# thread's own index still holds members. A route delete reclaims both indexes; a turn that
# was already in flight completes after it, and an unconditional stamp would re-create the
# route index holding a thread whose transcript index is empty — a pair no read reaches, no
# TTL expires and the prune (which walks LIVE routes only) never sees.
_RESTAMP_LIVE_THREAD = f"""
if redis.call('ZCARD', {_THREAD_INDEX_KEY}) > 0 then
  redis.call('ZADD', {_ROUTE_THREADS_KEY}, ARGV[2], ARGV[{{slot}}])
end
"""

# Move an intake record from ``accepted`` to ``pending_delivery`` carrying the turn's
# outcome, releasing the intake lease: 1 transitioned, 0 no longer at intake, -1 gone.
# Guarded on the current status, so a finishing turn and a re-drive produce ONE outcome.
# ARGV = content_json, now, message_id, index_score, thread_id.
_COMPLETE_TURN_LUA = f"""
-- conversations:record:complete_turn
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status ~= 'accepted' then return 0 end
redis.call('HSET', KEYS[1], 'data', ARGV[1], 'delivery_status', 'pending_delivery', 'updated_at', ARGV[2],
  'intake_claim', '')
{_reindex("ARGV[3]", "ARGV[4]")}
{_RESTAMP_LIVE_THREAD.format(slot=5)}
return 1
"""

# Move an intake record from ``accepted`` straight to terminal ``silent`` — a tool turn
# whose reply mapped to nothing, so nothing is ever sent — releasing the intake lease and
# applying the retention TTL in the SAME step: 1 transitioned, 0 no longer at intake, -1
# gone. Guarded on the current status, so a finishing turn and a re-drive produce ONE
# outcome. ARGV = content_json, now, ttl_ms, message_id, index_score, thread_id.
_COMPLETE_SILENT_LUA = f"""
-- conversations:record:complete_silent
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status ~= 'accepted' then return 0 end
redis.call('HSET', KEYS[1], 'data', ARGV[1], 'delivery_status', 'silent', 'updated_at', ARGV[2],
  'intake_claim', '', 'claim', '')
redis.call('PEXPIRE', KEYS[1], ARGV[3])
{_reindex("ARGV[4]", "ARGV[5]")}
{_RESTAMP_LIVE_THREAD.format(slot=6)}
return 1
"""

# Take (or refresh) the intake lease under a worker token — the liveness marker a running
# turn holds: 1 held, 0 a DIFFERENT worker's lease is still live, -1 gone, -2 the record
# has left intake. Claim value is ``token:expiry``. The holder refreshes; a re-drive may
# adopt only a LAPSED lease. KEYS[1]=record key; ARGV = now, lease_seconds, token.
_CLAIM_INTAKE_LUA = """
-- conversations:record:intake_claim
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status ~= 'accepted' then return -2 end
local claim = redis.call('HGET', KEYS[1], 'intake_claim')
local now = tonumber(ARGV[1])
if claim and claim ~= '' then
  local sep = string.find(claim, ':')
  local ctoken = string.sub(claim, 1, sep - 1)
  local cexp = tonumber(string.sub(claim, sep + 1))
  if ctoken ~= ARGV[3] and cexp > now then return 0 end
end
redis.call('HSET', KEYS[1], 'intake_claim', ARGV[3] .. ':' .. tostring(now + tonumber(ARGV[2])))
return 1
"""

# Take (or refresh) the exactly-once delivery lease under a worker token: 1 won, 0 the
# record is already sent (provisional) or terminal or a DIFFERENT worker holds a live lease,
# -1 gone, -2 still at intake and carrying no answer. Only a pending_delivery record is
# claimable for a send; a provisional one awaits a receipt, not a re-send. The holder may
# re-claim to extend; anyone else waits for expiry. Claim value is ``token:expiry``.
# KEYS[1]=record key; ARGV = now, lease_seconds, token.
_CLAIM_DELIVERY_LUA = """
-- conversations:record:claim
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status == 'accepted' then return -2 end
if status ~= 'pending_delivery' then return 0 end
local claim = redis.call('HGET', KEYS[1], 'claim')
local now = tonumber(ARGV[1])
if claim and claim ~= '' then
  local sep = string.find(claim, ':')
  local ctoken = string.sub(claim, 1, sep - 1)
  local cexp = tonumber(string.sub(claim, sep + 1))
  if ctoken ~= ARGV[3] and cexp > now then return 0 end
end
redis.call('HSET', KEYS[1], 'claim', ARGV[3] .. ':' .. tostring(now + tonumber(ARGV[2])))
return 1
"""


def _foreign_lease_guard(now_argv: str, token_argv: str) -> str:
    """Lua that returns -3 when a DIFFERENT worker holds a live delivery lease.

    The check every delivery-state write takes before it mutates, so a worker whose lease lapsed and
    was taken over cannot overwrite the holder's progress.
    """
    return f"""
local claim = redis.call('HGET', KEYS[1], 'claim')
if claim and claim ~= '' then
  local sep = string.find(claim, ':')
  if string.sub(claim, 1, sep - 1) ~= {token_argv}
     and tonumber(string.sub(claim, sep + 1)) > tonumber({now_argv}) then
    return -3
  end
end
"""


# Move a record to ``provisional``, recording the outbound ids and grace deadline and
# releasing the lease: 1 transitioned, 0 already terminal, -1 gone, -3 a foreign live lease.
# ARGV = outbound_json, attempts, grace_deadline, now, token, message_id, index_score.
_PROVISIONAL_LUA = f"""
-- conversations:record:provisional
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status == 'delivered' or status == 'failed' or status == 'shed' then return 0 end
{_foreign_lease_guard("ARGV[4]", "ARGV[5]")}
redis.call('HSET', KEYS[1], 'delivery_status', 'provisional', 'outbound_ids', ARGV[1],
  'attempts', ARGV[2], 'grace_deadline', ARGV[3], 'updated_at', ARGV[4], 'claim', '')
{_reindex("ARGV[6]", "ARGV[7]")}
return 1
"""

# Terminal delivered write from the send path: 1 transitioned, 0 already delivered
# (idempotent), -1 gone, -2 already failed (a conflict the caller logs), -3 a foreign live
# lease. Sets the retention TTL.
# ARGV = outbound_json, attempts, now, ttl_ms, token, message_id, index_score.
_DELIVERED_LUA = f"""
-- conversations:record:delivered
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status == 'failed' or status == 'shed' then return -2 end
if status == 'delivered' then return 0 end
{_foreign_lease_guard("ARGV[3]", "ARGV[5]")}
redis.call('HSET', KEYS[1], 'delivery_status', 'delivered', 'outbound_ids', ARGV[1],
  'attempts', ARGV[2], 'updated_at', ARGV[3], 'claim', '', 'grace_deadline', '')
redis.call('PEXPIRE', KEYS[1], ARGV[4])
{_reindex("ARGV[6]", "ARGV[7]")}
return 1
"""

# Terminal failed write from the send path (retries exhausted): 1 transitioned, 0 already
# failed, -1 gone, -2 the send already completed (delivered/shed/provisional), -3 a foreign
# live lease. Sets the retention TTL.
# ARGV = attempts, now, ttl_ms, token, message_id, index_score.
_FAILED_LUA = f"""
-- conversations:record:failed
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status == 'delivered' or status == 'shed' or status == 'provisional' then return -2 end
if status == 'failed' then return 0 end
{_foreign_lease_guard("ARGV[2]", "ARGV[4]")}
redis.call('HSET', KEYS[1], 'delivery_status', 'failed', 'attempts', ARGV[1],
  'updated_at', ARGV[2], 'claim', '', 'grace_deadline', '')
redis.call('PEXPIRE', KEYS[1], ARGV[3])
{_reindex("ARGV[5]", "ARGV[6]")}
return 1
"""

# Ingest an out-of-band receipt against a fully sent (``provisional``) record: 1
# transitioned, 0 already in the target terminal state, -1 gone, -2 conflicting terminal
# state, -3 the send has not finished. ARGV = target_status, now, ttl_ms, message_id,
# index_score.
_RECEIPT_LUA = f"""
-- conversations:record:receipt
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
local target = ARGV[1]
if status == target then return 0 end
if status == 'delivered' or status == 'failed' or status == 'shed' then return -2 end
if status ~= 'provisional' then return -3 end
redis.call('HSET', KEYS[1], 'delivery_status', target, 'updated_at', ARGV[2],
  'claim', '', 'grace_deadline', '')
redis.call('PEXPIRE', KEYS[1], ARGV[3])
{_reindex("ARGV[4]", "ARGV[5]")}
return 1
"""


# Delete a record and its index membership in ONE step, so no listing can name a row that
# is gone. The thread leaves the route index with its LAST record, never before: the other
# records of a thread outlive any one of them. Returns the number of keys removed.
# ARGV = message_id, thread_id.
_DELETE_LUA = f"""
-- conversations:record:delete
local removed = redis.call('DEL', KEYS[1])
for i = 2, {1 + len(_INDEXED_STATUSES)} do redis.call('ZREM', KEYS[i], ARGV[1]) end
redis.call('ZREM', {_DELETE_THREAD_INDEX_KEY}, ARGV[1])
if redis.call('ZCARD', {_DELETE_THREAD_INDEX_KEY}) == 0 then
  redis.call('ZREM', {_DELETE_ROUTE_THREADS_KEY}, ARGV[2])
end
return removed
"""

# Unindex a member whose row is already gone — an orphan a status listing found. It names
# no thread (a vanished row cannot say which), so the thread indexes are reclaimed by the
# prune pass instead. Returns the number of index members removed.
# ARGV = message_id.
_UNINDEX_LUA = f"""
-- conversations:record:unindex
local removed = 0
for i = 2, {1 + len(_INDEXED_STATUSES)} do removed = removed + redis.call('ZREM', KEYS[i], ARGV[1]) end
return removed
"""

# Reclaim one thread's expired members and, once nothing is left, the thread itself — in ONE
# step, so a create landing mid-prune is never clobbered: it either precedes the ZCARD (which
# then sees its member) or follows the whole script (and re-adds the thread). Called with NO
# candidate too, which is how a thread whose index is already empty leaves the route index.
# KEYS[1]=thread index, KEYS[2]=route thread index, KEYS[3..]=one record key per candidate;
# ARGV[1]=thread_id, ARGV[2..]=the candidates' message_ids, parallel to KEYS[3..]. Returns
# ``{members removed, members the thread index still holds}`` — the walker needs the second
# number to know whether this thread just left the route index and so shifted the ranks of
# the threads behind it.
_PRUNE_THREAD_LUA = """
-- conversations:thread:prune
local removed = 0
for i = 3, #KEYS do
  if redis.call('EXISTS', KEYS[i]) == 0 then
    removed = removed + redis.call('ZREM', KEYS[1], ARGV[i - 1])
  end
end
local remaining = redis.call('ZCARD', KEYS[1])
if remaining == 0 then redis.call('ZREM', KEYS[2], ARGV[1]) end
return {removed, remaining}
"""
