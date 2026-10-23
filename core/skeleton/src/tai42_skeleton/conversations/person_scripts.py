"""The atomic Redis Lua steps of the per-target person registry.

Each multi-key person mutation and each read that must see one consistent state is ONE script here,
run by :class:`tai42_skeleton.conversations.persons.ConversationPersonStore`. The scripts build the
person-row and person-index keys from a prefix in-script, which assumes a single-node Redis.
"""

from __future__ import annotations

from tai42_skeleton.conversations.record_scripts import _WRITER_LIB

# One atomic index lookup. KEYS[1]=person index HASH. ARGV = door_address_key,
# person_key_prefix, person_erasing_key_prefix. HGET the owner, then GET its row in the SAME step,
# so a concurrent merge (which rewrites the index and deletes the absorbed row atomically) is never
# observed torn. An owner whose erase mark stands answers ``{'erasing'}``: it is attributed as unlinked.
_LOOKUP_LUA = """
-- conversations:person:lookup
local person_id = redis.call('HGET', KEYS[1], ARGV[1])
if not person_id then
  return {'miss'}
end
if redis.call('EXISTS', ARGV[3] .. person_id) == 1 then
  return {'erasing'}
end
local raw = redis.call('GET', ARGV[2] .. person_id)
if not raw then
  return {'torn', person_id}
end
return {'hit', raw}
"""

# One atomic get-or-create. KEYS[1]=person index HASH. ARGV = door_address_key,
# person_key_prefix, new_person_id, new_person_json, door, channel, our_identity, address,
# routes_json, person_erasing_key_prefix, the address's door JSON on the target
# (``[target_kind, target_name, door_address_key]``), writer_prefixes. Absent index field ⇒ HSET it
# and SET the new single-address row (created). Present ⇒ an owner whose erase mark stands answers
# ``{'erasing', person_id}`` with nothing written; otherwise read the owning row, union the call's
# routes into the matching address, and re-persist only if that changed anything (existing).
# Never a double-create race. On both outcomes the person adopts every route-keyed thread the door
# wrote in while nobody held it (``_WRITER_LIB``): the door's visit list becomes the person's, and
# so does the writer of every record in those threads that carries the door.
_ENSURE_LUA = f"""
-- conversations:person:ensure
{_WRITER_LIB}
W = cjson.decode(ARGV[12])
local function w_adopt(door, person_id)
  local held = W.threads .. door
  for _, thread in ipairs(redis.call('SMEMBERS', held)) do
    redis.call('SADD', W.threads .. person_id, thread)
    for _, record_key in ipairs(w_thread_records(thread)) do
      if redis.call('HGET', record_key, 'writer') == door then redis.call('HSET', record_key, 'writer', person_id) end
    end
  end
  redis.call('DEL', held)
end
local index_key = KEYS[1]
local dak, prefix, new_id, new_json = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
local door, channel, our_identity, address = ARGV[5], ARGV[6], ARGV[7], ARGV[8]
local routes = cjson.decode(ARGV[9])
local existing = redis.call('HGET', index_key, dak)
if not existing then
  redis.call('HSET', index_key, dak, new_id)
  redis.call('SET', prefix .. new_id, new_json)
  w_adopt(ARGV[11], new_id)
  return {{'created', new_json}}
end
if redis.call('EXISTS', ARGV[10] .. existing) == 1 then
  return {{'erasing', existing}}
end
local row_key = prefix .. existing
local raw = redis.call('GET', row_key)
if not raw then
  return {{'torn', existing}}
end
local person = cjson.decode(raw)
for _, addr in ipairs(person.addresses) do
  local match = (addr.door == door)
  if match and door == 'channel' then
    match = addr.channel == channel and addr.our_identity == our_identity and addr.address == address
  elseif match then
    match = addr.address == address
  end
  if match then
    local changed = false
    for _, want in ipairs(routes) do
      local present = false
      for _, have in ipairs(addr.routes) do
        if have == want then present = true break end
      end
      if not present then
        table.insert(addr.routes, want)
        changed = true
      end
    end
    if changed then
      raw = cjson.encode(person)
      redis.call('SET', row_key, raw)
    end
    break
  end
end
w_adopt(ARGV[11], existing)
return {{'existing', raw}}
"""

