"""An in-memory fake of the redis surface the answer/record store uses.

Covers the HASH + STRING commands the record store calls, the LIST commands the send
ledger calls, the ZSET commands the status and thread record indexes use, plus the record
Lua scripts (dispatched by marker comment, re-implemented here), and the pub/sub the thread
lease's release announcement travels on. Single-threaded async, so each faked ``eval`` runs
atomically.

``ttl_ms`` records the expiry each command applied, so a test can assert the retention
TTL lands only on a terminal record.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from tai42_contract.conversations import Person, TargetConversationConfig

from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.persons import _address_key_of
from tai42_skeleton.conversations.record_scripts import _INDEXED_STATUSES
from tai42_skeleton.conversations.settings import ConversationsSettings

#: The count of status indexes in the shared record key layout.
_N = len(_INDEXED_STATUSES)


class FakeRecordPubSub:
    """A subscriber of :class:`FakeRecordRedis`: confirmations and published messages, in order.

    Its connection never drops, so ``on_reconnect`` is kept and never run.
    """

    def __init__(self, fake: FakeRecordRedis, on_reconnect: Callable[[], None] | None = None) -> None:
        self._fake = fake
        self.on_reconnect = on_reconnect
        self._messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._channels: set[str] = set()

    def deliver(self, message: dict[str, Any]) -> None:
        self._messages.put_nowait(message)

    async def subscribe(self, *channels: str) -> None:
        for channel in channels:
            self._channels.add(channel)
            self._fake._subscribers.setdefault(channel, set()).add(self)
            self.deliver({"type": "subscribe", "pattern": None, "channel": channel, "data": len(self._channels)})

    async def get_message(
        self, ignore_subscribe_messages: bool = False, timeout: float | None = 0.0
    ) -> dict[str, Any] | None:
        try:
            if timeout is None:
                message = await self._messages.get()
            else:
                async with asyncio.timeout(timeout):
                    message = await self._messages.get()
        except TimeoutError:
            return None
        if ignore_subscribe_messages and message["type"] == "subscribe":
            return None
        return message

    async def aclose(self) -> None:
        for channel in self._channels:
            self._fake._subscribers.get(channel, set()).discard(self)
        self._channels.clear()


def _compact(value: Any) -> str:
    """``value`` as the compact JSON a script's ``cjson.encode`` writes."""
    return json.dumps(value, separators=(",", ":"))


class _Writers:
    """The writer-stamp helpers the scripts share, over the fake's maps.

    Built from a script's decoded writer prefixes; each method is the helper of the same name in
    the scripts' writer library.
    """

    def __init__(self, fake: FakeRecordRedis, prefixes: str) -> None:
        self._fake = fake
        decoded = json.loads(prefixes)
        self.index: str = decoded["index"]
        self.threads: str = decoded["threads"]
        self.records: str = decoded["records"]
        self.thread_index: str = decoded["thread_index"]
        self.route_thread: str = decoded["route_thread"]

    def holder(self, door: str) -> str:
        kind, name, door_address_key = json.loads(door)
        return self._fake._hashes.get(f"{self.index}{kind}:{name}", {}).get(door_address_key) or door

    def stamp(self, thread: str, door: str, record_key: str) -> None:
        who = self.holder(door)
        self._fake._sadd(self.threads + who, thread)
        self._fake._hset(record_key, "writer", who)
        self._fake._hset(record_key, "writer_door", door)

    def thread_records(self, thread: str) -> list[str]:
        rest = thread.removeprefix(self.route_thread)
        if rest == thread or ":" not in rest:
            raise RuntimeError(f"conversations: a writer threads entry is not route-keyed: {thread}")
        route = rest.split(":", 1)[0]
        return [self.records + message_id for message_id in self._fake._ordered(f"{self.thread_index}{route}:{thread}")]

    def adopt(self, door: str, person_id: str) -> None:
        held = self.threads + door
        for thread in sorted(self._fake._sets.get(held, set())):
            self._fake._sadd(self.threads + person_id, thread)
            for record_key in self.thread_records(thread):
                row = self._fake._hashes.get(record_key)
                if row is not None and row.get("writer") == door:
                    row["writer"] = person_id
        self._fake._drop(held)


