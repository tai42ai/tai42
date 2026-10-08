"""The durable park index a driver keeps for the runs it parks on an async ``ask``.

When a driver parks a run, the platform keeps only the interaction id; the driver reverses it
to its own run through this index. One consumer namespace holds, on one Redis:

* an ENTRY per resume key (an interaction id, or any key the consumer resumes by), carrying the
  consumer's own opaque rebuild payload;
* a BARRIER per parked super-step (a thread's set of asks resolved together): the consumer's
  opaque ``expected`` map, keyed by member id, the answers buffered so far, and any opaque
  consumer fields;
* a DRIVE LEASE per super-step, so exactly one worker drives a completed barrier, with a
  heartbeat and a token-checked release;
* a RESOLUTION RECORD per resolved super-step and a TOMBSTONE in place of every member entry,
  so a lapped redelivery replays what the first drive returned;
* a RUN-RESOLUTION INDEX per thread (every resolved super-step and its members), so a kill can
  reach every stored outcome of a run;
* a LIVE SET per thread (its super-steps whose barrier is live), so a checkpoint sweep can tell
  a parked thread from an abandoned one.

The module imports no Redis client at import time: the client is reached lazily, so a consumer
whose import graph excludes Redis may import this module. Every Redis error propagates.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from tai42_contract.app import tai42_app

from tai42_kit.interactions._park_types import (
    Barrier,
    BarrierNotFoundError,
    BufferResult,
    DriveInProgressError,
    LeaseLostError,
    ParkIndexCorruptError,
    ParkIndexError,
    ParkValueCodecError,
    Resolution,
    ResolutionMissingError,
    ResolutionRecord,
    SuperstepAlreadyResolvedError,
    decode_value,
    encode_value,
    validated_resolution,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis

    from tai42_kit.clients.settings import RedisConnectionSettings

logger = logging.getLogger(__name__)

__all__ = [
    "BARRIER_TTL_MARGIN_SECONDS",
    "DRIVE_LEASE_HEARTBEAT_SECONDS",
    "DRIVE_LEASE_SECONDS",
    "ENTRY_TTL_FLOOR_SECONDS",
    "RESOLVED_FIELD",
    "Barrier",
    "BarrierNotFoundError",
    "BufferResult",
    "DriveInProgressError",
    "DriveLease",
    "LeaseLostError",
    "ParkIndex",
    "ParkIndexCorruptError",
    "ParkIndexError",
    "ParkValueCodecError",
    "Resolution",
    "ResolutionMissingError",
    "ResolutionRecord",
    "SuperstepAlreadyResolvedError",
    "TombstoneKind",
    "barrier_ttl_seconds",
    "decode_value",
    "encode_value",
    "entry_ttl_seconds",
    "resolution_ttl_seconds",
    "superstep_id",
]

# The floor TTL of every entry: a never-resolved entry whose deadline is short or absent still
# lives this long, and a truly abandoned one still drops out instead of leaking forever.
ENTRY_TTL_FLOOR_SECONDS: Final[int] = 30 * 24 * 3600

# Headroom over an ask deadline when sizing an entry or a barrier, so the expiry continuation
# that fires AT the deadline still finds them.
BARRIER_TTL_MARGIN_SECONDS: Final[int] = 3600

# The drive lease's TTL and its heartbeat cadence (three beats per TTL): a crashed holder's lease
# expires within a minute, so a redelivery reclaims and re-drives.
DRIVE_LEASE_SECONDS: Final[int] = 60
DRIVE_LEASE_HEARTBEAT_SECONDS: Final[float] = 20.0

# The marker field of a tombstone written in place of an entry. A resolved tombstone also carries
# the coordinates of its resolution record; a detached tombstone carries none.
RESOLVED_FIELD: Final[str] = "__park_resolved__"
_TOMBSTONE_THREAD_FIELD: Final[str] = "thread_id"
_TOMBSTONE_SUPERSTEP_FIELD: Final[str] = "superstep"

# Every live entry the index writes carries its ``[thread_id, superstep]`` under this field, so the
# index locates an entry's barrier from the entry alone; the rest of the entry is the consumer's.
_ENTRY_COORDINATES_FIELD: Final[str] = "__park_step__"

_EXPECTED_FIELD: Final[str] = "expected"
_OUTPUT_PREFIX: Final[str] = "output:"

_NAMESPACE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_-]*(:[a-z][a-z0-9_-]*)*$")


TombstoneKind = Literal["live", "resolved", "detached"]

# Compare-and-expire the lease: renews only while the stored token is still this holder's.
_RENEW_SCRIPT: Final[str] = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""

# Compare-and-delete the lease: a stale holder can never drop a reclaimer's lease.
_RELEASE_SCRIPT: Final[str] = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

# Finalize a super-step in one step, guarded by the caller's lease token. KEYS: lease, barrier,
# resolution record, run-resolution index, live set, then every member entry. ARGV: token, TTL,
# tombstone, record, super-step id, member ids (JSON). Returns 0 when the token no longer holds
# the lease, 1 on the write.
_FINALIZE_SCRIPT: Final[str] = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then
    return 0
end
local ttl = tonumber(ARGV[2])
for i = 6, #KEYS do
    redis.call('set', KEYS[i], ARGV[3], 'EX', ttl)
end
redis.call('set', KEYS[3], ARGV[4], 'EX', ttl)
redis.call('hset', KEYS[4], ARGV[5], ARGV[6])
redis.call('expire', KEYS[4], ttl)
redis.call('srem', KEYS[5], ARGV[5])
redis.call('del', KEYS[2])
redis.call('del', KEYS[1])
return 1
"""

