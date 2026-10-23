"""The atomic Redis Lua steps of send records, on the shared record key layout.

A send record is begun in ``sending`` before its send goes out, placed in its thread and ended
exactly once. The layout constants and the shared Lua libraries come from
:mod:`tai42_skeleton.conversations.record_scripts`.
"""

from __future__ import annotations

from tai42_skeleton.conversations.record_scripts import (
    _DELETE_ROW_LIB,
    _DELIVERED_INDEX_KEY,
    _F_ASK_REPLY,
    _F_SENDS,
    _INDEXED_STATUSES,
    _OWNED_LIB,
    _ROUTE_ROW_KEY,
    _ROUTE_THREADS_KEY,
    _TARGET_INDEX_KEY,
    _THREAD_INDEX_KEY,
    _WRITER_LIB,
    _reindex,
)

# -- send records ------------------------------------------------------------------------------
#
# A send record is written in ``sending`` before its send goes out and ended exactly once, by its
# completion, its end or the settle. Every end script takes the shared layout plus, behind the
# routing row, the route's in-flight set and, for a person thread (the person flag ARGV set), that
# person's row key, erase-mark key and recorded-routes key.
_SEND_INFLIGHT_KEY = f"KEYS[{6 + len(_INDEXED_STATUSES)}]"
_SEND_PERSON_BASE = 7 + len(_INDEXED_STATUSES)


# Open a send script's locals: the record, its id, the thread the script places the row in, the
# row's ``created_at`` and route, the door its landing stamps ('' for a person thread), the decoded
# writer prefixes, the optional person group, and the first key slot behind them.
def _send_locals(
    mid: str, thread: str, created: str, route: str, person_flag: str, landing_door: str, prefixes: str
) -> str:
    return f"""
local rk = KEYS[1]
local mid = {mid}
local place_thread = {thread}
local created = {created}
local route_name = {route}
local landing_door = {landing_door}
W = cjson.decode({prefixes})
local person_row, person_mark, person_routes
local after = {_SEND_PERSON_BASE}
if {person_flag} ~= '' then
  person_row = KEYS[after]; person_mark = KEYS[after + 1]; person_routes = KEYS[after + 2]
  after = after + 3
end
"""


# The placement step's checks, run before any write: a gone row answers ``{-1, 'gone'}`` and writes
# nothing (no ``HSET``, which would re-create a partial row); a row no longer ``sending`` answers
# ``{-2, <its status>}``; a row whose place thread was forgotten while the send was going out
# (named in its ``forgotten_threads``), or a person thread whose person row is gone or carries the
# erase mark, is deleted with its keys and answers ``{-1, 'forgotten'}`` / ``{-1, 'person_closed'}``.
_SEND_PLACEMENT_CHECKS = """
if redis.call('EXISTS', rk) == 0 then return {-1, 'gone'} end
local status = redis.call('HGET', rk, 'delivery_status')
if status ~= 'sending' then return {-2, status or ''} end
local forgotten = redis.call('HGET', rk, 'forgotten_threads')
if forgotten and forgotten ~= '' then
  for _, thread in ipairs(cjson.decode(forgotten)) do
    if thread == place_thread then
      delete_row(rk, mid)
      return {-1, 'forgotten'}
    end
  end
end
if person_row and (redis.call('EXISTS', person_row) == 0 or redis.call('EXISTS', person_mark) == 1) then
  delete_row(rk, mid)
  return {-1, 'person_closed'}
end
"""

# The placement step's writes, run after the row's new state is written: the row leaves the
# route's in-flight set; while the routing row stands both thread indexes are written (scored by
# ``created_at``) and, for a person thread, the route joins the person's recorded routes (1);
# a gone routing row sets ``route_missing`` and writes neither index (0). In both cases a landing
# in a route-keyed thread stamps its writer on the thread (``_WRITER_LIB``).
_SEND_PLACEMENT_WRITES = f"""
redis.call('SREM', {_SEND_INFLIGHT_KEY}, mid)
disown(rk, {_SEND_INFLIGHT_KEY})
if landing_door ~= '' then w_stamp(place_thread, landing_door, rk) end
local placed = 1
if redis.call('EXISTS', {_ROUTE_ROW_KEY}) == 0 then
  redis.call('HSET', rk, 'route_missing', '1')
  placed = 0
else
  redis.call('ZADD', {_THREAD_INDEX_KEY}, created, mid)
  redis.call('ZADD', {_ROUTE_THREADS_KEY}, created, place_thread)
  if person_routes then redis.call('SADD', person_routes, route_name) end
end
"""