# One atomic, order-independent merge. KEYS[1]/KEYS[2]=the two person-row keys, KEYS[3]/KEYS[4]=their
# erase-mark keys, KEYS[5]/KEYS[6]=their absorbed SETs. ARGV[1]=person_index_key_prefix,
# ARGV[2]=person_absorbed_into_key_prefix. The survivor is the row with the earlier created_at (tie:
# lexically smaller person_id) — chosen IN-SCRIPT so no caller can decide it. The absorbed addresses
# union into the survivor, every index field pointing at the absorbed id is repointed at the survivor
# (the index itself is authoritative for which addresses belong, so no address key is re-derived
# here), and the absorbed row is deleted. Same-key ids are a no-op; differing targets refuse; a
# person whose erase mark stands refuses with ``{'erasing', person_id}``.
#
# The same step adds the absorbed id and every id it had absorbed to the survivor's absorbed SET
# (a chain flattens), deletes the absorbed person's own SET, and points each of those ids at the
# survivor. The threads' writer stamps are left as they are: an absorbed id's writes stay stamped
# with that id, and the survivor's erase reaches them through its absorbed SET.
_MERGE_LUA = """
-- conversations:person:merge
local key_a, key_b = KEYS[1], KEYS[2]
local raw_a = redis.call('GET', key_a)
if not raw_a then
  return {'unknown', key_a}
end
local a = cjson.decode(raw_a)
if redis.call('EXISTS', KEYS[3]) == 1 then
  return {'erasing', a.person_id}
end
if key_a == key_b then
  return {'ok', raw_a}
end
local raw_b = redis.call('GET', key_b)
if not raw_b then
  return {'unknown', key_b}
end
local b = cjson.decode(raw_b)
if redis.call('EXISTS', KEYS[4]) == 1 then
  return {'erasing', b.person_id}
end
if a.target_kind ~= b.target_kind or a.target_name ~= b.target_name then
  return {'cross_target'}
end
local survivor_is_a
if a.created_at < b.created_at then
  survivor_is_a = true
elseif b.created_at < a.created_at then
  survivor_is_a = false
else
  survivor_is_a = a.person_id < b.person_id
end
local survivor, absorbed, survivor_key, absorbed_key, survivor_record, absorbed_record
if survivor_is_a then
  survivor, absorbed, survivor_key, absorbed_key = a, b, key_a, key_b
  survivor_record, absorbed_record = KEYS[5], KEYS[6]
else
  survivor, absorbed, survivor_key, absorbed_key = b, a, key_b, key_a
  survivor_record, absorbed_record = KEYS[6], KEYS[5]
end
for _, addr in ipairs(absorbed.addresses) do
  for _, have in ipairs(survivor.addresses) do
    -- Compare the SAME fields _door_address_key consumes (door + address, plus the channel
    -- identity for a channel door) rather than re-encoding the key in Lua. A match means one
    -- address is owned by two persons on one target — the per-target index's single-ownership
    -- invariant is already broken, so surface it loudly instead of forging a duplicate row.
    local dup = (have.door == addr.door and have.address == addr.address)
    if dup and addr.door == 'channel' then
      dup = have.channel == addr.channel and have.our_identity == addr.our_identity
    end
    if dup then
      return {'duplicate_address', addr.address}
    end
  end
  table.insert(survivor.addresses, addr)
end
local tk, tn = survivor.target_kind, survivor.target_name
local index_key = ARGV[1] .. tk .. ':' .. tn
local flat = redis.call('HGETALL', index_key)
for i = 1, #flat, 2 do
  if flat[i + 1] == absorbed.person_id then
    redis.call('HSET', index_key, flat[i], survivor.person_id)
  end
end
local encoded = cjson.encode(survivor)
redis.call('SET', survivor_key, encoded)
local ids = redis.call('SMEMBERS', absorbed_record)
ids[#ids + 1] = absorbed.person_id
for _, id in ipairs(ids) do
  redis.call('SADD', survivor_record, id)
  redis.call('SET', ARGV[2] .. id, survivor.person_id)
end
redis.call('DEL', absorbed_record)
redis.call('DEL', absorbed_key)
return {'ok', encoded}
"""