# Write fields onto a barrier only while it still exists (never resurrects an expired or
# finalized one). ARGV: field, value pairs.
_SET_BARRIER_FIELDS_SCRIPT: Final[str] = """
if redis.call('hexists', KEYS[1], 'expected') == 0 then
    return 0
end
redis.call('hset', KEYS[1], unpack(ARGV))
return 1
"""

# Grow an entry's, its barrier's and its thread's live set's TTLs, never shrinking one, while the
# entry still holds exactly the value the caller read. KEYS: entry, barrier, live set. ARGV: the
# entry value read, entry TTL, barrier TTL. A key with no TTL is already unbounded and is left
# alone; a missing key has nothing to grow. Returns -1 when the entry changed since the read.
_EXTEND_HORIZON_SCRIPT: Final[str] = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then
    return -1
end
local wanted = {tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[3])}
for i = 1, 3 do
    local current = redis.call('ttl', KEYS[i])
    if current >= 0 and current < wanted[i] then
        redis.call('expire', KEYS[i], wanted[i])
    end
end
return 1
"""


def superstep_id(member_ids: Iterable[str]) -> str:
    """A deterministic super-step id: a sha256 over the sorted member ids, insensitive to their order."""
    return hashlib.sha256(",".join(sorted(member_ids)).encode("utf-8")).hexdigest()


def entry_ttl_seconds(expiry_at: datetime | None) -> int:
    """An entry's TTL: the floor, extended to outlast its ask's deadline plus the margin."""
    if expiry_at is None:
        return ENTRY_TTL_FLOOR_SECONDS
    horizon = int((expiry_at - datetime.now(UTC)).total_seconds()) + BARRIER_TTL_MARGIN_SECONDS
    return max(ENTRY_TTL_FLOOR_SECONDS, horizon)


def barrier_ttl_seconds(expiries: Iterable[datetime | None]) -> int:
    """A barrier's TTL: the entry floor, extended to outlast the LATEST deadline it coordinates plus the margin."""
    deadlines = [expiry for expiry in expiries if expiry is not None]
    if not deadlines:
        return ENTRY_TTL_FLOOR_SECONDS
    return entry_ttl_seconds(max(deadlines))


def resolution_ttl_seconds() -> int:
    """The TTL of a resolution record and its tombstones: twice the platform's redelivery horizon.

    They must outlive the last redelivery the reaper can fire, plus reaper-cycle and clock-skew
    margin, and no longer (they hold a run's outcome).
    """
    return 2 * tai42_app.interactions.redelivery_horizon_seconds()


