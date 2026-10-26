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
_F_SENDS = "sends"
_F_ATTEMPTS = "attempts"
_F_GRACE = "grace_deadline"
_F_UPDATED = "updated_at"
_F_INTAKE = "intake_claim"
_F_ASK_REPLY = "ask_reply"

# Every record-mutating script takes the same key layout: KEYS[1]=record key, then one
# key per DeliveryStatus (the per-status indexes), then the index the transition moves the
# record INTO. The status count is derived below, so the layout tracks the enum. A script
# that also touches the thread indexes carries them LAST — one slot earlier in the delete,
# which writes no target index — and the create carries the ROUTING ROW behind them, the
# key it decides against whether the route still routes.
_INDEXED_STATUSES = tuple(DeliveryStatus)
_TARGET_INDEX_KEY = f"KEYS[{2 + len(_INDEXED_STATUSES)}]"
# The per-status indexes a parked receipt's terminal names, addressed by the shared layout so a
# script applying a parked receipt threads no extra key.
_DELIVERED_INDEX_KEY = f"KEYS[{2 + _INDEXED_STATUSES.index(DeliveryStatus.DELIVERED)}]"
_FAILED_INDEX_KEY = f"KEYS[{2 + _INDEXED_STATUSES.index(DeliveryStatus.FAILED)}]"
_THREAD_INDEX_KEY = f"KEYS[{3 + len(_INDEXED_STATUSES)}]"
_ROUTE_THREADS_KEY = f"KEYS[{4 + len(_INDEXED_STATUSES)}]"
_ROUTE_ROW_KEY = f"KEYS[{5 + len(_INDEXED_STATUSES)}]"
_DELETE_THREAD_INDEX_KEY = f"KEYS[{2 + len(_INDEXED_STATUSES)}]"
_DELETE_ROUTE_THREADS_KEY = f"KEYS[{3 + len(_INDEXED_STATUSES)}]"

# A live record's index member outlives nothing; a terminal one expires with its row.
_NO_EXPIRY_SCORE = "+inf"


def _status_index_key(status: DeliveryStatus) -> str:
    """The shared layout's slot of the per-status index of ``status``."""
    return f"KEYS[{2 + _INDEXED_STATUSES.index(status)}]"


# The keys outside a record's row that name the record (its message refs, its outbound reverse-index
# keys, its ask index key and, for a send record, its route's in-flight set) are named on the row's
# ``owned_keys`` field, a JSON list, by the script that writes each one. Every writer goes through
# these helpers, so a key never exists without its name on its row, a gone row gets no key, and a
# writer re-run for the same record (a resume's re-index, a create re-run under the same id) leaves
# each ref list holding the record once and each name on the row once. Building key names read off a
# row inside a script follows the store's single-node Redis assumption.
_OWNED_LIB = """
local function owned_names(rk)
  local raw = redis.call('HGET', rk, 'owned_keys')
  if not raw or raw == '' then return {} end
  return cjson.decode(raw)
end
local function own(rk, name)
  local names = owned_names(rk)
  for _, held in ipairs(names) do if held == name then return false end end
  names[#names + 1] = name
  redis.call('HSET', rk, 'owned_keys', cjson.encode(names))
  return true
end
local function disown(rk, name)
  local kept = {}
  for _, held in ipairs(owned_names(rk)) do if held ~= name then kept[#kept + 1] = held end end
  redis.call('HSET', rk, 'owned_keys', #kept == 0 and '[]' or cjson.encode(kept))
end
local function own_ref(rk, ref, mid, ttl_ms)
  if own(rk, ref) then redis.call('RPUSH', ref, mid) end
  redis.call('PEXPIRE', ref, ttl_ms)
end
local function own_value(rk, key, mid, ttl_ms)
  redis.call('SET', key, mid, 'PX', ttl_ms)
  own(rk, key)
end
local function own_member(rk, set_key, mid)
  redis.call('SADD', set_key, mid)
  own(rk, set_key)
end
local function refresh_refs(rk, ttl_ms)
  for _, name in ipairs(owned_names(rk)) do
    if redis.call('TYPE', name)['ok'] == 'list' then redis.call('PEXPIRE', name, ttl_ms) end
  end
end
local function forget_owned(rk, mid)
  for _, name in ipairs(owned_names(rk)) do
    local kind = redis.call('TYPE', name)['ok']
    if kind == 'list' then
      redis.call('LREM', name, 0, mid)
    elseif kind == 'set' then
      redis.call('SREM', name, mid)
    elseif kind == 'string' and redis.call('GET', name) == mid then
      redis.call('DEL', name)
    end
  end
end
"""


