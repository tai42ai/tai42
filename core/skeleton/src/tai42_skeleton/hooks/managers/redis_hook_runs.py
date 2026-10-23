"""The Redis scripts of the hook-run store.

A job is the hash ``{prefix}:run:{id}``. The due ZSET indexes every pending and running job, scored
by its next attempt time while pending and by its lease expiry while running; the failed ZSET
indexes every failed job, scored by ``failed_at``. Every step that reads and writes a job is one
server-side script, so a claim and a fenced write never interleave with another process's. A
fenced script acts only on a job that is ``running`` under the caller's ``claimed_by`` and returns
0 (or nil) otherwise. Run keys are built inside the scripts from the ``prefix`` argument, mirroring
``HooksSettings.hook_run_key``, because a claim learns its ids from the due ZSET.
"""

from __future__ import annotations

# The accept script takes the keys due and (optional) seen, and the arguments prefix, jobs_json and
# seen_ttl.
# jobs_json is a list of {id, score, fields}; every value a string.
ACCEPT_LUA = """
-- hooks:run:accept
local due = KEYS[1]
local seen = KEYS[2]
if seen and redis.call('EXISTS', seen) == 1 then
  return 0
end
local prefix = ARGV[1]
local jobs = cjson.decode(ARGV[2])
for _, job in ipairs(jobs) do
  local key = prefix .. ':run:' .. job.id
  local args = {}
  for name, value in pairs(job.fields) do
    args[#args + 1] = name
    args[#args + 1] = value
  end
  redis.call('HSET', key, unpack(args))
  redis.call('ZADD', due, job.score, job.id)
end
if seen then
  redis.call('SET', seen, '1', 'EX', tonumber(ARGV[3]))
end
return 1
"""

# The claim_due script takes the key due and the arguments prefix, now, limit, claimed_by, lease_until
# and exclude_json.
# Returns a flat list: prior status, HGETALL of the claimed job, for each claimed job.
CLAIM_DUE_LUA = """
-- hooks:run:claim_due
local due = KEYS[1]
local prefix, now, limit, claimed_by, lease_until = ARGV[1], ARGV[2], tonumber(ARGV[3]), ARGV[4], ARGV[5]
local exclude = {}
local excluded = 0
for _, id in ipairs(cjson.decode(ARGV[6])) do
  exclude[id] = true
  excluded = excluded + 1
end
local ids = redis.call('ZRANGEBYSCORE', due, '-inf', now, 'LIMIT', 0, limit + excluded)
local out = {}
local claimed = 0
for _, id in ipairs(ids) do
  if claimed >= limit then
    break
  end
  if not exclude[id] then
    local key = prefix .. ':run:' .. id
    local status = redis.call('HGET', key, 'status')
    if status == 'pending' or status == 'running' then
      redis.call('HSET', key, 'status', 'running', 'claimed_by', claimed_by, 'lease_until', lease_until)
      redis.call('HINCRBY', key, 'attempts', 1)
      redis.call('ZADD', due, lease_until, id)
      out[#out + 1] = status
      out[#out + 1] = redis.call('HGETALL', key)
      claimed = claimed + 1
    else
      redis.call('ZREM', due, id)
    end
  end
end
return out
"""

_FENCE = """
local function fenced(key, claimed_by)
  return redis.call('HGET', key, 'status') == 'running' and redis.call('HGET', key, 'claimed_by') == claimed_by
end
"""

_APPEND_IDS = """
local function append_ids(key, ids_json)
  local ids = cjson.decode(ids_json)
  if #ids == 0 then
    return
  end
  local current = redis.call('HGET', key, 'acted_ids')
  local all = {}
  if current then
    all = cjson.decode(current)
  end
  for _, id in ipairs(ids) do
    all[#all + 1] = id
  end
  redis.call('HSET', key, 'acted_ids', cjson.encode(all))
end
"""

# The renew script takes the keys run and due, and the arguments id, claimed_by, lease_until.
RENEW_LUA = (
    "-- hooks:run:renew\n"
    + _FENCE
    + """
if not fenced(KEYS[1], ARGV[2]) then
  return 0
end
redis.call('HSET', KEYS[1], 'lease_until', ARGV[3])
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[1])
return 1
"""
)

# The mark script takes the key run and the arguments claimed_by, phase, action, ids_json, intent_json
# ('' for none) and start_json ('' for none).
MARK_LUA = (
    "-- hooks:run:mark\n"
    + _FENCE
    + _APPEND_IDS
    + """
local key = KEYS[1]
local phase, action, ids_json, intent_json, start_json = ARGV[2], ARGV[3], ARGV[4], ARGV[5], ARGV[6]
if not fenced(key, ARGV[1]) then
  return 0
end
if phase == 'committed' then
  redis.call('HSET', key, 'acted', action)
  append_ids(key, ids_json)
  redis.call('HDEL', key, 'claim_intent')
elseif intent_json ~= '' then
  redis.call('HSET', key, 'claim_intent', intent_json)
else
  redis.call('HSET', key, 'acted', action)
  append_ids(key, ids_json)
  if start_json ~= '' then
    redis.call('HSET', key, 'start', start_json)
  end
end
return 1
"""
)