# Begin a send record: write the row in ``sending`` with no TTL, index it in the ``sending`` index
# scored by its settle deadline, add it to its route's in-flight set, keep its begin thread on the
# row-only ``placed_thread`` field and, for an ask, write the ask key. For a route-keyed begin
# thread (``placed_door`` set) the send stamps its writer on that thread (``_WRITER_LIB``), which
# keeps the door on the row-only ``writer_door`` field, whether or not the routing row stands.
# Returns 1 written, 0 a row under this id already exists (nothing written). KEYS = the shared
# layout's record, status indexes and target index, then the route's in-flight set, then the ask
# key (only with an ask TTL). ARGV holds content_json, attempts, updated_at, message_id,
# settle_deadline, placed_thread, ask_ttl_ms (empty for no ask), placed_door ('' for a person
# thread) and writer_prefixes.
_BEGIN_SEND_LUA = f"""
-- conversations:record:begin_send
{_OWNED_LIB}
{_WRITER_LIB}
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('HSET', KEYS[1], 'data', ARGV[1], 'delivery_status', 'sending', '{_F_SENDS}', '[]',
  'attempts', ARGV[2], 'claim', '', 'grace_deadline', '', 'updated_at', ARGV[3], 'intake_claim', '',
  'placed_thread', ARGV[6])
if ARGV[8] ~= '' then
  W = cjson.decode(ARGV[9])
  w_stamp(ARGV[6], ARGV[8], KEYS[1])
end
{_reindex("ARGV[4]", "ARGV[5]")}
own_member(KEYS[1], KEYS[{3 + len(_INDEXED_STATUSES)}], ARGV[4])
if ARGV[7] ~= '' then own_value(KEYS[1], KEYS[{4 + len(_INDEXED_STATUSES)}], ARGV[4], ARGV[7]) end
return 1
"""

# Complete a send record from its outcome, only while it is still ``sending``: rewrite its content
# (the outcome's identity, address, route and thread), write ``sends``, move it to ``provisional``
# with the grace deadline, or to ``delivered`` with the retention TTL when it already carries an
# ``ask_reply``, write keyspace 3 and the message refs for its sent ids, and place it in the
# outcome's thread. Answers ``{1 | 0, <the status written>}`` (1 indexed, 0 route missing), or the
# placement checks' answer. KEYS = the send layout, then ``n_k3`` keyspace-3 keys, then the ref
# keys. ARGV holds content_json, sends_json, attempts, now, message_id, provisional_score,
# delivered_score, grace_deadline, ttl_ms, thread_id, created_at, route_name, person_flag, n_k3,
# the landing door ('' for a person thread) and writer_prefixes.
_COMPLETE_SEND_LUA = f"""
-- conversations:record:complete_send
{_DELETE_ROW_LIB}
{_WRITER_LIB}
{_send_locals("ARGV[5]", "ARGV[10]", "ARGV[11]", "ARGV[12]", "ARGV[13]", "ARGV[15]", "ARGV[16]")}
{_SEND_PLACEMENT_CHECKS}
local final = 'provisional'
local reply = redis.call('HGET', rk, '{_F_ASK_REPLY}')
if reply and reply ~= '' then final = 'delivered' end
for i = 2, {1 + len(_INDEXED_STATUSES)} do redis.call('ZREM', KEYS[i], mid) end
if final == 'delivered' then
  redis.call('HSET', rk, 'data', ARGV[1], 'delivery_status', final, '{_F_SENDS}', ARGV[2],
    'attempts', ARGV[3], 'updated_at', ARGV[4], 'grace_deadline', '')
  redis.call('ZADD', {_DELIVERED_INDEX_KEY}, ARGV[7], mid)
  redis.call('PEXPIRE', rk, ARGV[9])
else
  redis.call('HSET', rk, 'data', ARGV[1], 'delivery_status', final, '{_F_SENDS}', ARGV[2],
    'attempts', ARGV[3], 'updated_at', ARGV[4], 'grace_deadline', ARGV[8])
  redis.call('ZADD', {_TARGET_INDEX_KEY}, ARGV[6], mid)
end
local n_k3 = tonumber(ARGV[14])
for i = after, after + n_k3 - 1 do own_value(rk, KEYS[i], mid, ARGV[9]) end
for i = after + n_k3, #KEYS do own_ref(rk, KEYS[i], mid, ARGV[9]) end
if final == 'delivered' then refresh_refs(rk, ARGV[9]) end
{_SEND_PLACEMENT_WRITES}
return {{placed, final}}
"""

