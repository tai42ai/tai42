"""The per-target person registry — the runtime state that folds several channel addresses
into ONE identity for a single ``(target_kind, target_name)`` target.

Two Redis keyspaces, both built ONLY by the :class:`ConversationsSettings` helpers:

- a PERSON-ROW key (``conversations:person:<person_id>``) → the :class:`Person` JSON.
- a per-target PERSON INDEX — a HASH ``conversations:person_index:<target_kind>:<target_name>``
  mapping a ``door_address_key`` → the ``person_id`` that owns that address.

The ``door_address_key`` is a deterministic JSON array, the SINGLE canonical encoding of one
address, produced ONLY by :func:`_door_address_key`:

- a channel address → ``[channel, our_identity, address]``;
- an api address → ``["api", address]``.

Every part is charset-unconstrained (an address may carry ``:`` or quotes), so no hand-joined
delimiter form is ever used; :mod:`tai42_skeleton.conversations.pair_codes` imports this one
encoder rather than build its own, and no public store operation accepts a pre-built key — each
takes structured fields (``door, channel, our_identity, address`` or a :class:`PersonAddress`)
and derives the key internally, so no caller can ever encode divergently.

Every multi-key mutation is ONE Lua script (marker-commented), so racing writers produce one
outcome. The scripts build the person-row and person-index keys from a prefix IN-SCRIPT (a
survivor id or a target read off a row is not known to the caller), which assumes a single-node
Redis (not Cluster) exactly as the record store and the trigger-link store do. cjson round-trips
a person row through pydantic on every read, so a Lua-re-encoded row stays valid.

Durability is the deployment's Redis persistence, exactly as for conversation records. Persons
are user-generated runtime state and are deliberately OUT of backup (operator config only), the
same contract that excludes conversation records — this module registers no backup section.
Every failure path raises; the sole documented soft outcome is a :meth:`get_person` miss.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from tai42_contract.conversations import (
    ConversationDoor,
    ConversationTargetKind,
    CrossTargetMergeError,
    NotLinkedError,
    Person,
    PersonAddress,
)
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations.errors import NotSupportedError
from tai42_skeleton.utils.redis_typing import awaited, eval_script

_NO_BACKEND = "conversation persons require the redis conversations backend"


@dataclass(frozen=True)
class PairingTarget:
    """The ``(target_kind, target_name)`` a person is scoped to — persons never cross it."""

    target_kind: ConversationTargetKind
    target_name: str


def _door_address_key(*, door: ConversationDoor, channel: str | None, our_identity: str | None, address: str) -> str:
    """The single canonical encoding of one address for the person index and the open-code
    pointer: ``[channel, our_identity, address]`` for a channel address, ``["api", address]``
    for an api address. Deterministic JSON (no delimiter joining), so charset-unconstrained
    parts never collide."""
    if door == "channel":
        if channel is None or our_identity is None:
            raise ValueError("a channel address must carry channel and our_identity")
        return json.dumps([channel, our_identity, address], separators=(",", ":"))
    if channel is not None or our_identity is not None:
        raise ValueError("an api address carries no channel/our_identity")
    return json.dumps(["api", address], separators=(",", ":"))


def _address_key_of(address: PersonAddress) -> str:
    return _door_address_key(
        door=address.door, channel=address.channel, our_identity=address.our_identity, address=address.address
    )


def _as_str(value: Any) -> str:
    """Redis may hand back ``bytes`` or ``str`` depending on decode settings; the row JSON is
    utf-8, so normalize to ``str``."""
    return value.decode() if isinstance(value, bytes) else value


# One atomic index lookup. KEYS[1]=person index HASH. ARGV = door_address_key,
# person_key_prefix. HGET the owner, then GET its row in the SAME step, so a concurrent merge
# (which rewrites the index and deletes the absorbed row atomically) is never observed torn.
_LOOKUP_LUA = """
-- conversations:person:lookup
local person_id = redis.call('HGET', KEYS[1], ARGV[1])
if not person_id then
  return {'miss'}
end
local raw = redis.call('GET', ARGV[2] .. person_id)
if not raw then
  return {'torn', person_id}
end
return {'hit', raw}
"""

# One atomic get-or-create. KEYS[1]=person index HASH. ARGV = door_address_key,
# person_key_prefix, new_person_id, new_person_json, door, channel, our_identity, address,
# routes_json. Absent index field ⇒ HSET it and SET the new single-address row (created).
# Present ⇒ read the owning row, union the call's routes into the matching address, and
# re-persist only if that changed anything (existing). Never a double-create race.
_ENSURE_LUA = """
-- conversations:person:ensure
local index_key = KEYS[1]
local dak, prefix, new_id, new_json = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
local door, channel, our_identity, address = ARGV[5], ARGV[6], ARGV[7], ARGV[8]
local routes = cjson.decode(ARGV[9])
local existing = redis.call('HGET', index_key, dak)
if not existing then
  redis.call('HSET', index_key, dak, new_id)
  redis.call('SET', prefix .. new_id, new_json)
  return {'created', new_json}