class ParkIndex:
    """One consumer's park index under its own key namespace on its own Redis."""

    def __init__(
        self,
        namespace: str,
        redis: RedisConnectionSettings,
        *,
        client: Callable[[], AbstractAsyncContextManager[AsyncRedis]] | None = None,
    ) -> None:
        """Bind the index to ``namespace`` on the Redis ``redis`` names.

        Args:
            namespace: The consumer's key namespace, e.g. ``"probe:park"``; lower-case segments
                joined by ``:``.
            redis: The consumer's Redis connection settings.
            client: A factory of a connected async client; defaults to the kit's pooled client for
                ``redis``.

        Raises:
            ValueError: ``namespace`` is not a valid key namespace.
        """
        if not _NAMESPACE_RE.fullmatch(namespace):
            raise ValueError(f"invalid park index namespace {namespace!r}")
        self.namespace = namespace
        self._redis = redis
        self._client_factory = client

    @contextlib.asynccontextmanager
    async def _client(self) -> AsyncIterator[AsyncRedis]:
        if self._client_factory is not None:
            async with self._client_factory() as client:
                yield client
            return
        from tai42_kit.clients import client_ctx
        from tai42_kit.clients.impl.redis import RedisClient

        async with client_ctx(RedisClient, self._redis) as client:
            yield client

    def entry_key(self, entry_id: str) -> str:
        """The key of the entry stored for ``entry_id``."""
        return f"{self.namespace}:{entry_id}"

    def barrier_key(self, thread_id: str, superstep: str) -> str:
        """The key of a super-step's barrier hash."""
        return f"{self.namespace}:step:{thread_id}:{superstep}"

    def lease_key(self, thread_id: str, superstep: str) -> str:
        """The key of a super-step's drive lease."""
        return f"{self.barrier_key(thread_id, superstep)}:claim"

    def resolution_key(self, thread_id: str, superstep: str) -> str:
        """The key of a resolved super-step's resolution record."""
        return f"{self.barrier_key(thread_id, superstep)}:resolution"

    def run_resolutions_key(self, thread_id: str) -> str:
        """The key of a thread's run-resolution index."""
        return f"{self.namespace}:run-resolutions:{thread_id}"

    def live_set_key(self, thread_id: str) -> str:
        """The key of a thread's set of super-steps whose barrier is live."""
        return f"{self.namespace}:live:{thread_id}"

    async def persist(
        self,
        *,
        thread_id: str,
        superstep: str,
        entries: Mapping[str, Mapping[str, Any]],
        expected: Mapping[str, Any],
        barrier_fields: Mapping[str, str] | None = None,
        entry_ttl: Mapping[str, int],
        barrier_ttl: int,
    ) -> None:
        """Write a parked super-step's entries, its barrier and its live-set membership, all or nothing.

        Each entry is written whole under its own TTL; the barrier holds ``expected`` (keyed by
        member id) and ``barrier_fields``; the thread's live set lasts as long as its longest-lived
        barrier.
        """
        live = self.live_set_key(thread_id)
        barrier = self.barrier_key(thread_id, superstep)
        async with self._client() as client, client.pipeline(transaction=True) as pipe:
            for entry_id, entry in entries.items():
                stored = {**dict(entry), _ENTRY_COORDINATES_FIELD: [thread_id, superstep]}
                pipe.set(self.entry_key(entry_id), json.dumps(stored), ex=entry_ttl[entry_id])
            pipe.hset(barrier, mapping={_EXPECTED_FIELD: json.dumps(dict(expected)), **dict(barrier_fields or {})})
            pipe.expire(barrier, barrier_ttl)
            pipe.sadd(live, superstep)
            # NX sets a TTL on a new set; GT only ever extends one (GT alone never sets a TTL).
            pipe.expire(live, barrier_ttl, nx=True)
            pipe.expire(live, barrier_ttl, gt=True)
            await pipe.execute()

    async def threads_with_live_barriers(self, thread_ids: Sequence[str]) -> set[str]:
        """The subset of ``thread_ids`` with at least one live barrier."""
        if not thread_ids:
            return set()
        async with self._client() as client:
            async with client.pipeline(transaction=False) as pipe:
                for thread_id in thread_ids:
                    pipe.smembers(self.live_set_key(thread_id))
                members: list[set[str]] = await pipe.execute()
            probes = [
                (thread_id, superstep)
                for thread_id, supersteps in zip(thread_ids, members, strict=True)
                for superstep in sorted(supersteps)
            ]
            if not probes:
                return set()
            async with client.pipeline(transaction=False) as pipe:
                for thread_id, superstep in probes:
                    pipe.exists(self.barrier_key(thread_id, superstep))
                exists: list[int] = await pipe.execute()
        return {thread_id for (thread_id, _superstep), found in zip(probes, exists, strict=True) if found}

    async def read_entry(self, entry_id: str) -> dict[str, Any] | None:
        """The entry or tombstone stored for ``entry_id``, or ``None`` when there is none."""
        async with self._client() as client:
            raw = await client.get(self.entry_key(entry_id))
        if raw is None:
            return None
        entry = json.loads(raw)
        if not isinstance(entry, dict):
            raise ParkIndexCorruptError(f"park entry {self.entry_key(entry_id)!r} is not a JSON object")
        return entry

    @staticmethod
    def tombstone_kind(entry: Mapping[str, Any]) -> TombstoneKind:
        """Whether a read entry is a live entry, a resolved tombstone, or a detached tombstone."""
        if entry.get(RESOLVED_FIELD) is not True:
            return "live"
        if entry.get(_TOMBSTONE_THREAD_FIELD) is None or entry.get(_TOMBSTONE_SUPERSTEP_FIELD) is None:
            return "detached"
        return "resolved"

    @staticmethod
    def tombstone_coordinates(entry: Mapping[str, Any]) -> tuple[str, str]:
        """The ``(thread_id, superstep)`` a resolved tombstone points at.

        Raises:
            ParkIndexCorruptError: ``entry`` is not a resolved tombstone.
        """
        if ParkIndex.tombstone_kind(entry) != "resolved":
            raise ParkIndexCorruptError("only a resolved tombstone carries resolution coordinates")
        return str(entry[_TOMBSTONE_THREAD_FIELD]), str(entry[_TOMBSTONE_SUPERSTEP_FIELD])

    async def read_tombstone_resolution(self, entry: Mapping[str, Any]) -> ResolutionRecord | None:
        """The record a tombstone resolves to: the stored record of a resolved one, ``None`` for a detached one.

        Raises:
            ResolutionMissingError: A resolved tombstone's record is gone.
            ParkIndexCorruptError: ``entry`` is a live entry.
        """
        kind = self.tombstone_kind(entry)
        if kind == "detached":
            return None
        thread_id, superstep = self.tombstone_coordinates(entry)
        record = await self.read_resolution(thread_id, superstep)
        if record is None:
            raise ResolutionMissingError(thread_id, superstep)
        return record

    async def read_barrier(self, thread_id: str, superstep: str) -> Barrier | None:
        """A super-step's barrier, or ``None`` when it is gone; buffered answers are decoded."""
        from tai42_kit.clients.impl.redis import hgetall

        key = self.barrier_key(thread_id, superstep)
        async with self._client() as client:
            raw = await hgetall(client, key)
        if not raw:
            return None
        if _EXPECTED_FIELD not in raw:
            raise ParkIndexCorruptError(f"park barrier {key!r} has no expected map")
        outputs = {
            field[len(_OUTPUT_PREFIX) :]: decode_value(json.loads(value))
            for field, value in raw.items()
            if field.startswith(_OUTPUT_PREFIX)
        }
        fields = {
            field: value
            for field, value in raw.items()
            if field != _EXPECTED_FIELD and not field.startswith(_OUTPUT_PREFIX)
        }
        return Barrier(expected=json.loads(raw[_EXPECTED_FIELD]), outputs=outputs, fields=fields)

    async def set_barrier_fields(self, thread_id: str, superstep: str, fields: Mapping[str, str]) -> bool:
        """Write consumer fields onto a live barrier; ``False`` (nothing written) when the barrier is gone."""
        if not fields:
            raise ValueError("set_barrier_fields needs at least one field")
        reserved = [name for name in fields if name == _EXPECTED_FIELD or name.startswith(_OUTPUT_PREFIX)]
        if reserved:
            raise ValueError(f"barrier fields {reserved} are owned by the park index")
        args = [part for pair in fields.items() for part in pair]
        async with self._client() as client:
            written = await cast(
                "Awaitable[int]",
                client.eval(_SET_BARRIER_FIELDS_SCRIPT, 1, self.barrier_key(thread_id, superstep), *args),
            )
        return bool(written)

    async def buffer(self, thread_id: str, superstep: str, member_id: str, answer: Any) -> BufferResult:
        """Buffer one member's answer into its barrier, idempotently, and report progress.

        A redelivered answer for a member already answered keeps the first one.

        Raises:
            BarrierNotFoundError: The barrier is gone.
            KeyError: ``member_id`` is not a member the barrier expects.
            SuperstepAlreadyResolvedError: The barrier was finalized during the write; the
                resurrected key is deleted.
        """
        key = self.barrier_key(thread_id, superstep)
        encoded = json.dumps(encode_value(answer))
        async with self._client() as client:
            expected_raw = await cast("Awaitable[str | None]", client.hget(key, _EXPECTED_FIELD))
            if expected_raw is None:
                raise BarrierNotFoundError(thread_id, superstep)
            expected: dict[str, Any] = json.loads(expected_raw)
            if member_id not in expected:
                raise KeyError(member_id)
            await cast("Awaitable[int]", client.hsetnx(key, f"{_OUTPUT_PREFIX}{member_id}", encoded))
            if not await cast("Awaitable[bool]", client.hexists(key, _EXPECTED_FIELD)):
                await client.delete(key)
                raise SuperstepAlreadyResolvedError(thread_id, superstep)
            values = await cast(
                "Awaitable[list[str | None]]", client.hmget(key, [f"{_OUTPUT_PREFIX}{m}" for m in expected])
            )
        remaining = [member for member, value in zip(expected, values, strict=True) if value is None]
        return BufferResult(present=len(expected) - len(remaining), total=len(expected), remaining=remaining)

    def claim(self, thread_id: str, superstep: str) -> DriveLease:
        """A drive lease on a super-step, won by entering it (``async with`` or :meth:`DriveLease.acquire`)."""
        return DriveLease(self, thread_id, superstep)

    async def finalize(
        self,
        lease: DriveLease,
        *,
        member_ids: Sequence[str],
        resolution: Resolution,
        value: Any,
    ) -> None:
        """Resolve a super-step under ``lease``: tombstone its members, store its record, drop its barrier and lease.

        One guarded step: it lands only while ``lease`` still holds the super-step, and it also
        records the super-step in its thread's run-resolution index and leaves the thread's live
        set. On success the lease is consumed and its heartbeat stopped.

        Raises:
            LeaseLostError: ``lease`` no longer holds the super-step (or never did).
            ParkIndexCorruptError: ``resolution`` is not a resolution word.
            ParkValueCodecError: ``value`` cannot be stored.
        """
        validated_resolution(resolution)
        if lease.index is not self:
            raise ValueError("finalize takes a lease claimed on this park index")
        if not lease.acquired or lease.consumed or lease.lost:
            raise LeaseLostError(lease.thread_id, lease.superstep)
        thread_id, superstep = lease.thread_id, lease.superstep
        members = list(member_ids)
        tombstone = json.dumps(
            {RESOLVED_FIELD: True, _TOMBSTONE_THREAD_FIELD: thread_id, _TOMBSTONE_SUPERSTEP_FIELD: superstep}
        )
        record = json.dumps({"resolution": resolution, "value": encode_value(value)})
        async with self._client() as client:
            landed = await cast(
                "Awaitable[int]",
                client.eval(
                    _FINALIZE_SCRIPT,
                    5 + len(members),
                    self.lease_key(thread_id, superstep),
                    self.barrier_key(thread_id, superstep),
                    self.resolution_key(thread_id, superstep),
                    self.run_resolutions_key(thread_id),
                    self.live_set_key(thread_id),
                    *(self.entry_key(member) for member in members),
                    lease.token,
                    str(resolution_ttl_seconds()),
                    tombstone,
                    record,
                    superstep,
                    json.dumps(members),
                ),
            )
        if not landed:
            raise LeaseLostError(thread_id, superstep)
        await lease.mark_consumed()

    async def read_resolution(self, thread_id: str, superstep: str) -> ResolutionRecord | None:
        """A resolved super-step's record, or ``None`` when it has none."""
        key = self.resolution_key(thread_id, superstep)
        async with self._client() as client:
            raw = await client.get(key)
        if raw is None:
            return None
        record = json.loads(raw)
        if not isinstance(record, dict) or "value" not in record:
            raise ParkIndexCorruptError(f"resolution record {key!r} is malformed")
        return ResolutionRecord(
            resolution=validated_resolution(record.get("resolution")), value=decode_value(record["value"])
        )

    async def run_resolutions(self, thread_id: str) -> dict[str, list[str]]:
        """Every resolved super-step of a thread with its members, ``{}`` when none."""
        from tai42_kit.clients.impl.redis import hgetall

        async with self._client() as client:
            raw = await hgetall(client, self.run_resolutions_key(thread_id))
        return {superstep: json.loads(members) for superstep, members in raw.items()}

    async def drop_run_resolutions(self, thread_id: str, *, keep: str | None = None) -> None:
        """Drop every resolved super-step of a thread but ``keep`` (member tombstones and record), then its index."""
        resolved = await self.run_resolutions(thread_id)
        async with self._client() as client, client.pipeline(transaction=True) as pipe:
            for superstep, members in resolved.items():
                if superstep == keep:
                    continue
                for member in members:
                    pipe.delete(self.entry_key(member))
                pipe.delete(self.resolution_key(thread_id, superstep))
            pipe.delete(self.run_resolutions_key(thread_id))
            await pipe.execute()

    async def drop_resolution(self, thread_id: str, superstep: str, *, member_ids: Iterable[str] = ()) -> None:
        """Drop one resolved super-step: its record, its members' tombstones (and ``member_ids``), its index member."""
        members = set((await self.run_resolutions(thread_id)).get(superstep, [])) | set(member_ids)
        async with self._client() as client, client.pipeline(transaction=True) as pipe:
            for member in sorted(members):
                pipe.delete(self.entry_key(member))
            pipe.delete(self.resolution_key(thread_id, superstep))
            pipe.hdel(self.run_resolutions_key(thread_id), superstep)
            await pipe.execute()

    async def detach(self, entry_ids: Sequence[str]) -> None:
        """Tombstone keys that were claimed but never parked on, never overwriting an existing key."""
        if not entry_ids:
            return
        ttl = resolution_ttl_seconds()
        tombstone = json.dumps({RESOLVED_FIELD: True})
        async with self._client() as client, client.pipeline(transaction=True) as pipe:
            for entry_id in entry_ids:
                pipe.set(self.entry_key(entry_id), tombstone, ex=ttl, nx=True)
            await pipe.execute()

    async def extend_horizon(self, entry_id: str, expiry_at: datetime | None) -> bool:
        """Grow a live entry's, its barrier's and its live set's TTLs to outlast ``expiry_at``; never shrink one.

        Returns ``False`` when no live entry backs ``entry_id`` (absent or a tombstone).
        """
        key = self.entry_key(entry_id)
        async with self._client() as client:
            raw = await client.get(key)
            if raw is None:
                return False
            entry = json.loads(raw)
            if not isinstance(entry, dict) or self.tombstone_kind(entry) != "live":
                return False
            thread_id, superstep = self._entry_coordinates(entry, key)
            outcome = await cast(
                "Awaitable[int]",
                client.eval(
                    _EXTEND_HORIZON_SCRIPT,
                    3,
                    key,
                    self.barrier_key(thread_id, superstep),
                    self.live_set_key(thread_id),
                    raw,
                    str(entry_ttl_seconds(expiry_at)),
                    str(barrier_ttl_seconds([expiry_at])),
                ),
            )
        return outcome == 1

    def _entry_coordinates(self, entry: Mapping[str, Any], key: str) -> tuple[str, str]:
        coordinates = entry.get(_ENTRY_COORDINATES_FIELD)
        if not (
            isinstance(coordinates, list) and len(coordinates) == 2 and all(isinstance(c, str) for c in coordinates)
        ):
            raise ParkIndexCorruptError(f"park entry {key!r} carries no super-step coordinates")
        return coordinates[0], coordinates[1]