# End a send record that returned no outcome, only while it is still ``sending``: move it to
# ``failed`` or ``outcome_unknown`` with its error (or to ``delivered``, its content without the
# error, when it already carries an ``ask_reply``), apply the retention TTL to the row and its ref
# lists, and place it in its begin thread. Shared by the recorder's end and the settle of a row
# whose process stopped; the landing stamps the begin's ``writer_door``. Answers
# ``{1 | 0, <the status written>}`` or the placement checks' answer. KEYS = the send layout.
# Its ARGV, in order: content_with_error, content_without_error, status, now, message_id,
# terminal_score, ttl_ms, thread_id, created_at, route_name, person_flag, writer_prefixes.
_END_SEND_WRITER_DOOR = "redis.call('HGET', KEYS[1], 'writer_door') or ''"
_END_SEND_LUA = f"""
-- conversations:record:end_send
{_DELETE_ROW_LIB}
{_WRITER_LIB}
{_send_locals("ARGV[5]", "ARGV[8]", "ARGV[9]", "ARGV[10]", "ARGV[11]", _END_SEND_WRITER_DOOR, "ARGV[12]")}
{_SEND_PLACEMENT_CHECKS}
local final, content = ARGV[3], ARGV[1]
local reply = redis.call('HGET', rk, '{_F_ASK_REPLY}')
if reply and reply ~= '' then final, content = 'delivered', ARGV[2] end
redis.call('HSET', rk, 'data', content, 'delivery_status', final, 'updated_at', ARGV[4],
  'claim', '', 'grace_deadline', '')
for i = 2, {1 + len(_INDEXED_STATUSES)} do redis.call('ZREM', KEYS[i], mid) end
if final == 'delivered' then
  redis.call('ZADD', {_DELIVERED_INDEX_KEY}, ARGV[6], mid)
else
  redis.call('ZADD', {_TARGET_INDEX_KEY}, ARGV[6], mid)
end
redis.call('PEXPIRE', rk, ARGV[7])
refresh_refs(rk, ARGV[7])
{_SEND_PLACEMENT_WRITES}
return {{placed, final}}
"""

# Write a bridge reply chunk's keyspace-3 keys and message refs on its row, idempotently per
# (key, record), so the resume's re-index of a chunk already indexed changes nothing. A gone row
# (its thread was forgotten while the reply was being delivered) gets no key: -1; otherwise 1.
# KEYS = record key, then ``n_k3`` keyspace-3 keys, then the ref keys. ARGV = message_id, ttl_ms,
# n_k3.
_INDEX_OUTBOUND_LUA = f"""
-- conversations:record:index_outbound
{_OWNED_LIB}
if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
local n_k3 = tonumber(ARGV[3])
for i = 2, 1 + n_k3 do own_value(KEYS[1], KEYS[i], ARGV[1], ARGV[2]) end
for i = 2 + n_k3, #KEYS do own_ref(KEYS[1], KEYS[i], ARGV[1], ARGV[2]) end
return 1
"""

# Record the participant's channel reply on an ask's send record, only while the row is in the
# status the caller read (else -3, the caller re-reads): the reply is written on the row-only
# ``ask_reply`` field; a ``provisional``, ``failed`` or ``outcome_unknown`` row moves to
# ``delivered`` (a reply proves the question arrived) with the retention TTL on the row and its
# ref lists, its content rewritten without the error when the caller passes one; a ``sending`` or
# ``delivered`` row keeps its state. The reply's message ref is written when its key is passed.
# Returns 1 written, -1 the row is gone. KEYS = the shared layout's record, status indexes and
# target index (``delivered``), then the ref key (only with the ref flag). ARGV = reply_json,
# message_id, now, ttl_ms, delivered_score, expected_status, content_json ('' keeps the content),
# ref_flag.
_ASK_REPLY_LUA = f"""
-- conversations:record:ask_reply
{_OWNED_LIB}
if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if status ~= ARGV[6] then return -3 end
redis.call('HSET', KEYS[1], '{_F_ASK_REPLY}', ARGV[1], 'updated_at', ARGV[3])
local settles = status == 'provisional' or status == 'failed' or status == 'outcome_unknown'
if settles then
  if ARGV[7] ~= '' then redis.call('HSET', KEYS[1], 'data', ARGV[7]) end
  redis.call('HSET', KEYS[1], 'delivery_status', 'delivered', 'claim', '', 'grace_deadline', '')
  {_reindex("ARGV[2]", "ARGV[5]")}
  redis.call('PEXPIRE', KEYS[1], ARGV[4])
end
if ARGV[8] ~= '' then own_ref(KEYS[1], KEYS[{3 + len(_INDEXED_STATUSES)}], ARGV[2], ARGV[4]) end
if settles then refresh_refs(KEYS[1], ARGV[4]) end
return 1
"""

# Mark one member of a route's in-flight set for a thread being forgotten: a gone row, or one no
# longer ``sending`` (already in its thread index, drained with it), leaves the set (0); a
# ``sending`` row gets the thread appended to its ``forgotten_threads`` once and stays in the set
# (1), so its placement step deletes it if it lands in that thread. Removes no row.
# KEYS[1] = the route's in-flight set, KEYS[2] = the record key; ARGV = message_id, thread_id.
_FORGET_SENDING_LUA = """
-- conversations:record:forget_sending
if redis.call('EXISTS', KEYS[2]) == 0 or redis.call('HGET', KEYS[2], 'delivery_status') ~= 'sending' then
  redis.call('SREM', KEYS[1], ARGV[1])
  return 0
end
local raw = redis.call('HGET', KEYS[2], 'forgotten_threads')
local threads = {}
if raw and raw ~= '' then threads = cjson.decode(raw) end
for _, thread in ipairs(threads) do if thread == ARGV[2] then return 1 end end
threads[#threads + 1] = ARGV[2]
redis.call('HSET', KEYS[2], 'forgotten_threads', cjson.encode(threads))
return 1
"""