class FakeRecordRedis:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[FakeRecordPubSub]] = {}
        self._strings: dict[str, str] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._lists: dict[str, list[str]] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._sets: dict[str, set[str]] = {}
        # The live expiry (in milliseconds) each key carries, so a test can assert the
        # retention TTL lands only on a terminal transition.
        self.ttl_ms: dict[str, int] = {}

    def advance(self, seconds: float) -> None:
        """Age every keyed TTL by ``seconds`` and evict what has expired — the reaper Redis
        runs on its own. ``ttl_ms`` holds the remaining lifetime, so a test that never calls
        this sees the applied TTL unchanged (the record suite asserts it directly)."""
        elapsed = int(seconds * 1000)
        for key in list(self.ttl_ms):
            self.ttl_ms[key] -= elapsed
            if self.ttl_ms[key] <= 0:
                self.ttl_ms.pop(key, None)
                for store in (self._strings, self._hashes, self._lists, self._zsets):
                    store.pop(key, None)

    def seed_hash(self, key: str, fields: dict[str, str]) -> None:
        """Plant a raw hash under ``key`` — the seam a test uses to put a row no store
        write could produce (a corrupt or foreign row) in the record keyspace."""
        self._hashes[key] = dict(fields)

    def seed_route(self, route_name: str) -> None:
        """Plant the ROUTING ROW for ``route_name`` — the key the record create checks
        before it writes the thread indexes. Without it every create here would behave as
        one landing after the route's delete and index nothing."""
        self._strings[ConversationsSettings().route_key(route_name)] = "{}"

    def seed_person(self, person: Person) -> None:
        """Plant ``person``'s row and its address→person index mappings the way the person store
        writes them, so a person-thread record create finds its person standing."""
        settings = ConversationsSettings()
        self._strings[settings.person_key(person.person_id)] = person.model_dump_json()
        index_key = settings.person_index_key(person.target_kind, person.target_name)
        for address in person.addresses:
            self._hashes.setdefault(index_key, {})[_address_key_of(address)] = person.person_id

    def seed_target_config(self, config: TargetConversationConfig) -> None:
        """Plant a per-target config row the way the config store's upsert writes it: the
        row, its name-index member and a fresh write token for the map (the fake carries no
        config put Lua)."""
        settings = ConversationsSettings()
        self._strings[settings.target_config_key(config.target_kind, config.target_name)] = config.model_dump_json()
        self._sets.setdefault(settings.target_config_names_key, set()).add(f"{config.target_kind}:{config.target_name}")
        self._strings[settings.target_config_version_key] = uuid.uuid4().hex

    def drop_route(self, route_name: str) -> None:
        """Remove the routing row, so a create lands as one racing the route's delete."""
        self._strings.pop(ConversationsSettings().route_key(route_name), None)

    # -- the server-side primitives the faked scripts write through ---------
    def _stores(self) -> tuple[dict, ...]:
        return (self._strings, self._hashes, self._lists, self._zsets, self._sets)

    def _exists(self, key: str) -> bool:
        return any(key in store for store in self._stores())

    def _drop(self, key: str) -> int:
        self.ttl_ms.pop(key, None)
        held = [store.pop(key, None) for store in self._stores()]
        return 1 if any(value is not None for value in held) else 0

    def _hset(self, key: str, field: str, value: str) -> None:
        self._hashes.setdefault(key, {})[field] = value

    def _hdel(self, key: str, field: str) -> None:
        # Redis drops a hash that holds no field, so neither does the fake.
        target = self._hashes.get(key)
        if target is None:
            return
        target.pop(field, None)
        if not target:
            self._drop(key)

    def _hincrby(self, key: str, field: str, amount: int) -> int:
        target = self._hashes.setdefault(key, {})
        new = int(target.get(field, "0")) + amount
        target[field] = str(new)
        return new

    def _sadd(self, key: str, member: str) -> None:
        self._sets.setdefault(key, set()).add(member)

    def _srem(self, key: str, member: str) -> None:
        target = self._sets.get(key)
        if target is None:
            return
        target.discard(member)
        if not target:
            self._drop(key)

    def _pexpire(self, key: str, ttl_ms: str | int) -> None:
        if self._exists(key):
            self.ttl_ms[key] = int(ttl_ms)

    # The ``owned_keys`` helpers the record scripts share: every key outside a row that names the
    # record is named on the row's ``owned_keys`` JSON list by the write that makes it.
    def _owned_names(self, record_key: str) -> list[str]:
        raw = self._hashes.get(record_key, {}).get("owned_keys")
        return json.loads(raw) if raw else []

    def _own(self, record_key: str, name: str) -> bool:
        names = self._owned_names(record_key)
        if name in names:
            return False
        names.append(name)
        self._hset(record_key, "owned_keys", _compact(names))
        return True

    def _own_ref(self, record_key: str, ref: str, message_id: str, ttl_ms: str) -> None:
        if self._own(record_key, ref):
            self._lists.setdefault(ref, []).append(message_id)
        self._pexpire(ref, ttl_ms)

    def _own_value(self, record_key: str, key: str, message_id: str, ttl_ms: str) -> None:
        self._strings[key] = message_id
        self.ttl_ms[key] = int(ttl_ms)
        self._own(record_key, key)

    def _own_member(self, record_key: str, set_key: str, message_id: str) -> None:
        self._sadd(set_key, message_id)
        self._own(record_key, set_key)

    def _refresh_refs(self, record_key: str, ttl_ms: str) -> None:
        for name in self._owned_names(record_key):
            if name in self._lists:
                self.ttl_ms[name] = int(ttl_ms)

    def _forget_owned(self, record_key: str, message_id: str) -> None:
        for name in self._owned_names(record_key):
            if name in self._lists:
                kept = [held for held in self._lists[name] if held != message_id]
                if kept:
                    self._lists[name] = kept
                else:
                    self._drop(name)
            elif name in self._sets:
                self._srem(name, message_id)
            elif self._strings.get(name) == message_id:
                self._drop(name)

    def _delete_row(self, keys: list[str], record_key: str, message_id: str) -> int:
        """A row's delete with everything that names it, the scripts' shared ``delete_row``."""
        self._forget_owned(record_key, message_id)
        removed = self._drop(record_key)
        for index_key in keys[1 : _N + 1]:
            self._zsets.get(index_key, {}).pop(message_id, None)
        return removed

    # -- sorted-set commands (the status / thread record indexes) ------------
    def pubsub(self, *, on_reconnect: Callable[[], None] | None = None) -> FakeRecordPubSub:
        return FakeRecordPubSub(self, on_reconnect)

    async def publish(self, channel: str, message: str) -> int:
        receivers = list(self._subscribers.get(channel, ()))
        for receiver in receivers:
            receiver.deliver({"type": "message", "pattern": None, "channel": channel, "data": message})
        return len(receivers)

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        target = self._zsets.setdefault(key, {})
        added = sum(1 for member in mapping if member not in target)
        target.update(mapping)
        return added

    async def zrem(self, key: str, *members: str) -> int:
        target = self._zsets.get(key, {})
        return sum(1 for member in members if target.pop(member, None) is not None)

    def _ordered(self, key: str) -> list[str]:
        return [m for m, _ in sorted(self._zsets.get(key, {}).items(), key=lambda item: (item[1], item[0]))]

    def _window(self, ordered: list[str], key: str, start: int, end: int, withscores: bool) -> list:
        stop = len(ordered) if end == -1 else end + 1
        window = ordered[start:stop]
        if withscores:
            return [(member, self._zsets[key][member]) for member in window]
        return window

    async def zrange(self, key: str, start: int, end: int, withscores: bool = False) -> list:
        return self._window(self._ordered(key), key, start, end, withscores)

    async def zrevrange(self, key: str, start: int, end: int, withscores: bool = False) -> list:
        return self._window(list(reversed(self._ordered(key))), key, start, end, withscores)

    @staticmethod
    def _score_bound(value: str | float) -> tuple[float, bool]:
        """Parse a redis score-range bound into ``(number, inclusive)``.

        A leading ``(`` is an EXCLUSIVE bound, ``+inf``/``-inf`` are the open ends; everything else
        is inclusive — exactly the ``ZRANGEBYSCORE`` min/max grammar the store relies on.
        """
        text = str(value)
        if text.startswith("("):
            return float(text[1:]), False
        return float(text), True

    async def zrangebyscore(
        self,
        key: str,
        minimum: str | float,
        maximum: str | float,
        start: int | None = None,
        num: int | None = None,
    ) -> list[str]:
        low, low_inclusive = self._score_bound(minimum)
        high, high_inclusive = self._score_bound(maximum)

        def _in_range(score: float) -> bool:
            above = score >= low if low_inclusive else score > low
            below = score <= high if high_inclusive else score < high
            return above and below

        matched = [m for m in self._ordered(key) if _in_range(self._zsets[key][m])]
        if start is None and num is None:
            return matched
        offset = start or 0
        return matched[offset : offset + num] if num is not None else matched[offset:]

    async def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, {}))

    async def zscore(self, key: str, member: str) -> float | None:
        return self._zsets.get(key, {}).get(member)

    async def exists(self, key: str) -> int:
        return 1 if self._exists(key) else 0

    async def zremrangebyscore(self, key: str, minimum: str | float, maximum: str | float) -> int:
        low, high = float(minimum), float(maximum)
        target = self._zsets.get(key, {})
        doomed = [member for member, score in target.items() if low <= score <= high]
        for member in doomed:
            del target[member]
        return len(doomed)

    # -- list commands (the channel send ledger) -----------------------------
    async def rpush(self, key: str, *values: str) -> int:
        target = self._lists.setdefault(key, [])
        target.extend(values)
        return len(target)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        entries = self._lists.get(key, [])
        stop = len(entries) if end == -1 else end + 1
        return entries[start:stop]

    async def expire(self, key: str, seconds: int) -> bool:
        # As redis EXPIRE: a TTL only lands on a key that EXISTS, and a missing key is left
        # absent (never recreated), so the command reports whether it applied.
        if not self._exists(key):
            return False
        self.ttl_ms[key] = seconds * 1000
        return True

    async def delete(self, key: str) -> int:
        return self._drop(key)

    # -- set commands (the config name index) -------------------------------
    async def smembers(self, key: str) -> set[str]:
        return set(self._sets.get(key, set()))

    # -- string commands -----------------------------------------------------
    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self._strings.get(key) for key in keys]

    async def get(self, key: str) -> str | None:
        # Captures the value THEN yields (as a real single-server redis does: each command runs
        # to completion server-side, so two concurrent readers both observe pre-write state).
        # This makes a non-atomic check-then-act genuinely interleave — the read ops
        # (``get``/``hget``) yield, while the atomic burn ``getdel`` never does, so the
        # single-use pair-code redeem stays exclusive.
        value = self._strings.get(key)
        await asyncio.sleep(0)
        return value

    async def set(
        self, key: str, value: str, ex: int | None = None, px: int | None = None, nx: bool = False
    ) -> bool | None:
        if nx and key in self._strings:
            return None
        self._strings[key] = value
        if ex is not None:
            self.ttl_ms[key] = ex * 1000
        if px is not None:
            self.ttl_ms[key] = px
        return True

    async def getdel(self, key: str) -> str | None:
        # The atomic single-use burn — no yield inside, so two concurrent redeems admit
        # exactly one winner.
        self.ttl_ms.pop(key, None)
        return self._strings.pop(key, None)

    # -- hash commands -------------------------------------------------------
    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    async def hget(self, key: str, field: str) -> str | None:
        # Reads THEN yields, exactly like ``get``: a hypothetical NON-atomic ensure that HGETs
        # the index and only then HSETs would have both racers observe the same empty index and
        # double-create, so the race test catches it. The real ensure runs as ONE atomic
        # ``eval`` (which never routes through this method), so it stays a single create.
        value = self._hashes.get(key, {}).get(field)
        await asyncio.sleep(0)
        return value

    async def hset(self, key: str, field: str, value: str) -> int:
        target = self._hashes.setdefault(key, {})
        is_new = field not in target
        target[field] = value
        return 1 if is_new else 0

    async def incr(self, key: str) -> int:
        new = int(self._strings.get(key, "0")) + 1
        self._strings[key] = str(new)
        return new

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        target = self._hashes.setdefault(key, {})
        new = int(target.get(field, "0")) + amount
        target[field] = str(new)
        return new

    def scan_iter(self, match: str) -> AsyncIterator[str]:
        prefix = match.rstrip("*")

        async def _iter() -> AsyncIterator[str]:
            for key in list(self._hashes):
                if key.startswith(prefix):
                    yield key

        return _iter()

    # -- Lua eval (re-implements the record scripts by marker) ---------------
    @staticmethod
    def _foreign_lease(hashed: dict[str, str] | None, *, now: str, token: str) -> bool:
        """Whether a DIFFERENT worker holds a live delivery lease on the row."""
        claim = (hashed or {}).get("claim", "")
        if not claim:
            return False
        ctoken, cexp = claim.split(":", 1)
        return ctoken != token and float(cexp) > float(now)

    def _reindex(self, index_keys: list[str], member: str, score: str) -> None:
        """Move ``member`` into the target index (the LAST key) and out of every other, as
        the scripts' shared reindex step does."""
        for index_key in index_keys[:-1]:
            self._zsets.get(index_key, {}).pop(member, None)
        self._zsets.setdefault(index_keys[-1], {})[member] = float(score)

    # -- the record and person scripts that stamp and read a route-keyed thread's writers -----
    def _create_script(self, keys: list[str], argv: list[str]) -> list:
        # The shared layout with the routing row, then the optional dedupe key, mode key, person
        # group and inbound ref key, each announced by its ARGV flag.
        record_key, message_id, thread_id = keys[0], argv[7], argv[9]
        thread_key, route_threads_key, route_row_key = keys[_N + 2], keys[_N + 3], keys[_N + 4]
        slot = _N + 5
        claim_key = mode_key = person_row = person_mark = person_routes = ref_key = None
        if argv[11] != "":
            claim_key, slot = keys[slot], slot + 1
        if argv[12] != "":
            mode_key, slot = keys[slot], slot + 1
        if argv[13] != "":
            person_row, person_mark, person_routes = keys[slot : slot + 3]
            slot += 3
        if argv[15] != "":
            ref_key = keys[slot]
        claimed = self._strings.get(claim_key) if claim_key is not None else None
        if claimed is not None and claimed != message_id:
            return [-1, claimed]
        if person_row is not None and (not self._exists(person_row) or self._exists(person_mark or "")):
            return [-2, ""]
        self._hashes.setdefault(record_key, {}).update(
            data=argv[0],
            delivery_status=argv[1],
            sends=argv[2],
            attempts=argv[3],
            claim="",
            grace_deadline="",
            updated_at=argv[4],
            intake_claim=argv[6],
        )
        if argv[5]:
            self.ttl_ms[record_key] = int(argv[5])
        self._reindex(keys[1 : _N + 2], message_id, argv[8])
        # Written only while the route still routes: the indexes of a route whose row is gone
        # are reclaimed already, so re-creating them would strand the pair. The record is marked
        # so the operator read carries the honest marker, not only a log line.
        indexed = 1
        if not self._exists(route_row_key):
            self._hset(record_key, "route_missing", "1")
            indexed = 0
        else:
            self._zsets.setdefault(thread_key, {})[message_id] = float(argv[10])
            self._zsets.setdefault(route_threads_key, {})[thread_id] = float(argv[10])
            if person_routes is not None:
                self._sadd(person_routes, argv[14])
        if ref_key is not None:
            self._own_ref(record_key, ref_key, message_id, argv[15])
        if claim_key is not None and claimed is None:
            self._strings[claim_key] = message_id
        # As the script's EXPIRE: lands only on a live key, never recreates a missing one.
        if mode_key is not None and mode_key in self._strings:
            self.ttl_ms[mode_key] = int(argv[12]) * 1000
        if argv[16] != "":
            _Writers(self, argv[17]).stamp(thread_id, argv[16], record_key)
        return [indexed, message_id]

    def _delete_script(self, keys: list[str], argv: list[str]) -> int:
        removed = self._delete_row(keys, keys[0], argv[0])
        thread_key, route_threads_key = keys[_N + 1], keys[_N + 2]
        self._zsets.get(thread_key, {}).pop(argv[0], None)
        if not self._zsets.get(thread_key):
            self._zsets.get(route_threads_key, {}).pop(argv[1], None)
        return removed

    def _begin_send_script(self, keys: list[str], argv: list[str]) -> int:
        record_key, message_id, placed_thread, placed_door = keys[0], argv[3], argv[5], argv[7]
        if self._exists(record_key):
            return 0
        self._hashes[record_key] = {
            "data": argv[0],
            "delivery_status": DeliveryStatus.SENDING.value,
            "sends": "[]",
            "attempts": argv[1],
            "claim": "",
            "grace_deadline": "",
            "updated_at": argv[2],
            "intake_claim": "",
            "placed_thread": placed_thread,
        }
        if placed_door != "":
            _Writers(self, argv[8]).stamp(placed_thread, placed_door, record_key)
        self._reindex(keys[1 : _N + 2], message_id, argv[4])
        self._own_member(record_key, keys[_N + 2], message_id)
        if argv[6] != "":
            self._own_value(record_key, keys[_N + 3], message_id, argv[6])
        return 1

    def _send_placement(
        self,
        keys: list[str],
        *,
        message_id: str,
        place_thread: str,
        created: str,
        route_name: str,
        person_flag: str,
        landing_door: str,
        prefixes: str,
        write,
    ) -> list:
        """A send script's placement: the checks, then ``write`` (the row's new state), then the placement writes."""
        record_key = keys[0]
        person_row = person_mark = person_routes = None
        after = _N + 6
        if person_flag != "":
            person_row, person_mark, person_routes = keys[after : after + 3]
            after += 3
        if not self._exists(record_key):
            return [-1, "gone"]
        row = self._hashes.get(record_key, {})
        status = row.get("delivery_status")
        if status != DeliveryStatus.SENDING.value:
            return [-2, status or ""]
        forgotten = row.get("forgotten_threads")
        if forgotten and place_thread in json.loads(forgotten):
            self._delete_row(keys, record_key, message_id)
            return [-1, "forgotten"]
        if person_row is not None and (not self._exists(person_row) or self._exists(person_mark or "")):
            self._delete_row(keys, record_key, message_id)
            return [-1, "person_closed"]
        final = write(after)
        self._srem(keys[_N + 5], message_id)
        if landing_door != "":
            _Writers(self, prefixes).stamp(place_thread, landing_door, record_key)
        placed = 1
        if not self._exists(keys[_N + 4]):
            self._hset(record_key, "route_missing", "1")
            placed = 0
        else:
            self._zsets.setdefault(keys[_N + 2], {})[message_id] = float(created)
            self._zsets.setdefault(keys[_N + 3], {})[place_thread] = float(created)
            if person_routes is not None:
                self._sadd(person_routes, route_name)
        return [placed, final]

    def _complete_send_script(self, keys: list[str], argv: list[str]) -> list:
        record_key, message_id = keys[0], argv[4]
        delivered_index = keys[1 + _INDEXED_STATUSES.index(DeliveryStatus.DELIVERED)]

        def write(after: int) -> str:
            row = self._hashes[record_key]
            final = "delivered" if row.get("ask_reply") else "provisional"
            for index_key in keys[1 : _N + 1]:
                self._zsets.get(index_key, {}).pop(message_id, None)
            row.update(data=argv[0], delivery_status=final, sends=argv[1], attempts=argv[2], updated_at=argv[3])
            if final == "delivered":
                row["grace_deadline"] = ""
                self._zsets.setdefault(delivered_index, {})[message_id] = float(argv[6])
                self.ttl_ms[record_key] = int(argv[8])
            else:
                row["grace_deadline"] = argv[7]
                self._zsets.setdefault(keys[_N + 1], {})[message_id] = float(argv[5])
            n_k3 = int(argv[13])
            for key in keys[after : after + n_k3]:
                self._own_value(record_key, key, message_id, argv[8])
            for key in keys[after + n_k3 :]:
                self._own_ref(record_key, key, message_id, argv[8])
            if final == "delivered":
                self._refresh_refs(record_key, argv[8])
            return final

        return self._send_placement(
            keys,
            message_id=message_id,
            place_thread=argv[9],
            created=argv[10],
            route_name=argv[11],
            person_flag=argv[12],
            landing_door=argv[14],
            prefixes=argv[15],
            write=write,
        )

    def _end_send_script(self, keys: list[str], argv: list[str]) -> list:
        record_key, message_id = keys[0], argv[4]
        delivered_index = keys[1 + _INDEXED_STATUSES.index(DeliveryStatus.DELIVERED)]

        def write(after: int) -> str:
            row = self._hashes[record_key]
            final, content = argv[2], argv[0]
            if row.get("ask_reply"):
                final, content = "delivered", argv[1]
            row.update(data=content, delivery_status=final, updated_at=argv[3], claim="", grace_deadline="")
            for index_key in keys[1 : _N + 1]:
                self._zsets.get(index_key, {}).pop(message_id, None)
            target = delivered_index if final == "delivered" else keys[_N + 1]
            self._zsets.setdefault(target, {})[message_id] = float(argv[5])
            self.ttl_ms[record_key] = int(argv[6])
            self._refresh_refs(record_key, argv[6])
            return final

        return self._send_placement(
            keys,
            message_id=message_id,
            place_thread=argv[7],
            created=argv[8],
            route_name=argv[9],
            person_flag=argv[10],
            landing_door=self._hashes.get(record_key, {}).get("writer_door") or "",
            prefixes=argv[11],
            write=write,
        )

    def _ask_reply_script(self, keys: list[str], argv: list[str]) -> int:
        record_key, message_id = keys[0], argv[1]
        if not self._exists(record_key):
            return -1
        row = self._hashes.get(record_key, {})
        status = row.get("delivery_status")
        if status != argv[5]:
            return -3
        row.update(ask_reply=argv[0], updated_at=argv[2])
        settles = status in ("provisional", "failed", "outcome_unknown")
        if settles:
            if argv[6] != "":
                row["data"] = argv[6]
            row.update(delivery_status="delivered", claim="", grace_deadline="")
            self._reindex(keys[1 : _N + 2], message_id, argv[4])
            self.ttl_ms[record_key] = int(argv[3])
        if argv[7] != "":
            self._own_ref(record_key, keys[_N + 2], message_id, argv[3])
        if settles:
            self._refresh_refs(record_key, argv[3])
        return 1

    def _forget_sending_script(self, keys: list[str], argv: list[str]) -> int:
        inflight, record_key = keys[0], keys[1]
        row = self._hashes.get(record_key)
        if row is None or row.get("delivery_status") != DeliveryStatus.SENDING.value:
            self._srem(inflight, argv[0])
            return 0
        threads = json.loads(row["forgotten_threads"]) if row.get("forgotten_threads") else []
        if argv[1] in threads:
            return 1
        threads.append(argv[1])
        row["forgotten_threads"] = _compact(threads)
        return 1

    def _index_outbound_script(self, keys: list[str], argv: list[str]) -> int:
        record_key = keys[0]
        if not self._exists(record_key):
            return -1
        n_k3 = int(argv[2])
        for key in keys[1 : 1 + n_k3]:
            self._own_value(record_key, key, argv[0], argv[1])
        for key in keys[1 + n_k3 :]:
            self._own_ref(record_key, key, argv[0], argv[1])
        return 1

    def _merge_script(self, keys: list[str], argv: list[str]) -> list:
        key_a, key_b, mark_a, mark_b, record_a, record_b = keys[:6]
        raw_a = self._strings.get(key_a)
        if raw_a is None:
            return ["unknown", key_a]
        a = json.loads(raw_a)
        if self._exists(mark_a):
            return ["erasing", a["person_id"]]
        if key_a == key_b:
            return ["ok", raw_a]
        raw_b = self._strings.get(key_b)
        if raw_b is None:
            return ["unknown", key_b]
        b = json.loads(raw_b)
        if self._exists(mark_b):
            return ["erasing", b["person_id"]]
        if a["target_kind"] != b["target_kind"] or a["target_name"] != b["target_name"]:
            return ["cross_target"]
        if a["created_at"] != b["created_at"]:
            survivor_is_a = a["created_at"] < b["created_at"]
        else:
            survivor_is_a = a["person_id"] < b["person_id"]
        if survivor_is_a:
            survivor, absorbed, survivor_key, absorbed_key = a, b, key_a, key_b
            survivor_record, absorbed_record = record_a, record_b
        else:
            survivor, absorbed, survivor_key, absorbed_key = b, a, key_b, key_a
            survivor_record, absorbed_record = record_b, record_a
        for addr in absorbed["addresses"]:
            for have in survivor["addresses"]:
                dup = have["door"] == addr["door"] and have["address"] == addr["address"]
                if dup and addr["door"] == "channel":
                    dup = have["channel"] == addr["channel"] and have["our_identity"] == addr["our_identity"]
                if dup:
                    return ["duplicate_address", addr["address"]]
            survivor["addresses"].append(addr)
        index_key = argv[0] + survivor["target_kind"] + ":" + survivor["target_name"]
        absorbed_id, survivor_id = absorbed["person_id"], survivor["person_id"]
        for field, held in list(self._hashes.get(index_key, {}).items()):
            if held == absorbed_id:
                self._hset(index_key, field, survivor_id)
        encoded = json.dumps(survivor)
        self._strings[survivor_key] = encoded
        for held_id in [*sorted(self._sets.get(absorbed_record, set())), absorbed_id]:
            self._sadd(survivor_record, held_id)
            self._strings[argv[1] + held_id] = survivor_id
        self._drop(absorbed_record)
        self._drop(absorbed_key)
        return ["ok", encoded]

    def _erase_script(self, keys: list[str], argv: list[str]) -> list:
        row_key, index_key, mark_key, absorbed_record, routes_key = keys[:5]
        person_id, routes_prefix, into_prefix = argv[0], argv[1], argv[2]
        writers = _Writers(self, argv[3])
        visited = set(json.loads(argv[4]))
        prefixes = (argv[5], argv[6], into_prefix)
        absorbed_ids = argv[7:]
        erased = {person_id, *absorbed_ids}
        for held_id in [person_id, *absorbed_ids]:
            unvisited = sorted(self._sets.get(writers.threads + held_id, set()) - visited)
            if unvisited:
                return ["unvisited", unvisited[0], held_id]
        handed: list[tuple[str, str]] = []
        for thread in json.loads(argv[4]):
            if self._kept(writers, thread, erased, prefixes):
                continue
            for record_key in writers.thread_records(thread):
                row = self._hashes.get(record_key, {})
                if row.get("writer") in erased:
                    door = row.get("writer_door")
                    if door is None:
                        raise RuntimeError(
                            f"conversations: a record of the route-keyed thread {thread} carries no writer_door: "
                            f"{record_key}"
                        )
                    handed.append((door, thread))
        for door, thread in handed:
            self._sadd(writers.threads + door, thread)
        for absorbed_id in absorbed_ids:
            self._drop(routes_prefix + absorbed_id)
            self._drop(into_prefix + absorbed_id)
        self._drop(absorbed_record)
        self._drop(routes_key)
        removed_fields = 0
        for field, held in list(self._hashes.get(index_key, {}).items()):
            if held == person_id:
                self._hdel(index_key, field)
                removed_fields += 1
        for held_id in [person_id, *absorbed_ids]:
            self._drop(writers.threads + held_id)
        self._drop(mark_key)
        removed_row = self._drop(row_key)
        return ["ok", removed_row, removed_fields]

    def _kept(self, writers: _Writers, thread: str, erased: set[str], prefixes: tuple[str, str, str]) -> str:
        """The scripts' outside-writer decision: the first writer of ``thread`` that keeps it standing, else ''."""
        person_prefix, erasing_prefix, into_prefix = prefixes

        def keeps(who: str, door: str | None) -> bool:
            if who in erased:
                return False
            if who == door:
                return True
            if self._exists(person_prefix + who):
                return not self._exists(erasing_prefix + who)
            survivor = self._strings.get(into_prefix + who)
            if survivor is None or survivor in erased:
                return False
            if not self._exists(person_prefix + survivor):
                raise RuntimeError(
                    f"conversations: the writer {who} points at the survivor {survivor}, whose row is gone"
                )
            return not self._exists(erasing_prefix + survivor)

        judged: dict[str, bool] = {}
        for record_key in writers.thread_records(thread):
            row = self._hashes.get(record_key)
            if row is None:
                continue
            who = row.get("writer")
            if who is None:
                raise RuntimeError(
                    f"conversations: a record of the route-keyed thread {thread} carries no writer: {record_key}"
                )
            if who not in judged:
                judged[who] = keeps(who, row.get("writer_door"))
                if judged[who]:
                    return who
        return ""

    def _outside_writer_script(self, _keys: list[str], argv: list[str]) -> str:
        return self._kept(_Writers(self, argv[1]), argv[0], set(argv[5:]), (argv[2], argv[3], argv[4]))

    def _read_for_erase_script(self, keys: list[str], argv: list[str]) -> list:
        row_key, absorbed_record, routes_key, pointer_key = keys[:4]
        routes_prefix, person_prefix, threads_prefix, person_id = argv[:4]
        pointer = self._strings.get(pointer_key) or ""
        survivor_stands = int(self._exists(person_prefix + pointer)) if pointer else 0
        threads = set(self._sets.get(threads_prefix + person_id, set()))
        absorbed = []
        for absorbed_id in sorted(self._sets.get(absorbed_record, set())):
            absorbed.append([absorbed_id, sorted(self._sets.get(routes_prefix + absorbed_id, set()))])
            threads |= self._sets.get(threads_prefix + absorbed_id, set())
        return [
            self._strings.get(row_key) or "",
            pointer,
            survivor_stands,
            sorted(self._sets.get(routes_key, set())),
            absorbed,
            list(threads),
        ]

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        keys = [str(k) for k in keys_and_args[:numkeys]]
        argv = [str(a) for a in keys_and_args[numkeys:]]
        marker_line = next(line for line in script.splitlines() if line.startswith("-- conversations:"))
        marker = marker_line.removeprefix("-- ")
        scripted = {
            "conversations:record:create": self._create_script,
            "conversations:record:delete": self._delete_script,
            "conversations:record:begin_send": self._begin_send_script,
            "conversations:record:complete_send": self._complete_send_script,
            "conversations:record:end_send": self._end_send_script,
            "conversations:record:ask_reply": self._ask_reply_script,
            "conversations:record:forget_sending": self._forget_sending_script,
            "conversations:record:index_outbound": self._index_outbound_script,
            "conversations:person:merge": self._merge_script,
            "conversations:person:erase": self._erase_script,
            "conversations:person:read_for_erase": self._read_for_erase_script,
            "conversations:thread:outside_writer": self._outside_writer_script,
        }
        if marker in scripted:
            return scripted[marker](keys, argv)
        if marker == "conversations:person:mark_erasing":
            if not self._exists(keys[0]):
                return 0
            self._strings[keys[1]] = "1"
            return 1
        if marker == "conversations:person:get_by_id":
            return [self._strings.get(keys[0]) or "", int(self._exists(keys[1]))]
        key = keys[0]
        indexes = keys[1:]
        # A script that touches the thread indexes carries them as the LAST two keys, after
        # the status layout the reindex step reads; the others never look at these. The
        # create carries the routing row one slot further out again.
        threaded, thread_keys = keys[1:-2], keys[-2:]
        if "conversations:dedupe:claim" in script:
            existing = self._strings.get(key)
            if existing is not None:
                return existing
            self._strings[key] = argv[0]
            return argv[0]
        if "conversations:person:lookup" in script:
            person_id = self._hashes.get(key, {}).get(argv[0])
            if person_id is None:
                return ["miss"]
            if self._exists(argv[2] + person_id):
                return ["erasing"]
            raw = self._strings.get(argv[1] + person_id)
            if raw is None:
                return ["torn", person_id]
            return ["hit", raw]
        if "conversations:person:ensure" in script:
            index_key = key
            dak, prefix, new_id, new_json = argv[0], argv[1], argv[2], argv[3]
            door, channel, our_identity, address = argv[4], argv[5], argv[6], argv[7]
            routes = json.loads(argv[8])
            writers = _Writers(self, argv[11])
            existing = self._hashes.get(index_key, {}).get(dak)
            if existing is None:
                self._hashes.setdefault(index_key, {})[dak] = new_id
                self._strings[prefix + new_id] = new_json
                writers.adopt(argv[10], new_id)
                return ["created", new_json]
            if self._exists(argv[9] + existing):
                return ["erasing", existing]
            row_key = prefix + existing
            raw = self._strings.get(row_key)
            if raw is None:
                return ["torn", existing]
            person = json.loads(raw)
            for addr in person["addresses"]:
                match = addr["door"] == door
                if match and door == "channel":
                    match = (
                        addr["channel"] == channel
                        and addr["our_identity"] == our_identity
                        and addr["address"] == address
                    )
                elif match:
                    match = addr["address"] == address
                if match:
                    changed = False
                    for want in routes:
                        if want not in addr["routes"]:
                            addr["routes"].append(want)
                            changed = True
                    if changed:
                        raw = json.dumps(person)
                        self._strings[row_key] = raw
                    break
            writers.adopt(argv[10], existing)
            return ["existing", raw]
        if "conversations:person:detach" in script:
            row_key, new_row_key = keys[0], keys[1]
            index_prefix, dak, new_id, new_created_at = argv[0], argv[1], argv[2], argv[3]
            door, channel, our_identity, address = argv[4], argv[5], argv[6], argv[7]
            raw = self._strings.get(row_key)
            if raw is None:
                return ["unknown", row_key]
            if self._exists(keys[2]):
                return ["erasing"]
            person = json.loads(raw)
            if len(person["addresses"]) <= 1:
                return ["not_linked"]
            found = None
            for i, addr in enumerate(person["addresses"]):
                match = addr["door"] == door
                if match and door == "channel":
                    match = (
                        addr["channel"] == channel
                        and addr["our_identity"] == our_identity
                        and addr["address"] == address
                    )
                elif match:
                    match = addr["address"] == address
                if match:
                    found = i
                    break
            if found is None:
                return ["not_member"]
            removed = person["addresses"].pop(found)
            self._strings[row_key] = json.dumps(person)
            fresh = {
                "person_id": new_id,
                "target_kind": person["target_kind"],
                "target_name": person["target_name"],
                "created_at": new_created_at,
                "addresses": [removed],
            }
            # Carry the stored locale onto the split-off person, matching the detach Lua.
            if person.get("locale") is not None:
                fresh["locale"] = person["locale"]
            fresh_json = json.dumps(fresh)
            self._strings[new_row_key] = fresh_json
            index_key = index_prefix + person["target_kind"] + ":" + person["target_name"]
            self._hashes.setdefault(index_key, {})[dak] = new_id
            return ["ok", fresh_json]
        if "conversations:person:set_locale" in script:
            row_key = keys[0]
            mode, locale = argv[0], argv[1]
            raw = self._strings.get(row_key)
            if raw is None:
                return ["missing"]
            if self._exists(keys[1]):
                return ["erasing"]
            person = json.loads(raw)
            if mode == "clear":
                person.pop("locale", None)
            else:
                person["locale"] = locale
            encoded = json.dumps(person)
            self._strings[row_key] = encoded
            return ["ok", encoded]
        if "conversations:pair_code:mint" in script:
            open_key, new_code_key = keys[0], keys[1]
            new_hash, record_json, ttl, code_prefix = argv[0], argv[1], int(argv[2]), argv[3]
            if new_code_key in self._strings:
                return 0
            open_hash = self._strings.get(open_key)
            if open_hash is not None:
                stale_key = code_prefix + open_hash
                self._strings.pop(stale_key, None)
                self.ttl_ms.pop(stale_key, None)
            self._strings[new_code_key] = record_json
            self.ttl_ms[new_code_key] = ttl * 1000
            self._strings[open_key] = new_hash
            self.ttl_ms[open_key] = ttl * 1000
            return 1
        if "conversations:thread_lease:refresh" in script:
            token, lease_ms = argv[0], int(argv[1])
            if self._strings.get(key) == token:
                self.ttl_ms[key] = lease_ms
                return 1
            return 0
        if "conversations:thread_lease:acquire" in script:
            token, lease_ms = argv[0], int(argv[1])
            if key in self._strings:
                return [0, self.ttl_ms.get(key, -1)]
            self._strings[key] = token
            self.ttl_ms[key] = lease_ms
            return [1, 0]
        if "conversations:thread_lease:release" in script:
            token, channel, thread_id = argv[0], argv[1], argv[2]
            if self._strings.get(key) == token:
                await self.delete(key)
                await self.publish(channel, thread_id)
                return 1
            return 0
        h = self._hashes.get(key)
        status = h.get("delivery_status") if h else None
        if "conversations:record:complete_turn" in script:
            if status is None:
                return -1
            if status != "accepted":
                return 0
            h.update(data=argv[0], delivery_status="pending_delivery", updated_at=argv[1], intake_claim="")  # type: ignore[union-attr]
            self._reindex(threaded, argv[2], argv[3])
            # Re-stamped only while the thread's own index still holds members, as the
            # script's guard does: a route delete reclaims both, and an unconditional
            # stamp would resurrect the route index behind it.
            if self._zsets.get(thread_keys[0]):
                self._zsets.setdefault(thread_keys[1], {})[argv[4]] = float(argv[1])
            return 1
        if "conversations:record:complete_silent" in script:
            if status is None:
                return -1
            if status != "accepted":
                return 0
            h.update(  # type: ignore[union-attr]
                data=argv[0], delivery_status="silent", updated_at=argv[1], intake_claim="", claim=""
            )
            self.ttl_ms[key] = int(argv[2])
            self._refresh_refs(key, argv[2])
            self._reindex(threaded, argv[3], argv[4])
            if self._zsets.get(thread_keys[0]):
                self._zsets.setdefault(thread_keys[1], {})[argv[5]] = float(argv[1])
            return 1
        if "conversations:record:merge" in script or "conversations:record:supersede" in script:
            # The channel-door overlap terminals share the ``complete_silent`` shape, differing
            # only in the status literal they stamp; both apply the retention TTL and re-stamp a
            # live thread. The record's content (ARGV[1]) already carries the successor_id.
            terminal = "merged" if "conversations:record:merge" in script else "superseded"
            if status is None:
                return -1
            if status != "accepted":
                return 0
            h.update(  # type: ignore[union-attr]
                data=argv[0], delivery_status=terminal, updated_at=argv[1], intake_claim="", claim=""
            )
            self.ttl_ms[key] = int(argv[2])
            self._refresh_refs(key, argv[2])
            self._reindex(threaded, argv[3], argv[4])
            if self._zsets.get(thread_keys[0]):
                self._zsets.setdefault(thread_keys[1], {})[argv[5]] = float(argv[1])
            return 1
        if "conversations:record:intake_claim" in script:
            now, lease, token = float(argv[0]), float(argv[1]), argv[2]
            if status is None:
                return -1
            if status != "accepted":
                return -2
            claim = h.get("intake_claim", "") if h else ""
            if claim:
                ctoken, cexp = claim.split(":", 1)
                if ctoken != token and float(cexp) > now:
                    return 0
            h["intake_claim"] = f"{token}:{now + lease}"  # type: ignore[index]
            return 1
        if "conversations:record:claim" in script:
            now, lease, token = float(argv[0]), float(argv[1]), argv[2]
            if status is None:
                return -1
            if status == "accepted":
                return -2
            if status != "pending_delivery":
                return 0
            claim = h.get("claim", "") if h else ""
            if claim:
                ctoken, cexp = claim.split(":", 1)
                if ctoken != token and float(cexp) > now:
                    return 0
            h["claim"] = f"{token}:{now + lease}"  # type: ignore[index]
            return 1
        if "conversations:record:provisional" in script:
            if status is None:
                return -1
            if status in ("delivered", "failed", "shed"):
                return 0
            if self._foreign_lease(h, now=argv[3], token=argv[4]):
                return -3
            parked = h.get("pending_receipt", "") if h else ""
            if parked:
                # A receipt staged during the send terminalises the record in this same step,
                # straight to the parked terminal, skipping ``provisional``.
                h.update(  # type: ignore[union-attr]
                    delivery_status=parked,
                    sends=argv[0],
                    attempts=argv[1],
                    updated_at=argv[3],
                    claim="",
                    grace_deadline="",
                    pending_receipt="",
                )
                self.ttl_ms[key] = int(argv[7])
                self._refresh_refs(key, argv[7])
                member = argv[5]
                for index_key in indexes[:-1]:
                    self._zsets.get(index_key, {}).pop(member, None)
                terminal_index = indexes[3] if parked == "delivered" else indexes[4]
                self._zsets.setdefault(terminal_index, {})[member] = float(argv[8])
                return 2 if parked == "delivered" else 3
            h.update(  # type: ignore[union-attr]
                delivery_status="provisional",
                sends=argv[0],
                attempts=argv[1],
                grace_deadline=argv[2],
                updated_at=argv[3],
                claim="",
            )
            self._reindex(indexes, argv[5], argv[6])
            return 1
        if "conversations:record:delivered" in script:
            if status is None:
                return -1
            if status in ("failed", "shed"):
                return -2
            if status == "delivered":
                return 0
            if self._foreign_lease(h, now=argv[2], token=argv[4]):
                return -3
            h.update(  # type: ignore[union-attr]
                delivery_status="delivered",
                sends=argv[0],
                attempts=argv[1],
                updated_at=argv[2],
                claim="",
                grace_deadline="",
            )
            self.ttl_ms[key] = int(argv[3])
            self._refresh_refs(key, argv[3])
            self._reindex(indexes, argv[5], argv[6])
            return 1
        if "conversations:record:failed" in script:
            if status is None:
                return -1
            if status in ("delivered", "shed", "provisional"):
                return -2
            if status == "failed":
                return 0
            if self._foreign_lease(h, now=argv[1], token=argv[3]):
                return -3
            parked = h.get("pending_receipt", "") if h else ""
            h.update(  # type: ignore[union-attr]
                delivery_status="failed",
                attempts=argv[0],
                sends=argv[6],
                updated_at=argv[1],
                claim="",
                grace_deadline="",
                pending_receipt="",
            )
            self.ttl_ms[key] = int(argv[2])
            self._refresh_refs(key, argv[2])
            self._reindex(indexes, argv[4], argv[5])
            return 2 if parked else 1
        if "conversations:record:unreadable" in script:
            # The sweep-owned terminal write: no lease token is threaded, a corrupt row's status
            # is read opaquely, and a provisional row (which the failed script refuses) is owned.
            if h is None:
                return -1
            if status == "failed":
                return 0
            if status in ("delivered", "shed"):
                return -2
            if self._foreign_lease(h, now=argv[0], token=""):
                return -3
            parked = h.get("pending_receipt", "") if h else ""
            h.update(delivery_status="failed", claim="", grace_deadline="", updated_at=argv[0], pending_receipt="")
            self.ttl_ms[key] = int(argv[1])
            self._refresh_refs(key, argv[1])
            self._reindex(indexes, argv[2], argv[3])
            return 2 if parked else 1
        if "conversations:record:receipt" in script:
            target = argv[0]
            if status is None:
                return -1
            if status == target:
                return 0
            if status in ("delivered", "failed", "shed"):
                return -2
            if status == "pending_delivery":
                # The send is still in flight: stage the receipt on the record for the completing
                # provisional write to apply; first parked wins.
                parked = h.get("pending_receipt", "") if h else ""
                if parked == target:
                    return 0
                if parked:
                    return -2
                h["pending_receipt"] = target  # type: ignore[index]
                return 2
            if status != "provisional":
                return -3
            h.update(delivery_status=target, updated_at=argv[1], claim="", grace_deadline="")  # type: ignore[union-attr]
            self.ttl_ms[key] = int(argv[2])
            self._refresh_refs(key, argv[2])
            self._reindex(indexes, argv[3], argv[4])
            return 1
        if "conversations:record:unindex" in script:
            return sum(1 for index_key in indexes if self._zsets.get(index_key, {}).pop(argv[0], None) is not None)
        if "conversations:thread:prune" in script:
            # KEYS[1]=thread index, KEYS[2]=route thread index, KEYS[3..]=candidate record
            # keys; ARGV[1]=thread_id, ARGV[2..]=the candidates, parallel to KEYS[3..]. The
            # whole step mutates the maps directly, as the sibling branches do: server-side
            # is where a script's writes happen, never back through the client commands.
            # Called with NO candidate too, which is how a thread whose index already ran
            # empty leaves the route index.
            thread_index = self._zsets.get(keys[0], {})
            removed = 0
            for record_key, message_id in zip(keys[2:], argv[1:], strict=True):
                if record_key not in self._hashes and thread_index.pop(message_id, None) is not None:
                    removed += 1
            remaining = len(thread_index)
            if remaining == 0:
                # Redis drops a sorted set that holds nothing, so neither does the fake.
                self._zsets.pop(keys[0], None)
                self._zsets.get(keys[1], {}).pop(argv[0], None)
            return [removed, remaining]
        raise NotImplementedError(f"FakeRecordRedis.eval: unknown script {script[:60]!r}")


def make_record_client_ctx(fake: FakeRecordRedis):
    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake

    return _ctx