class DriveLease:
    """A super-step's drive lease: ``SET NX EX`` to win it, a heartbeat while held, a token-checked release.

    Win it by entering (``async with``) or :meth:`acquire`; leave it by exiting or :meth:`close`,
    which stops the heartbeat and releases it unless :meth:`ParkIndex.finalize` consumed it.
    """

    def __init__(self, index: ParkIndex, thread_id: str, superstep: str) -> None:
        """Prepare a lease on one super-step; nothing is claimed until :meth:`acquire`."""
        self.index = index
        self.thread_id = thread_id
        self.superstep = superstep
        self.token = uuid.uuid4().hex
        self.acquired = False
        self.consumed = False
        self.lost = False
        self.closed = False
        self._heartbeat: asyncio.Task[None] | None = None

    async def acquire(self) -> DriveLease:
        """Win the lease and start its heartbeat.

        Raises:
            DriveInProgressError: Another holder has it.
        """
        if self.acquired:
            raise RuntimeError("a drive lease is acquired once")
        key = self.index.lease_key(self.thread_id, self.superstep)
        async with self.index._client() as client:
            won = await client.set(key, self.token, nx=True, ex=DRIVE_LEASE_SECONDS)
        if not won:
            raise DriveInProgressError(self.thread_id, self.superstep)
        self.acquired = True
        self._heartbeat = asyncio.create_task(self._beat(), name=f"park-lease-{self.index.namespace}")
        return self

    async def __aenter__(self) -> DriveLease:
        """Acquire the lease."""
        return await self.acquire()

    async def __aexit__(self, *_exc: object) -> None:
        """Stop the heartbeat and release the lease (a consumed lease is not released again)."""
        await self.close()

    async def holds(self) -> bool:
        """Whether this lease's token still holds the super-step right now."""
        if not self.acquired or self.consumed:
            return False
        async with self.index._client() as client:
            held = await client.get(self.index.lease_key(self.thread_id, self.superstep))
        return held == self.token

    async def release(self) -> None:
        """Drop the lease if this token still holds it (a reclaimer's lease is never dropped)."""
        async with self.index._client() as client:
            await cast(
                "Awaitable[int]",
                client.eval(_RELEASE_SCRIPT, 1, self.index.lease_key(self.thread_id, self.superstep), self.token),
            )

    async def close(self) -> None:
        """Stop the heartbeat and release the lease once; a consumed lease is left as finalize left it.

        A cancel of the calling task delivered while the heartbeat stops propagates, after the release.
        """
        if self.closed:
            return
        self.closed = True
        try:
            await self._stop_heartbeat()
        finally:
            if self.acquired and not self.consumed:
                await self.release()

    async def mark_consumed(self) -> None:
        """Record that a finalize dropped the lease key; stops the heartbeat so no renew reports it lost."""
        self.consumed = True
        await self._stop_heartbeat()

    async def _stop_heartbeat(self) -> None:
        heartbeat, self._heartbeat = self._heartbeat, None
        if heartbeat is None or heartbeat is asyncio.current_task():
            return
        heartbeat.cancel()
        # ``asyncio.wait`` raises only for a cancel of the calling task, never for the heartbeat's own.
        await asyncio.wait({heartbeat})
        if not heartbeat.cancelled():
            heartbeat.result()

    async def _beat(self) -> None:
        key = self.index.lease_key(self.thread_id, self.superstep)
        while True:
            await asyncio.sleep(DRIVE_LEASE_HEARTBEAT_SECONDS)
            if self.consumed:
                return
            try:
                async with self.index._client() as client:
                    renewed = await cast(
                        "Awaitable[int]", client.eval(_RENEW_SCRIPT, 1, key, self.token, str(DRIVE_LEASE_SECONDS))
                    )
            except Exception:
                self.lost = True
                logger.exception(
                    "the drive lease of thread %r super-step %r could not be renewed; it is treated as lost",
                    self.thread_id,
                    self.superstep,
                )
                return
            if not renewed and not self.consumed:
                self.lost = True
                logger.error(
                    "the drive lease of thread %r super-step %r was taken by another writer while held",
                    self.thread_id,
                    self.superstep,
                )
                return