# Who wrote into a route-keyed thread. A script that stamps it decodes the key prefixes
# (``ConversationsKeyspace.writer_prefixes``) into ``W`` before it calls these helpers, and builds
# every key in-script under the store's single-node Redis assumption. A door is the JSON
# ``[target_kind, target_name, door_address_key]`` of the route a record was written under; its
# writer is the person the target's person index names for it, or the door itself while the index
# names nobody. ``w_stamp`` keeps the writer on the record's row-only ``writer`` field and the door
# on ``writer_door``, and adds the thread to the writer's ``writer_threads`` visit list, which an
# erase walks. ``w_thread_records`` answers the record keys a route-keyed thread's transcript index
# holds; a thread id that is not route-keyed raises.
_WRITER_LIB = """
local W
local function w_holder(door)
  local parts = cjson.decode(door)
  return redis.call('HGET', W.index .. parts[1] .. ':' .. parts[2], parts[3]) or door
end
local function w_stamp(thread, door, record_key)
  local who = w_holder(door)
  redis.call('SADD', W.threads .. who, thread)
  redis.call('HSET', record_key, 'writer', who, 'writer_door', door)
end
local function w_thread_records(thread)
  local rest = string.sub(thread, #W.route_thread + 1)
  local cut = string.find(rest, ':', 1, true)
  if string.sub(thread, 1, #W.route_thread) ~= W.route_thread or not cut then
    error('conversations: a writer threads entry is not route-keyed: ' .. thread)
  end
  local keys = {}
  for _, mid in ipairs(redis.call('ZRANGE', W.thread_index .. string.sub(rest, 1, cut - 1) .. ':' .. thread, 0, -1)) do
    keys[#keys + 1] = W.records .. mid
  end
  return keys
end
"""


def _reindex(member_argv: str, score_argv: str) -> str:
    """Lua moving the record's id into the target status index and out of every other.

    So exactly one index names it and a listing never walks the record keyspace.
    """
    return f"""
for i = 2, {1 + len(_INDEXED_STATUSES)} do redis.call('ZREM', KEYS[i], {member_argv}) end
redis.call('ZADD', {_TARGET_INDEX_KEY}, {score_argv}, {member_argv})
"""