end
local row_key = prefix .. existing
local raw = redis.call('GET', row_key)
if not raw then
  return {'torn', existing}
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
return {'existing', raw}
"""

# One atomic, order-independent merge. KEYS[1]/KEYS[2]=the two person-row keys.
# ARGV[1]=person_index_key_prefix. The survivor is the row with the earlier created_at (tie:
# lexically smaller person_id) — chosen IN-SCRIPT so no caller can decide it. The absorbed
# addresses union into the survivor, every index field pointing at the absorbed id is
# repointed at the survivor (the index itself is authoritative for which addresses belong,
# so no address key is re-derived here), and the absorbed row is deleted. Same-key ids are a
# no-op; differing targets refuse.
_MERGE_LUA = """
-- conversations:person:merge
local key_a, key_b = KEYS[1], KEYS[2]
local raw_a = redis.call('GET', key_a)
if not raw_a then
  return {'unknown', key_a}
end
if key_a == key_b then
  return {'ok', raw_a}
end
local raw_b = redis.call('GET', key_b)
if not raw_b then
  return {'unknown', key_b}
end
local a = cjson.decode(raw_a)
local b = cjson.decode(raw_b)
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
local survivor, absorbed, survivor_key, absorbed_key
if survivor_is_a then
  survivor, absorbed, survivor_key, absorbed_key = a, b, key_a, key_b
else
  survivor, absorbed, survivor_key, absorbed_key = b, a, key_b, key_a
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
local index_key = ARGV[1] .. survivor.target_kind .. ':' .. survivor.target_name
local fields = redis.call('HGETALL', index_key)
for i = 1, #fields, 2 do
  if fields[i + 1] == absorbed.person_id then
    redis.call('HSET', index_key, fields[i], survivor.person_id)
  end