# One atomic detach. KEYS[1]=person row key, KEYS[2]=the detached address's fresh
# provisional row key, KEYS[3]=the person's erase-mark key. ARGV = person_index_key_prefix,
# door_address_key, new_person_id, new_created_at_iso, door, channel, our_identity, address.
# Refuses a person whose erase mark stands (erasing) and the person's only address (not_linked).
# Removes the matching address, writes it back as a fresh single-address person (so its next
# inbound is NOT a row creation — no re-greeting), and repoints its index field.
_DETACH_LUA = """
-- conversations:person:detach
local row_key, new_row_key = KEYS[1], KEYS[2]
local index_prefix, dak, new_id, new_created_at = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
local door, channel, our_identity, address = ARGV[5], ARGV[6], ARGV[7], ARGV[8]
local raw = redis.call('GET', row_key)
if not raw then
  return {'unknown', row_key}
end
if redis.call('EXISTS', KEYS[3]) == 1 then
  return {'erasing'}
end
local person = cjson.decode(raw)
if #person.addresses <= 1 then
  return {'not_linked'}
end
local found
for i, addr in ipairs(person.addresses) do
  local match = (addr.door == door)
  if match and door == 'channel' then
    match = addr.channel == channel and addr.our_identity == our_identity and addr.address == address
  elseif match then
    match = addr.address == address
  end
  if match then
    found = i
    break
  end
end
if not found then
  -- The row has >1 address (the single-address case returned 'not_linked' above), yet the
  -- asked-for address is not one of them: a caller/id mismatch, NOT the legitimate
  -- nothing-to-unlink case. Surface it distinctly instead of masking it as 'not_linked'.
  return {'not_member'}
end
local removed = table.remove(person.addresses, found)
redis.call('SET', row_key, cjson.encode(person))
local fresh = {
  person_id = new_id,
  target_kind = person.target_kind,
  target_name = person.target_name,
  created_at = new_created_at,
  addresses = {removed},
  -- Carry the stored locale onto the split-off person (nil assignment is a no-op), so the
  -- detached conversation still renders in the participant's resolved language.
  locale = person.locale,
}
local fresh_json = cjson.encode(fresh)
redis.call('SET', new_row_key, fresh_json)
local index_key = index_prefix .. person.target_kind .. ':' .. person.target_name
redis.call('HSET', index_key, dak, new_id)
return {'ok', fresh_json}
"""


# Whether a writer outside the erased ids keeps a route-keyed thread standing, shared by the
# erase's decision and its final step (after ``_WRITER_LIB``). ``erased`` is the erased ids as a set
# table; ``K`` holds the person row, erase mark and absorbed-into key prefixes. ``w_kept`` answers the
# first writer of the thread's records, in transcript order, that keeps the thread standing, else
# nil. A writer that is an erased id is going; a door writer (``writer == writer_door``: nobody held
# the door) keeps it; a person whose row stands keeps it unless its erase mark stands; a person whose
# row is gone is followed by its absorbed-into pointer to the survivor, judged the same way (an
# erased id or a marked survivor is going, an unmarked survivor keeps it); a gone person with no
# pointer was erased and is going. An index member whose row already expired is no record and counts
# for nothing. A record without a ``writer`` and a pointer whose survivor row is gone are torn and
# fail the step.
_KEEPS_LIB = """
local function w_keeps(who, door, erased, K)
  if erased[who] then return false end
  if who == door then return true end
  if redis.call('EXISTS', K.person .. who) == 1 then return redis.call('EXISTS', K.erasing .. who) == 0 end
  local survivor = redis.call('GET', K.into .. who)
  if not survivor then return false end
  if erased[survivor] then return false end
  if redis.call('EXISTS', K.person .. survivor) == 0 then
    error('conversations: the writer ' .. who .. ' points at the survivor ' .. survivor .. ', whose row is gone')
  end
  return redis.call('EXISTS', K.erasing .. survivor) == 0
end
local function w_kept(thread, erased, K)
  local judged = {}
  for _, record_key in ipairs(w_thread_records(thread)) do
    local who = redis.call('HGET', record_key, 'writer')
    if not who and redis.call('EXISTS', record_key) == 1 then
      error('conversations: a record of the route-keyed thread ' .. thread .. ' carries no writer: ' .. record_key)
    end
    if who and judged[who] == nil then
      judged[who] = w_keeps(who, redis.call('HGET', record_key, 'writer_door'), erased, K)
      if judged[who] then return who end
    end
  end
  return nil
end
"""