# Atomic get-or-set of the inbound-dedupe marker: returns the message_id owning the pair —
# the caller's on a fresh claim, the prior turn's on a redelivery. The create folds the same
# get-or-set into its own step; this one serves a re-claim of an existing record.
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
# for a route that no longer routes; the record's ``route_missing`` field is then set, so
# the operator read carries the honest marker and not only a log line.
#
# The same step optionally claims the inbound pair, refreshes the thread's mode override, checks a
# person thread's person and writes the inbound's message ref. Three OPTIONAL key groups follow the
# routing row, in this order and each only when its ARGV flag is set: the dedupe key (ARGV[12] = its
# TTL seconds, '' for no claim), then the thread's mode key (ARGV[13] = the retention TTL seconds,
# '' for no refresh), then, for a person thread (ARGV[14] = '1'), that person's row key, erase-mark
# key and recorded-routes key. Last comes the inbound's message ref key (ARGV[16] = the retention TTL
# in milliseconds, '' for none). The claim is the get-or-set of ``_CLAIM_INBOUND_LUA``, decided
# FIRST: a pair another message owns writes nothing and answers ``{-1, owner}``. A person thread
# whose person row is gone or carries the erase mark writes nothing either and answers ``{-2, ''}``.
# Otherwise the record is written, the pair claimed, the mode refreshed (``EXPIRE`` never resurrects
# an absent override), the ref appended once per record and named on the row, and the script answers
# ``{indexed, message_id}`` — indexed 1 when the thread indexes were written (and, for a person
# thread, the route recorded among its routes), 0 when the route was gone. For a route-keyed thread
# (ARGV[17], the record's door, set) the record stamps its writer on the thread in either case
# (``_WRITER_LIB``).
# ARGV = content_json, delivery_status, sends_json, attempts, updated_at, ttl_ms ('' for a record
# that must not expire yet), intake_claim ('' off the intake path), message_id, index_score,
# thread_id, created_at, claim_ttl, mode_ttl, person_flag, route_name, ref_ttl_ms, door,
# writer_prefixes.
_CREATE_LUA = f"""
-- conversations:record:create
{_OWNED_LIB}
{_WRITER_LIB}
local optional = {6 + len(_INDEXED_STATUSES)}
local claim_key, mode_key, person_row, person_mark, person_routes, ref_key
if ARGV[12] ~= '' then claim_key = KEYS[optional]; optional = optional + 1 end
if ARGV[13] ~= '' then mode_key = KEYS[optional]; optional = optional + 1 end
if ARGV[14] ~= '' then
  person_row = KEYS[optional]; person_mark = KEYS[optional + 1]; person_routes = KEYS[optional + 2]
  optional = optional + 3
end
if ARGV[16] ~= '' then ref_key = KEYS[optional] end
local owner
if claim_key then
  owner = redis.call('GET', claim_key)
  if owner and owner ~= ARGV[8] then return {{-1, owner}} end
end
if person_row and (redis.call('EXISTS', person_row) == 0 or redis.call('EXISTS', person_mark) == 1) then
  return {{-2, ''}}
end
redis.call('HSET', KEYS[1], 'data', ARGV[1], 'delivery_status', ARGV[2], '{_F_SENDS}', ARGV[3],
  'attempts', ARGV[4], 'claim', '', 'grace_deadline', '', 'updated_at', ARGV[5], 'intake_claim', ARGV[7])
if ARGV[6] ~= '' then redis.call('PEXPIRE', KEYS[1], ARGV[6]) end
{_reindex("ARGV[8]", "ARGV[9]")}
local indexed = 1
if redis.call('EXISTS', {_ROUTE_ROW_KEY}) == 0 then
  redis.call('HSET', KEYS[1], 'route_missing', '1')
  indexed = 0
else
  redis.call('ZADD', {_THREAD_INDEX_KEY}, ARGV[11], ARGV[8])
  redis.call('ZADD', {_ROUTE_THREADS_KEY}, ARGV[11], ARGV[10])
  if person_routes then redis.call('SADD', person_routes, ARGV[15]) end
end
if ref_key then own_ref(KEYS[1], ref_key, ARGV[8], ARGV[16]) end
if claim_key and not owner then redis.call('SET', claim_key, ARGV[8], 'EX', ARGV[12]) end
if mode_key then redis.call('EXPIRE', mode_key, ARGV[13]) end
if ARGV[17] ~= '' then
  W = cjson.decode(ARGV[18])
  w_stamp(ARGV[10], ARGV[17], KEYS[1])
end
return {{indexed, ARGV[8]}}
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
{_OWNED_LIB}
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status ~= 'accepted' then return 0 end
redis.call('HSET', KEYS[1], 'data', ARGV[1], 'delivery_status', 'silent', 'updated_at', ARGV[2],
  'intake_claim', '', 'claim', '')
redis.call('PEXPIRE', KEYS[1], ARGV[3])
refresh_refs(KEYS[1], ARGV[3])
{_reindex("ARGV[4]", "ARGV[5]")}
{_RESTAMP_LIVE_THREAD.format(slot=6)}
return 1
"""


def _accepted_to_overlap_terminal_lua(marker: str, status: str) -> str:
    """Lua moving an intake record from ``accepted`` straight to the terminal overlap ``status``.

    The channel-door outcome for a message merged into a later turn (``merged``) or dropped in
    favour of one (``superseded``): nothing is ever sent, so it releases the intake lease and
    applies the retention TTL in the SAME step, the ``complete_silent`` shape. The record's
    content (already carrying ``successor_id``) rides in ``ARGV[1]``. Guarded on the current
    status, so an overlap decision and a re-drive produce ONE outcome: 1 transitioned, 0 no
    longer at intake, -1 gone. ARGV = content_json, now, ttl_ms, message_id, index_score,
    thread_id.
    """
    return f"""
-- {marker}
{_OWNED_LIB}
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status ~= 'accepted' then return 0 end
redis.call('HSET', KEYS[1], 'data', ARGV[1], 'delivery_status', '{status}', 'updated_at', ARGV[2],
  'intake_claim', '', 'claim', '')
redis.call('PEXPIRE', KEYS[1], ARGV[3])
refresh_refs(KEYS[1], ARGV[3])
{_reindex("ARGV[4]", "ARGV[5]")}
{_RESTAMP_LIVE_THREAD.format(slot=6)}
return 1
"""


# Move an intake record from ``accepted`` straight to terminal ``merged`` — a channel-door
# message whose text was carried into a later turn, so nothing is ever sent. The ``silent``
# shape, stamping ``successor_id`` (in the content) and the retention TTL.
_MERGE_RECORD_LUA = _accepted_to_overlap_terminal_lua("conversations:record:merge", "merged")

# Move an intake record from ``accepted`` straight to terminal ``superseded`` — a channel-door
# message dropped in favour of a later turn, so nothing is ever sent. The ``silent`` shape,
# stamping ``successor_id`` (in the content) and the retention TTL.
_SUPERSEDE_RECORD_LUA = _accepted_to_overlap_terminal_lua("conversations:record:supersede", "superseded")


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