end
local encoded = cjson.encode(survivor)
redis.call('SET', survivor_key, encoded)
redis.call('DEL', absorbed_key)
return {'ok', encoded}
"""

# One atomic detach. KEYS[1]=person row key, KEYS[2]=the detached address's fresh
# provisional row key. ARGV = person_index_key_prefix, door_address_key, new_person_id,
# new_created_at_iso, door, channel, our_identity, address. Refuses the person's only address
# (not_linked). Removes the matching address, writes it back as a fresh single-address person
# (so its next inbound is NOT a row creation — no re-greeting), and repoints its index field.
_DETACH_LUA = """
-- conversations:person:detach
local row_key, new_row_key = KEYS[1], KEYS[2]
local index_prefix, dak, new_id, new_created_at = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
local door, channel, our_identity, address = ARGV[5], ARGV[6], ARGV[7], ARGV[8]
local raw = redis.call('GET', row_key)
if not raw then
  return {'unknown', row_key}
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
}
local fresh_json = cjson.encode(fresh)
redis.call('SET', new_row_key, fresh_json)
local index_key = index_prefix .. person.target_kind .. ':' .. person.target_name
redis.call('HSET', index_key, dak, new_id)
return {'ok', fresh_json}
"""


class ConversationPersonStore:
    """The Redis-backed per-target person registry. Construction refuses with a loud 501
    without the redis conversations backend — a person folded across channels must not live in
    per-worker state that vanishes with the process."""

    def __init__(self, settings: ConversationsSettings) -> None:
        if settings.in_memory:
            raise NotSupportedError(_NO_BACKEND)
        self.settings = settings

    async def get_person(
        self,
        target: PairingTarget,
        *,
        door: ConversationDoor,
        channel: str | None,
        our_identity: str | None,
        address: str,
    ) -> Person | None:
        """The person owning ``address`` on ``target``, or ``None`` when the address has never
        been seen there. The index field and its row are read in one atomic step; an index
        field naming a missing row is corruption and raises."""
        dak = _door_address_key(door=door, channel=channel, our_identity=our_identity, address=address)
        index_key = self.settings.person_index_key(target.target_kind, target.target_name)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            result = await eval_script(r, _LOOKUP_LUA, 1, index_key, dak, self.settings.person_key_prefix)
        status = _as_str(result[0])
        if status == "miss":
            return None
        if status == "hit":
            return Person.model_validate_json(_as_str(result[1]))
        raise RuntimeError(f"conversations: person index names a missing row: {_as_str(result[1])!r}")

    async def get_by_id(self, person_id: str) -> Person | None:
        """The person row named by ``person_id``, or ``None`` when no such row exists. The
        aggregated person-thread read door resolves the ``bridge:@person:{person_id}`` key
        this way — against the store, never by parsing addresses out of the thread id."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            raw = await awaited(r.get(self.settings.person_key(person_id)))
        if raw is None:
            return None
        return Person.model_validate_json(_as_str(raw))

    async def ensure_provisional(self, target: PairingTarget, address_row: PersonAddress) -> tuple[Person, bool]:
        """Get-or-create the single-address person for ``address_row`` on ``target``, atomic
        against a concurrent first contact. On an EXISTING person, unions ``address_row``'s
        routes into its matching address. Returns ``(person, created)`` — ``created`` is True
        ONLY when this call wrote the row, the first-contact signal a greeting keys off."""
        new_id = str(uuid4())
        person = Person(
            person_id=new_id,
            target_kind=target.target_kind,
            target_name=target.target_name,
            created_at=datetime.now(UTC),
            addresses=[address_row],
        )
        dak = _address_key_of(address_row)
        index_key = self.settings.person_index_key(target.target_kind, target.target_name)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            result = await eval_script(
                r,
                _ENSURE_LUA,
                1,
                index_key,
                dak,
                self.settings.person_key_prefix,
                new_id,
                person.model_dump_json(),
                address_row.door,
                address_row.channel or "",
                address_row.our_identity or "",
                address_row.address,
                json.dumps(address_row.routes, separators=(",", ":")),
            )
        status = _as_str(result[0])
        if status == "created":
            return Person.model_validate_json(_as_str(result[1])), True
        if status == "existing":
            return Person.model_validate_json(_as_str(result[1])), False
        raise RuntimeError(f"conversations: person index names a missing row: {_as_str(result[1])!r}")

    async def merge(self, person_id_a: str, person_id_b: str) -> Person:
        """Merge two persons of the SAME target into one and return the survivor. The store
        picks the survivor (earlier ``created_at``; ties by lexically smaller ``person_id``),
        unions the absorbed addresses, repoints the absorbed index entries, and deletes the
        absorbed row — one atomic unit, so callers never decide the survivor. ``merge(P, P)``
        (both ids the same live person) is a no-op returning P. A cross-target merge raises
        :class:`CrossTargetMergeError`."""
        key_a = self.settings.person_key(person_id_a)
        key_b = self.settings.person_key(person_id_b)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            result = await eval_script(r, _MERGE_LUA, 2, key_a, key_b, self.settings.person_index_key_prefix)
        status = _as_str(result[0])
        if status == "ok":
            return Person.model_validate_json(_as_str(result[1]))
        if status == "cross_target":
            raise CrossTargetMergeError(f"cannot merge persons across targets ({person_id_a!r}, {person_id_b!r})")
        if status == "duplicate_address":
            raise RuntimeError(
                "conversations: merge found an address owned by both persons — the per-target "
                f"index's single-ownership is corrupt: {_as_str(result[1])!r}"
            )
        raise RuntimeError(f"conversations: merge names a missing person row: {_as_str(result[1])!r}")

    async def detach(
        self,
        person_id: str,
        *,
        door: ConversationDoor,
        channel: str | None,
        our_identity: str | None,
        address: str,
    ) -> Person:
        """Detach one address from a multi-address person and return it as its own fresh
        provisional person (the rest of the original stays linked). Refuses with
        :class:`NotLinkedError` when the address is the person's only one — it is already
        provisional, so there is nothing to unlink. The detached address's fresh row carries
        its accumulated routes, and being a fresh row means its next inbound is not a
        first-contact creation, so no greeting re-fires."""
        new_id = str(uuid4())
        dak = _door_address_key(door=door, channel=channel, our_identity=our_identity, address=address)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            result = await eval_script(
                r,
                _DETACH_LUA,
                2,
                self.settings.person_key(person_id),
                self.settings.person_key(new_id),
                self.settings.person_index_key_prefix,
                dak,
                new_id,
                datetime.now(UTC).isoformat(),
                door,
                channel or "",
                our_identity or "",
                address,
            )
        status = _as_str(result[0])
        if status == "ok":
            return Person.model_validate_json(_as_str(result[1]))
        if status == "not_linked":
            raise NotLinkedError(f"address is not part of a multi-address person: {person_id!r}")
        if status == "not_member":
            raise RuntimeError(f"conversations: detach asked to unlink an address not owned by person {person_id!r}")
        raise RuntimeError(f"conversations: detach names a missing person row: {_as_str(result[1])!r}")