# The clear_intent script takes the key run and the arguments claimed_by.
CLEAR_INTENT_LUA = (
    "-- hooks:run:clear_intent\n"
    + _FENCE
    + """
if not fenced(KEYS[1], ARGV[1]) then
  return 0
end
redis.call('HDEL', KEYS[1], 'claim_intent')
return 1
"""
)

# The finish script takes the keys run and due, and the arguments id, claimed_by.
FINISH_LUA = (
    "-- hooks:run:finish\n"
    + _FENCE
    + """
if not fenced(KEYS[1], ARGV[2]) then
  return 0
end
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return 1
"""
)

# The reschedule script takes the keys run and due, and the arguments id, claimed_by, next_attempt_at,
# error and kind.
RESCHEDULE_LUA = (
    "-- hooks:run:reschedule\n"
    + _FENCE
    + """
if not fenced(KEYS[1], ARGV[2]) then
  return 0
end
redis.call('HSET', KEYS[1], 'status', 'pending', 'next_attempt_at', ARGV[3], 'last_error', ARGV[4],
  'last_error_kind', ARGV[5])
redis.call('HDEL', KEYS[1], 'claimed_by', 'lease_until')
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[1])
return 1
"""
)

# The fail script takes the keys run, due and failed, and the arguments id, claimed_by, error, kind,
# failed_at, acted, acted_ids_json ('' for none) and intent_json ('' for none).
FAIL_LUA = (
    "-- hooks:run:fail\n"
    + _FENCE
    + """
local key = KEYS[1]
if not fenced(key, ARGV[2]) then
  return false
end
redis.call('HSET', key, 'status', 'failed', 'last_error', ARGV[3], 'last_error_kind', ARGV[4],
  'failed_at', ARGV[5], 'acted', ARGV[6])
if ARGV[7] ~= '' then
  redis.call('HSET', key, 'acted_ids', ARGV[7])
else
  redis.call('HDEL', key, 'acted_ids')
end
if ARGV[8] ~= '' then
  redis.call('HSET', key, 'claim_intent', ARGV[8])
else
  redis.call('HDEL', key, 'claim_intent')
end
redis.call('HDEL', key, 'claimed_by', 'lease_until')
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZADD', KEYS[3], ARGV[5], ARGV[1])
return redis.call('HGETALL', key)
"""
)

# The release script takes the keys run and due, and the arguments id, claimed_by, now.
RELEASE_LUA = (
    "-- hooks:run:release\n"
    + _FENCE
    + """
if not fenced(KEYS[1], ARGV[2]) then
  return 0
end
redis.call('HSET', KEYS[1], 'status', 'pending', 'next_attempt_at', ARGV[3])
redis.call('HDEL', KEYS[1], 'claimed_by', 'lease_until')
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[1])
return 1
"""
)

# The retry script takes the keys run, due and failed, and the arguments id, now, claimed_by ('' for
# none) and lease_until ('' for none).
# Returns {outcome} or {outcome, HGETALL}.
RETRY_LUA = """
-- hooks:run:retry
local key = KEYS[1]
local status = redis.call('HGET', key, 'status')
if not status then
  return {'missing'}
end
if status ~= 'failed' then
  return {'not_failed'}
end
local acted = redis.call('HGET', key, 'acted') or ''
if acted == 'resume' or acted == 'take' then
  return {'refused', redis.call('HGETALL', key)}
end
if acted ~= 'start' then
  redis.call('HSET', key, 'acted', '')
  redis.call('HDEL', key, 'acted_ids', 'start')
end
redis.call('HDEL', key, 'claim_intent', 'failed_at')
if ARGV[3] ~= '' then
  redis.call('HSET', key, 'status', 'running', 'attempts', 1, 'next_attempt_at', ARGV[2], 'claimed_by', ARGV[3],
    'lease_until', ARGV[4])
  redis.call('ZADD', KEYS[2], ARGV[4], ARGV[1])
else
  redis.call('HSET', key, 'status', 'pending', 'attempts', 0, 'next_attempt_at', ARGV[2])
  redis.call('ZADD', KEYS[2], ARGV[2], ARGV[1])
end
redis.call('ZREM', KEYS[3], ARGV[1])
return {'ok', redis.call('HGETALL', key)}
"""

# The discard script takes the keys run, due and failed, and the arguments id.
DISCARD_LUA = """
-- hooks:run:discard
local status = redis.call('HGET', KEYS[1], 'status')
if not status then
  return 'missing'
end
if status ~= 'failed' then
  return 'not_failed'
end
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
return 'ok'
"""


__all__ = [
    "ACCEPT_LUA",
    "CLAIM_DUE_LUA",
    "CLEAR_INTENT_LUA",
    "DISCARD_LUA",
    "FAIL_LUA",
    "FINISH_LUA",
    "MARK_LUA",
    "RELEASE_LUA",
    "RENEW_LUA",
    "RESCHEDULE_LUA",
    "RETRY_LUA",
]