# Move a record to ``provisional``, recording the parts sent with their ids and the grace deadline and
# releasing the lease: 1 transitioned, 0 already terminal, -1 gone, -3 a foreign live lease.
# A receipt that arrived during the send is staged on the record's ``pending_receipt`` field;
# when it is set, this step applies it in the SAME script — straight to the parked terminal,
# skipping ``provisional`` — and returns 2 (parked receipt was ``delivered``) or 3 (parked
# receipt was ``failed``). ARGV = sends_json, attempts, grace_deadline, now, token,
# message_id, index_score, ttl_ms, terminal_index_score.
_PROVISIONAL_LUA = f"""
-- conversations:record:provisional
{_OWNED_LIB}
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status == 'delivered' or status == 'failed' or status == 'shed' then return 0 end
{_foreign_lease_guard("ARGV[4]", "ARGV[5]")}
local parked = redis.call('HGET', KEYS[1], 'pending_receipt')
if parked and parked ~= '' then
  redis.call('HSET', KEYS[1], 'delivery_status', parked, '{_F_SENDS}', ARGV[1],
    'attempts', ARGV[2], 'updated_at', ARGV[4], 'claim', '', 'grace_deadline', '', 'pending_receipt', '')
  redis.call('PEXPIRE', KEYS[1], ARGV[8])
  refresh_refs(KEYS[1], ARGV[8])
  for i = 2, {1 + len(_INDEXED_STATUSES)} do redis.call('ZREM', KEYS[i], ARGV[6]) end
  if parked == 'delivered' then
    redis.call('ZADD', {_DELIVERED_INDEX_KEY}, ARGV[9], ARGV[6])
    return 2
  end
  redis.call('ZADD', {_FAILED_INDEX_KEY}, ARGV[9], ARGV[6])
  return 3
end
redis.call('HSET', KEYS[1], 'delivery_status', 'provisional', '{_F_SENDS}', ARGV[1],
  'attempts', ARGV[2], 'grace_deadline', ARGV[3], 'updated_at', ARGV[4], 'claim', '')
{_reindex("ARGV[6]", "ARGV[7]")}
return 1
"""

# Terminal delivered write from the send path: 1 transitioned, 0 already delivered
# (idempotent), -1 gone, -2 already failed (a conflict the caller logs), -3 a foreign live
# lease. Sets the retention TTL.
# ARGV = sends_json, attempts, now, ttl_ms, token, message_id, index_score.
_DELIVERED_LUA = f"""
-- conversations:record:delivered
{_OWNED_LIB}
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status == 'failed' or status == 'shed' then return -2 end
if status == 'delivered' then return 0 end
{_foreign_lease_guard("ARGV[3]", "ARGV[5]")}
redis.call('HSET', KEYS[1], 'delivery_status', 'delivered', '{_F_SENDS}', ARGV[1],
  'attempts', ARGV[2], 'updated_at', ARGV[3], 'claim', '', 'grace_deadline', '')
redis.call('PEXPIRE', KEYS[1], ARGV[4])
refresh_refs(KEYS[1], ARGV[4])
{_reindex("ARGV[6]", "ARGV[7]")}
return 1
"""

# Terminal failed write from the send path (retries exhausted): 1 transitioned, 0 already
# failed, -1 gone, -2 the send already completed (delivered/shed/provisional), -3 a foreign
# live lease. A staged receipt is cleared in the same step and the write returns 2 when it
# dropped a non-empty ``pending_receipt`` — first terminal wins, the send-side outcome stands.
# Sets the retention TTL and records the parts already sent (``sends``, the ids of a partially
# failed reply). ARGV = attempts, now, ttl_ms, token, message_id, index_score, sends_json.
_FAILED_LUA = f"""
-- conversations:record:failed
{_OWNED_LIB}
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status == 'delivered' or status == 'shed' or status == 'provisional' then return -2 end
if status == 'failed' then return 0 end
{_foreign_lease_guard("ARGV[2]", "ARGV[4]")}
local parked = redis.call('HGET', KEYS[1], 'pending_receipt')
redis.call('HSET', KEYS[1], 'delivery_status', 'failed', 'attempts', ARGV[1], '{_F_SENDS}', ARGV[7],
  'updated_at', ARGV[2], 'claim', '', 'grace_deadline', '', 'pending_receipt', '')
redis.call('PEXPIRE', KEYS[1], ARGV[3])
refresh_refs(KEYS[1], ARGV[3])
{_reindex("ARGV[5]", "ARGV[6]")}
if parked and parked ~= '' then return 2 end
return 1
"""

