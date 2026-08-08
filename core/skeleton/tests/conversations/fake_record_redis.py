"""An in-memory fake of the redis surface the answer/record store uses.

Covers the HASH + STRING commands the record store calls, the LIST commands the send
ledger calls, the ZSET commands the status and thread record indexes use, plus the record
Lua scripts (dispatched by marker comment, re-implemented here). Single-threaded async, so
each faked ``eval`` runs atomically.

``ttl_ms`` records the expiry each command applied, so a test can assert the retention
TTL lands only on a terminal record.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from tai42_skeleton.conversations.settings import ConversationsSettings


class FakeRecordRedis:
    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._lists: dict[str, list[str]] = {}
        self._zsets: dict[str, dict[str, float]] = {}
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

    def drop_route(self, route_name: str) -> None:
        """Remove the routing row, so a create lands as one racing the route's delete."""
        self._strings.pop(ConversationsSettings().route_key(route_name), None)

    # -- sorted-set commands (the status / thread record indexes) ------------
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

    async def zrangebyscore(
        self,
        key: str,
        minimum: str | float,
        maximum: str | float,
        start: int | None = None,
        num: int | None = None,
    ) -> list[str]:
        low, high = float(minimum), float(maximum)
        matched = [m for m in self._ordered(key) if low <= self._zsets[key][m] <= high]
        if start is None and num is None:
            return matched
        offset = start or 0
        return matched[offset : offset + num] if num is not None else matched[offset:]

    async def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, {}))

    async def exists(self, key: str) -> int:
        return 1 if any(key in store for store in (self._strings, self._hashes, self._lists, self._zsets)) else 0

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
        self.ttl_ms[key] = seconds * 1000
        return True

    async def delete(self, key: str) -> int:
        self.ttl_ms.pop(key, None)
        held = [store.pop(key, None) for store in (self._strings, self._hashes, self._lists, self._zsets)]
        return 1 if any(value is not None for value in held) else 0

    # -- string commands -----------------------------------------------------
    async def get(self, key: str) -> str | None:
        # Captures the value THEN yields (as a real single-server redis does: each command runs
        # to completion server-side, so two concurrent readers both observe pre-write state).
        # This makes a non-atomic check-then-act genuinely interleave — the read ops
        # (``get``/``hget``) yield, while the atomic burn ``getdel`` never does, so the
        # single-use pair-code redeem stays exclusive.
        value = self._strings.get(key)
        await asyncio.sleep(0)
        return value

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool | None:
        if nx and key in self._strings:
            return None
        self._strings[key] = value
        if ex is not None:
            self.ttl_ms[key] = ex * 1000
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

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        keys = [str(k) for k in keys_and_args[:numkeys]]
        key = keys[0]
        indexes = keys[1:]
        # A script that touches the thread indexes carries them as the LAST two keys, after
        # the status layout the reindex step reads; the others never look at these. The
        # create carries the routing row one slot further out again.
        threaded, thread_keys = keys[1:-2], keys[-2:]
        argv = [str(a) for a in keys_and_args[numkeys:]]
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
            raw = self._strings.get(argv[1] + person_id)
            if raw is None:
                return ["torn", person_id]
            return ["hit", raw]
        if "conversations:person:ensure" in script:
            index_key = key
            dak, prefix, new_id, new_json = argv[0], argv[1], argv[2], argv[3]
            door, channel, our_identity, address = argv[4], argv[5], argv[6], argv[7]
            routes = json.loads(argv[8])
            existing = self._hashes.get(index_key, {}).get(dak)
            if existing is None:
                self._hashes.setdefault(index_key, {})[dak] = new_id
                self._strings[prefix + new_id] = new_json
                return ["created", new_json]
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
            return ["existing", raw]
        if "conversations:person:merge" in script:
            key_a, key_b = keys[0], keys[1]
            raw_a = self._strings.get(key_a)
            if raw_a is None:
                return ["unknown", key_a]
            if key_a == key_b:
                return ["ok", raw_a]
            raw_b = self._strings.get(key_b)
            if raw_b is None:
                return ["unknown", key_b]
            a, b = json.loads(raw_a), json.loads(raw_b)
            if a["target_kind"] != b["target_kind"] or a["target_name"] != b["target_name"]:
                return ["cross_target"]
            if a["created_at"] < b["created_at"]:
                survivor_is_a = True
            elif b["created_at"] < a["created_at"]:
                survivor_is_a = False
            else:
                survivor_is_a = a["person_id"] < b["person_id"]
            survivor, absorbed, survivor_key, absorbed_key = (
                (a, b, key_a, key_b) if survivor_is_a else (b, a, key_b, key_a)
            )
            for addr in absorbed["addresses"]:
                for have in survivor["addresses"]:
                    dup = have["door"] == addr["door"] and have["address"] == addr["address"]
                    if dup and addr["door"] == "channel":
                        dup = have["channel"] == addr["channel"] and have["our_identity"] == addr["our_identity"]
                    if dup:
                        return ["duplicate_address", addr["address"]]
                survivor["addresses"].append(addr)
            index_key = argv[0] + survivor["target_kind"] + ":" + survivor["target_name"]
            index = self._hashes.get(index_key, {})
            for field, val in list(index.items()):
                if val == absorbed["person_id"]:
                    index[field] = survivor["person_id"]
            encoded = json.dumps(survivor)
            self._strings[survivor_key] = encoded
            self._strings.pop(absorbed_key, None)
            return ["ok", encoded]
        if "conversations:person:detach" in script:
            row_key, new_row_key = keys[0], keys[1]
            index_prefix, dak, new_id, new_created_at = argv[0], argv[1], argv[2], argv[3]
            door, channel, our_identity, address = argv[4], argv[5], argv[6], argv[7]
            raw = self._strings.get(row_key)
            if raw is None:
                return ["unknown", row_key]
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
            fresh_json = json.dumps(fresh)
            self._strings[new_row_key] = fresh_json
            index_key = index_prefix + person["target_kind"] + ":" + person["target_name"]
            self._hashes.setdefault(index_key, {})[dak] = new_id
            return ["ok", fresh_json]
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
        h = self._hashes.get(key)
        status = h.get("delivery_status") if h else None
        if "conversations:record:create" in script:
            # The create carries the ROUTING ROW behind its two thread index keys, so the
            # slicing the sibling scripts share shifts by one here.
            created_threaded, created_thread_keys, route_row_key = keys[1:-3], keys[-3:-1], keys[-1]
            self._hashes[key] = {
                "data": argv[0],
                "delivery_status": argv[1],
                "outbound_ids": argv[2],
                "attempts": argv[3],
                "claim": "",
                "grace_deadline": "",
                "updated_at": argv[4],
                "intake_claim": argv[6],
            }
            if argv[5]:
                self.ttl_ms[key] = int(argv[5])
            self._reindex(created_threaded, argv[7], argv[8])
            # Written only while the route still routes, as the script's guard does: the
            # indexes of a route whose row is gone are reclaimed already and nothing walks
            # the name again, so re-creating them would strand the pair.
            if await self.exists(route_row_key) == 0:
                return 0
            self._zsets.setdefault(created_thread_keys[0], {})[argv[7]] = float(argv[10])
            self._zsets.setdefault(created_thread_keys[1], {})[argv[9]] = float(argv[10])
            return 1
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
            h.update(  # type: ignore[union-attr]
                delivery_status="provisional",
                outbound_ids=argv[0],
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
                outbound_ids=argv[0],
                attempts=argv[1],
                updated_at=argv[2],
                claim="",
                grace_deadline="",
            )
            self.ttl_ms[key] = int(argv[3])
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
            h.update(delivery_status="failed", attempts=argv[0], updated_at=argv[1], claim="", grace_deadline="")  # type: ignore[union-attr]
            self.ttl_ms[key] = int(argv[2])
            self._reindex(indexes, argv[4], argv[5])
            return 1
        if "conversations:record:receipt" in script:
            target = argv[0]
            if status is None:
                return -1
            if status == target:
                return 0
            if status in ("delivered", "failed", "shed"):
                return -2
            if status != "provisional":
                return -3
            h.update(delivery_status=target, updated_at=argv[1], claim="", grace_deadline="")  # type: ignore[union-attr]
            self.ttl_ms[key] = int(argv[2])
            self._reindex(indexes, argv[3], argv[4])
            return 1
        if "conversations:record:delete" in script:
            removed = await self.delete(key)
            for index_key in (*threaded, thread_keys[0]):
                self._zsets.get(index_key, {}).pop(argv[0], None)
            if not self._zsets.get(thread_keys[0]):
                self._zsets.get(thread_keys[1], {}).pop(argv[1], None)
            return removed
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