# One atomic person erase. KEYS[1]=person row key, KEYS[2]=the person's per-target index HASH,
# KEYS[3]=its erase-mark key, KEYS[4]=its absorbed SET, KEYS[5]=its recorded-routes key.
# ARGV[1]=person_id, ARGV[2]=the recorded-routes key prefix, ARGV[3]=the absorbed-into key prefix,
# ARGV[4]=writer_prefixes, ARGV[5]=the JSON list of the route-keyed threads the erase visited,
# ARGV[6]=person_key_prefix, ARGV[7]=person_erasing_key_prefix, ARGV[8..]=the absorbed ids the
# erase cleared. A thread the person's or an absorbed id's visit list (``writer_threads``) names
# that is not in the visited list answers {'unvisited', thread, id} with nothing written: the mark
# stands and a retry visits it. Otherwise each visited thread that no writer outside the erased ids
# keeps standing (``_KEEPS_LIB``) and that still holds a record whose ``writer`` is the person or an
# absorbed id (a write that landed after the erase cleared the thread) is added to the visit list
# of that record's ``writer_door``, so the next person to hold the door adopts the thread and that
# person's erase visits it; a record without a ``writer_door`` is torn and fails the step before
# anything is deleted. A thread a writer outside keeps standing is reached by that writer's erase.
# Then deletes each absorbed id's recorded routes and absorbed-into pointer, the absorbed SET, the
# person's recorded routes, every index field pointing at the person (removed by VALUE: the index
# is authoritative for which addresses belong), the visit list of the person and of each absorbed
# id, then the mark and the row. A record of a route-keyed thread keeps the ``writer`` it was
# written with. Same-key idempotent: a re-run on an already-erased person removes nothing. Returns
# {'ok', row removed, index fields removed}.
_ERASE_LUA = (
    """
-- conversations:person:erase
"""
    + _WRITER_LIB
    + _KEEPS_LIB
    + """
W = cjson.decode(ARGV[4])
local K = {person = ARGV[6], erasing = ARGV[7], into = ARGV[3]}
local visited = {}
local visited_list = cjson.decode(ARGV[5])
for _, thread in ipairs(visited_list) do visited[thread] = true end
local ids = {ARGV[1]}
for i = 8, #ARGV do ids[#ids + 1] = ARGV[i] end
local erased = {}
for _, id in ipairs(ids) do
  erased[id] = true
  for _, thread in ipairs(redis.call('SMEMBERS', W.threads .. id)) do
    if not visited[thread] then return {'unvisited', thread, id} end
  end
end
local handed = {}
for _, thread in ipairs(visited_list) do
  if not w_kept(thread, erased, K) then
    for _, record_key in ipairs(w_thread_records(thread)) do
      if erased[redis.call('HGET', record_key, 'writer')] then
        local door = redis.call('HGET', record_key, 'writer_door')
        if not door then
          error('conversations: a record of the route-keyed thread ' .. thread .. ' carries no writer_door: '
            .. record_key)
        end
        handed[#handed + 1] = {door, thread}
      end
    end
  end
end
for _, pair in ipairs(handed) do redis.call('SADD', W.threads .. pair[1], pair[2]) end
for i = 8, #ARGV do
  redis.call('DEL', ARGV[2] .. ARGV[i], ARGV[3] .. ARGV[i])
end
redis.call('DEL', KEYS[4], KEYS[5])
local fields = redis.call('HGETALL', KEYS[2])
local removed_fields = 0
for i = 1, #fields, 2 do
  if fields[i + 1] == ARGV[1] then
    redis.call('HDEL', KEYS[2], fields[i])
    removed_fields = removed_fields + 1
  end
end
for _, id in ipairs(ids) do redis.call('DEL', W.threads .. id) end
redis.call('DEL', KEYS[3])
local removed_row = redis.call('DEL', KEYS[1])
return {'ok', removed_row, removed_fields}
"""
)