# Ingest an out-of-band receipt: 1 transitioned (a ``provisional`` record settled), 2 parked
# (the send is still in flight — the target is staged on ``pending_receipt`` and applied by the
# completing ``provisional`` write), 0 idempotent (already in the target state, or re-parked to
# the same target), -1 gone, -2 conflicting terminal (a real terminal, or a different target
# already parked — first parked wins), -3 a state that never sent an answer. ARGV =
# target_status, now, ttl_ms, message_id, index_score.
_RECEIPT_LUA = f"""
-- conversations:record:receipt
{_OWNED_LIB}
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
local target = ARGV[1]
if status == target then return 0 end
if status == 'delivered' or status == 'failed' or status == 'shed' then return -2 end
if status == 'pending_delivery' then
  local parked = redis.call('HGET', KEYS[1], 'pending_receipt')
  if parked == target then return 0 end
  if parked and parked ~= '' then return -2 end
  redis.call('HSET', KEYS[1], 'pending_receipt', target)
  return 2
end
if status ~= 'provisional' then return -3 end
redis.call('HSET', KEYS[1], 'delivery_status', target, 'updated_at', ARGV[2],
  'claim', '', 'grace_deadline', '')
redis.call('PEXPIRE', KEYS[1], ARGV[3])
refresh_refs(KEYS[1], ARGV[3])
{_reindex("ARGV[4]", "ARGV[5]")}
return 1
"""

# Move an unrecoverable sweep row to the terminal ``failed`` state. The sweep holds no
# delivery lease, so the token/``attempts`` contract ``mark_failed`` guards on cannot serve a
# corrupt row; this composes the same reusable fragments without calling ``_FAILED_LUA`` (which
# refuses a ``provisional`` row). ``-1`` the row is gone (its hash is absent — not merely a
# missing status field); ``0`` already ``failed``; ``-2`` already ``delivered``/``shed`` (moved
# on since the index read, its writer answers for it); ``-3`` a DIFFERENT worker holds a live
# delivery lease, so the row is left to the holder for this pass; otherwise (``pending_delivery``,
# ``provisional``, ``accepted`` or an unreadable/missing status) HSET ``failed`` and reindex.
# ``attempts`` is left untouched. A staged receipt on a corrupt row is cleared in the same step
# and the write returns 2 when it dropped a non-empty ``pending_receipt`` — first terminal wins,
# the sweep's outcome stands. ARGV = now, ttl_ms, message_id, index_score.
_UNREADABLE_LUA = f"""
-- conversations:record:unreadable
{_OWNED_LIB}
if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if status == 'failed' then return 0 end
if status == 'delivered' or status == 'shed' then return -2 end
{_foreign_lease_guard("ARGV[1]", "''")}
local parked = redis.call('HGET', KEYS[1], 'pending_receipt')
redis.call('HSET', KEYS[1], 'delivery_status', 'failed', 'claim', '', 'grace_deadline', '',
  'updated_at', ARGV[1], 'pending_receipt', '')
redis.call('PEXPIRE', KEYS[1], ARGV[2])
refresh_refs(KEYS[1], ARGV[2])
{_reindex("ARGV[3]", "ARGV[4]")}
if parked and parked ~= '' then return 2 end
return 1
"""


# Delete a record row with everything that names it: the keys its ``owned_keys`` names (each ref
# list and in-flight set loses the record, each keyspace-3 key and the ask key that still hold the
# record's id are deleted) and its status-index member. Shared by the delete and the send placement
# step, so one cleanup serves every path that removes a row on request. The writer the record
# stamped stays a writer of its thread until the thread is cleared. Returns the keys removed.
_DELETE_ROW_LIB = f"""
{_OWNED_LIB}
local function delete_row(rk, mid)
  forget_owned(rk, mid)
  local removed = redis.call('DEL', rk)
  for i = 2, {1 + len(_INDEXED_STATUSES)} do redis.call('ZREM', KEYS[i], mid) end
  return removed
end
"""

# Delete a record and its index membership in ONE step, so no listing can name a row that
# is gone. The record's refs, keyspace-3 entries and ask entry go with it (``_DELETE_ROW_LIB``).
# The thread leaves the route index with its LAST record, never before: the other records of a
# thread outlive any one of them. Returns the number of keys removed. ARGV = message_id, thread_id.
_DELETE_LUA = f"""
-- conversations:record:delete
{_DELETE_ROW_LIB}
local removed = delete_row(KEYS[1], ARGV[1])
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