# Whether a writer outside the erased ids keeps a route-keyed thread standing, read-only in one
# step (``_KEEPS_LIB``). ARGV[1]=the thread id, ARGV[2]=writer_prefixes, ARGV[3]=person_key_prefix,
# ARGV[4]=person_erasing_key_prefix, ARGV[5]=person_absorbed_into_key_prefix, ARGV[6..]=the erased
# ids. Answers the first writer that keeps the thread standing, else ''.
_OUTSIDE_WRITER_LUA = (
    """
-- conversations:thread:outside_writer
"""
    + _WRITER_LIB
    + _KEEPS_LIB
    + """
W = cjson.decode(ARGV[2])
local erased = {}
for i = 6, #ARGV do erased[ARGV[i]] = true end
return w_kept(ARGV[1], erased, {person = ARGV[3], erasing = ARGV[4], into = ARGV[5]}) or ''
"""
)

# The erase's one read-only step. KEYS[1]=person row key, KEYS[2]=its absorbed SET, KEYS[3]=its
# recorded-routes key, KEYS[4]=its absorbed-into pointer. ARGV[1]=the recorded-routes key prefix,
# ARGV[2]=person_key_prefix, ARGV[3]=the writer-threads key prefix, ARGV[4]=person_id. Answers {row
# or '', pointer or '', 1 when the pointer's survivor row stands else 0, the recorded routes, per
# absorbed id {id, its recorded routes}, the route-keyed threads the person or an absorbed id is a
# writer of}.
_READ_FOR_ERASE_LUA = """
-- conversations:person:read_for_erase
local row = redis.call('GET', KEYS[1]) or ''
local pointer = redis.call('GET', KEYS[4]) or ''
local survivor_stands = 0
if pointer ~= '' then survivor_stands = redis.call('EXISTS', ARGV[2] .. pointer) end
local threads = {}
local function add_threads(id)
  for _, thread in ipairs(redis.call('SMEMBERS', ARGV[3] .. id)) do threads[thread] = true end
end
add_threads(ARGV[4])
local absorbed = {}
for _, id in ipairs(redis.call('SMEMBERS', KEYS[2])) do
  absorbed[#absorbed + 1] = {id, redis.call('SMEMBERS', ARGV[1] .. id)}
  add_threads(id)
end
local route_threads = {}
for thread in pairs(threads) do route_threads[#route_threads + 1] = thread end
return {row, pointer, survivor_stands, redis.call('SMEMBERS', KEYS[3]), absorbed, route_threads}
"""

# Set a person's erase mark only while its row stands. KEYS[1]=person row key, KEYS[2]=its
# erase-mark key. Answers 1 when the mark was set (the row stood), 0 when the row is gone.
_MARK_ERASING_LUA = """
-- conversations:person:mark_erasing
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
redis.call('SET', KEYS[2], '1')
return 1
"""

# A person row with its erase mark, in one step. KEYS[1]=person row key, KEYS[2]=its erase-mark key.
_GET_BY_ID_LUA = """
-- conversations:person:get_by_id
return {redis.call('GET', KEYS[1]) or '', redis.call('EXISTS', KEYS[2])}
"""

# Set (or clear) a person's stored locale in place. KEYS[1]=person row key, KEYS[2]=its erase-mark
# key. ARGV[1]=mode ("set"/"clear"), ARGV[2]=the canonical locale when mode is "set".
# Read-modify-write of the one JSON row in a single script so a concurrent read never sees a
# half-written row; a "clear" removes the field so the row reads back as an explicit absent locale.
# Returns {"missing"} when no such person row exists, {"erasing"} when its erase mark stands
# (nothing written), else {"ok", updated_json}.
_SET_LOCALE_LUA = """
-- conversations:person:set_locale
local raw = redis.call('GET', KEYS[1])
if not raw then
  return {'missing'}
end
if redis.call('EXISTS', KEYS[2]) == 1 then
  return {'erasing'}
end
local person = cjson.decode(raw)
if ARGV[1] == 'clear' then
  person.locale = nil
else
  person.locale = ARGV[2]
end
local encoded = cjson.encode(person)
redis.call('SET', KEYS[1], encoded)
return {'ok', encoded}
"""
