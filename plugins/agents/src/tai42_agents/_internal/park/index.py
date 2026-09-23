"""The agents-plugin durable index that resolves an async-parked agent run by interaction id.

When an async ``ask`` parks a park-capable agent run, the flow-blind platform keeps only
the interaction id — never any agent, thread, or graph state. This index is how the
agents plugin reverses that id back to the parked run: at park it records
``interaction_id -> {agent_name, thread_id, superstep_id, interrupt_id, rebuild_kwargs,
completion_tool, completion_context, retention_bound}``
(any engine fact — a LangGraph checkpoint provider / recursion limit — rides INSIDE
``rebuild_kwargs``, never a top-level field, so the index stays provider-free);
when the platform later fires the continuation with ``{interaction_id, answer}`` the
resume entrypoint reads the entry to locate the thread, the interrupt the answer
targets, and the rebuild kwargs needed to recompile the same graph. Once the
super-step completes, each entry is replaced with a short-TTL resolved tombstone —
never deleted — so a redelivered answer resolves benignly instead of raising on an
absent key.

The key space is RESUME KEYS, not only interaction ids: a run that parks on a nested CALL it
chained records that chained key here the same way, and the same reversal drives it. Nothing
in this module reads a key's meaning — the driver decides which policy a key kind gets.

It lives in the agents plugin's OWN Redis (``agents_park_redis_settings`` /
``TAI_AGENTS_REDIS_URL``, falling back to ``TAI_DEFAULT_REDIS_URL``), independent of the
checkpoint provider, so the index survives a cross-worker resume even when the paused
graph is checkpointed to postgres. An unset agents Redis fails loudly at the write
through ``RedisClient``'s not-configured guard.

The structure — per-interaction entry, per-super-step barrier, drive lease with
heartbeat and token-checked release — is what the platform's continuation seam
(at-least-once, redelivered, flow-blind) demands, identically for every consumer that
resumes through it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Iterable
from datetime import UTC, datetime
from typing import Any, Final, cast

from tai42_contract.app import tai42_app

from tai42_agents._internal.park.errors import AgentResumeBarrierNotFoundError, AgentSuperstepLeaseLostError
from tai42_agents.settings import agents_park_redis_settings


# Redis (reached through the kit's ``RedisClient``) is an OPTIONAL feature dependency: the
# park index is the only agents consumer of it, and a deployment that never async-parks
# never touches it. So the kit redis client is imported LAZILY here, keeping the shipped
# agents import graph a light leaf; an unconfigured / uninstalled redis surfaces loudly at
# the first park write, never silently.
@contextlib.asynccontextmanager
async def _park_client() -> AsyncIterator[Any]:
    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    async with client_ctx(RedisClient, agents_park_redis_settings()) as client:
        yield client


# Typed seams over the redis-py async client, mirroring the kit's ``hgetall`` /
# ``hset_mapping``: the shared sync/async command stubs annotate a ``Awaitable[T] | T``
# return, so ``await client.hget(...)`` fails type checking though the async client
# always returns an awaitable. These pin the async half.
def _hget(client: Any, key: str, field: str) -> Awaitable[str | None]:
    return cast("Awaitable[str | None]", client.hget(key, field))


def _hsetnx(client: Any, key: str, field: str, value: str) -> Awaitable[int]:
    return cast("Awaitable[int]", client.hsetnx(key, field, value))


def _hmget(client: Any, key: str, fields: list[str]) -> Awaitable[list[str | None]]:
    return cast("Awaitable[list[str | None]]", client.hmget(key, fields))


def _eval(client: Any, script: str, numkeys: int, *keys_and_args: str) -> Awaitable[Any]:
    return cast("Awaitable[Any]", client.eval(script, numkeys, *keys_and_args))


# Compare-and-expire the drive lease in ONE round trip: the EXPIRE fires only while the
# stored token still matches this holder. A non-atomic GET-then-EXPIRE would let a stale
# holder that read its own token, then lapsed and was reclaimed, bump the reclaimer's
# fresh TTL between the two calls.
_RENEW_CLAIM_SCRIPT: Final[str] = """
-- agent:drive-lease-renew
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""

# Compare-and-delete the drive lease in ONE round trip: the DEL fires only while the
# stored token still matches this holder, so a stale holder's release can never drop a
# reclaimer's fresh lease.
_RELEASE_CLAIM_SCRIPT: Final[str] = """
-- agent:drive-lease-release
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

# Finalize a resolved super-step in ONE round trip, GUARDED by the caller's drive-lease token:
# the whole write lands only while the claim key (KEYS[1]) still holds ARGV[1]. Both writers of a
# super-step's resolution — the resume drive and the whole-chain kill — pass the token they hold,
# so the two can never both land: whoever holds the lease wins, the other is refused (return 0)
# and raises rather than overwriting the winner's record. Writes every park entry (KEYS[5..]) as a
# tombstone, the resolution record (KEYS[3]), the run resolution index member + its TTL (KEYS[4]),
# and drops the barrier (KEYS[2]) and the claim lease (KEYS[1]) — all-or-nothing, so a crash
# tombstones either none or all. Returns 1 on success, 0 when the token no longer holds the claim.
_FINALIZE_SUPERSTEP_SCRIPT: Final[str] = """
-- agent:finalize-superstep
if redis.call('get', KEYS[1]) ~= ARGV[1] then
    return 0
end
local ttl = tonumber(ARGV[2])
for i = 5, #KEYS do
    redis.call('set', KEYS[i], ARGV[3], 'EX', ttl)
end
redis.call('set', KEYS[3], ARGV[4], 'EX', ttl)
redis.call('hset', KEYS[4], ARGV[5], ARGV[6])
redis.call('expire', KEYS[4], ttl)
redis.call('del', KEYS[2])
redis.call('del', KEYS[1])
return 1
"""


# One key per parked interaction, namespaced to the agents plugin so it never collides
# with another consumer on a shared Redis.
_PARK_KEY_PREFIX = "agent:park:"

# One super-step barrier hash per parked super-step, and its companion drive-claim lease
# key. Keyed by thread + a super-step id (a hash of the sorted interrupt ids) so a run
# that re-parks on a later super-step of the SAME thread never collides with the barrier
# it is resuming from.
_STEP_KEY_PREFIX = "agent:park:step:"

# Floor TTL under every park entry: even a never-resolved interaction whose deadline is short
# (or absent) survives at least this long, so a late answer/expiry continuation still finds its
# entry and a truly abandoned key still drops out instead of leaking forever. The EFFECTIVE
# per-entry TTL is not this floor but ``park_entry_ttl_seconds`` of the ask's OWN deadline —
# extended above the floor to outlast any deadline past it (enforced here, never assumed to fall
# under it), so under keep-forever checkpoint retention an in-window answer never TTLs out first.
_PARK_ENTRY_TTL_SECONDS: Final[int] = 60 * 60 * 24 * 30

# Extra headroom added over the latest ask deadline when sizing the barrier TTL, so the
# expiry continuation that fires AT the deadline still finds the barrier.
_BARRIER_TTL_MARGIN_SECONDS: Final[int] = 60 * 60

# The drive-claim lease. The winner of the barrier holds this TTL'd key while it drives
# the single resume; a heartbeat renews it in place. A crashed winner's lease simply
# expires, so a later redelivery reclaims and re-drives — the claim is a lease, never a
# one-shot lock that could strand the super-step.
_DRIVE_LEASE_SECONDS: Final[int] = 60
_DRIVE_LEASE_HEARTBEAT_SECONDS: Final[float] = 20.0

# Marker written under a park key IN PLACE of the entry once its super-step resolved. A lapped
# redelivery of a losing sibling's still-open due-record reads this and REPLAYS the super-step's
# stored resolution, instead of finding an absent key (a genuine ordering race that must retry). A
# real resolution's tombstone carries the ``thread_id``/``superstep_id`` that locate its
# resolution record; a benign detach tombstone (a chained key claimed but never parked) carries
# neither, so a fire landing on it reads no record and no-ops.
_RESOLVED_TOMBSTONE_FIELD: Final[str] = "__park_resolved__"

# One resolution record per resolved super-step, holding ``{resolution, value}`` — the outcome the
# driver returned to the platform for that super-step, so a redrive of any still-open due-record
# replays it (a committed delivery dedupes, an uncommitted one lands). Keyed by (thread,
# super-step) beside the barrier key.
_RESOLUTION_KEY_PREFIX = "agent:park:resolution:"

# One per-run (thread) index of the super-steps whose resolution records exist, mapping
# ``superstep_id -> [interaction_ids]``. A whole-chain kill reads it to reach and drop every
# resolution record + tombstone of the killed run (erase reach); nothing else reads it.
# Refreshed to the resolution TTL on every finalize, so it outlives the records it points at and no
# longer.
_RUN_RESOLUTION_INDEX_PREFIX = "agent:park:run-resolutions:"


def _resolved_tombstone_ttl_seconds() -> int:
    """TTL for a resolution record and its tombstones: twice the platform's redelivery horizon.

    A resolution record and its tombstones must outlive the LAST redelivery the reaper can fire —
    one that fires at the horizon and lands on them — plus reaper-cycle and clock-skew margin, and
    no longer (they hold the run's outcome, which can carry person data). Derived from the platform
    facet ``redelivery_horizon_seconds()`` at write time, never a compile-time guess of the horizon.
    """
    return 2 * tai42_app.interactions.redelivery_horizon_seconds()


def _park_key(interaction_id: str) -> str:
    return f"{_PARK_KEY_PREFIX}{interaction_id}"


def _resolution_key(thread_id: str, superstep_id: str) -> str:
    return f"{_RESOLUTION_KEY_PREFIX}{thread_id}:{superstep_id}"


def _run_resolution_index_key(thread_id: str) -> str:
    return f"{_RUN_RESOLUTION_INDEX_PREFIX}{thread_id}"


def _barrier_key(thread_id: str, superstep_id: str) -> str:
    return f"{_STEP_KEY_PREFIX}{thread_id}:{superstep_id}"


def _claim_key(thread_id: str, superstep_id: str) -> str:
    return f"{_STEP_KEY_PREFIX}{thread_id}:{superstep_id}:claim"


def compute_superstep_id(interaction_ids: Iterable[str]) -> str:
    """Deterministic id for a parked super-step from its interaction ids.

    Sorting makes the id insensitive to enumeration order, so every one of the M
    concurrent continuations derives the same id and routes to the same barrier.
    """
    joined = ",".join(sorted(interaction_ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def barrier_ttl_seconds(expiries: Iterable[datetime | None]) -> int:
    """TTL for a super-step barrier: the park-entry backstop, extended to cover the latest ask deadline plus a margin.

    Flooring at the park-entry backstop keeps the
    barrier alive for at least as long as the park entries it coordinates, so a late
    answer never finds an entry with no barrier. Because it sizes to the LATEST deadline
    with the same margin every per-entry TTL uses, the barrier outlives every entry.
    """
    deadlines = [e for e in expiries if e is not None]
    if not deadlines:
        return _PARK_ENTRY_TTL_SECONDS
    horizon = int((max(deadlines) - datetime.now(UTC)).total_seconds()) + _BARRIER_TTL_MARGIN_SECONDS
    return max(_PARK_ENTRY_TTL_SECONDS, horizon)


def park_entry_ttl_seconds(expiry: datetime | None) -> int:
    """TTL for one park entry: the backstop floor, extended to outlast this ask's OWN deadline plus a margin.

    The margin matches the one the barrier uses. A deadline-less entry keeps the bare floor. Sizing to
    the deadline keeps the entry alive at least as long as its answer/expiry continuation can
    fire, so under keep-forever checkpoint retention a valid in-window answer always finds it.
    """
    if expiry is None:
        return _PARK_ENTRY_TTL_SECONDS
    horizon = int((expiry - datetime.now(UTC)).total_seconds()) + _BARRIER_TTL_MARGIN_SECONDS
    return max(_PARK_ENTRY_TTL_SECONDS, horizon)


async def read_park_entry(interaction_id: str) -> dict[str, Any] | None:
    """The park entry for ``interaction_id``, or ``None`` when no entry exists.

    No entry exists when never parked here, already resumed, or its thread ended.
    """
    async with _park_client() as client:
        raw = await client.get(_park_key(interaction_id))
    if raw is None:
        return None
    return json.loads(raw)


async def finalize_resolved_superstep(
    thread_id: str,
    superstep_id: str,
    item_interaction_ids: Iterable[str],
    *,
    resolution: str,
    value: Any,
    token: str,
) -> None:
    """Finalize a resolved super-step in ONE guarded Redis round trip: tombstones, its resolution record, and cleanup.

    GUARDED by ``token`` — the caller's drive-lease token: the whole write lands only while the
    claim key still holds ``token``. Both writers of a super-step's resolution — the resume drive
    and the whole-chain kill — pass the token they hold, so the two can never both land; the one
    that no longer holds the lease is refused and this raises :class:`AgentSuperstepLeaseLostError`
    rather than overwriting the winner's resolution record.

    On the guarded write it replaces every one of its M park entries with a resolved tombstone,
    writes the super-step's ONE resolution record (``{resolution, value}`` — the outcome the driver
    returned to the platform), AND drops the barrier plus its drive-claim lease, all-or-nothing.
    Atomicity closes the crash window a per-key loop would leave — a hard crash mid-loop could
    tombstone only some siblings, and a redelivery of an un-tombstoned sibling would re-claim and
    storm on a not-pending resume until the interaction group's give-up. Each tombstone carries this
    super-step's ``thread_id`` / ``superstep_id`` so a lapped redelivery of ANY sibling's orphaned
    due-record LOCATES the resolution record and REPLAYS it, rather than mistaking an absent key for
    a permanently dropped resume. Record and tombstones share the horizon-derived resolution TTL so a
    resolved slot outlives the last redelivery and no longer. Called only after a drive reaches a
    resolution; a crash mid-drive skips this and leaves every entry LIVE for a normal reclaim.

    ``resolution`` is one of ``terminal`` (``value`` = the outermost run's outcome the driver
    returns), ``suspended`` (``value`` = the re-park suspended return the face re-normalises), or
    ``aborted`` (written by the kill teardown; ``value`` = the aborted outcome a redrive re-raises
    as ``ParkResumeFailed``). ``value`` is stored as-is and must be JSON-serializable.

    The super-step is also recorded in its run's resolution index (``superstep_id ->
    [interaction_ids]``), refreshed to the same TTL, so a whole-chain kill can reach every record +
    tombstone of the run.
    """
    ttl = _resolved_tombstone_ttl_seconds()
    ids = list(item_interaction_ids)
    tombstone = json.dumps({_RESOLVED_TOMBSTONE_FIELD: True, "thread_id": thread_id, "superstep_id": superstep_id})
    record = json.dumps({"resolution": resolution, "value": value})
    async with _park_client() as client:
        landed = await _eval(
            client,
            _FINALIZE_SUPERSTEP_SCRIPT,
            4 + len(ids),
            _claim_key(thread_id, superstep_id),
            _barrier_key(thread_id, superstep_id),
            _resolution_key(thread_id, superstep_id),
            _run_resolution_index_key(thread_id),
            *(_park_key(interaction_id) for interaction_id in ids),
            token,
            str(ttl),
            tombstone,
            record,
            superstep_id,
            json.dumps(ids),
        )
    if not landed:
        raise AgentSuperstepLeaseLostError(thread_id, superstep_id)


async def read_run_resolutions(thread_id: str) -> dict[str, list[str]]:
    """Every resolved super-step of a run as ``{superstep_id: [interaction_ids]}``, or ``{}`` when none.

    The whole-chain kill teardown reads this to reach every resolution record + tombstone of the
    killed run and drop them (erase reach). ``{}`` when the run has no resolved super-step (or the
    index aged out).
    """
    from tai42_kit.clients.impl.redis import hgetall

    async with _park_client() as client:
        raw = await hgetall(client, _run_resolution_index_key(thread_id))
    if not raw:
        return {}
    return {superstep_id: json.loads(ids) for superstep_id, ids in raw.items()}


async def read_superstep_resolution(thread_id: str, superstep_id: str) -> dict[str, Any] | None:
    """The ``{resolution, value}`` a resolved super-step stored, or ``None`` when no record exists.

    ``None`` covers a benign detach tombstone (a chained key claimed but never parked, which writes
    no record) and a record aged out past its TTL. The caller replays a present record and treats
    ``None`` as a benign no-op landing.
    """
    async with _park_client() as client:
        raw = await client.get(_resolution_key(thread_id, superstep_id))
    if raw is None:
        return None
    return json.loads(raw)


async def drop_run_resolution(thread_id: str, superstep_id: str, item_interaction_ids: Iterable[str]) -> None:
    """Erase a super-step's stored outcome: its resolution record AND its tombstones, in ONE MULTI/EXEC.

    The whole-chain kill teardown calls this so a person erase reaches the outcome a resolution
    record holds — a fully-delivered run leaves nothing on the platform subject
    index, so its driver-held record would otherwise linger to the TTL. Dropping the tombstones too
    means a still-buffered sibling's redelivery finds an absent key; the kill chokepoint clears
    those sibling due-records in the same teardown, so no redelivery survives the drop. The
    super-step is removed from its run's resolution index in the same MULTI.
    """
    async with _park_client() as client, client.pipeline(transaction=True) as pipe:
        for interaction_id in item_interaction_ids:
            pipe.delete(_park_key(interaction_id))
        pipe.delete(_resolution_key(thread_id, superstep_id))
        pipe.hdel(_run_resolution_index_key(thread_id), superstep_id)
        await pipe.execute()


async def persist_superstep(
    entries: dict[str, dict[str, Any]],
    thread_id: str,
    superstep_id: str,
    expected: dict[str, Any],
    expiries: dict[str, datetime | None],
    barrier_ttl_seconds: int,
) -> None:
    """Persist a suspended super-step in ONE Redis MULTI/EXEC.

    Writes every one of its M park entries AND the barrier they converge on, all-or-nothing. Atomicity closes the
    crash window a per-key loop would leave — a hard crash mid-loop could write some entries without the
    barrier, or the barrier without every entry, stranding a resume that finds an entry with no
    barrier (buffer raises not-found) or a barrier expecting an interaction whose entry is
    missing. Each entry's TTL is ``park_entry_ttl_seconds`` of ITS ask's deadline (``expiries``,
    keyed by interaction id) — the backstop floor extended to outlast that deadline; the barrier's
    TTL floors above the LATEST of them so it outlives every entry it coordinates. The entries are
    written whole and keyed by interaction id, so a re-run super-step re-parking the same
    interaction rewrites identically rather than corrupting the index.
    """
    async with _park_client() as client, client.pipeline(transaction=True) as pipe:
        for interaction_id, entry in entries.items():
            pipe.set(
                _park_key(interaction_id),
                json.dumps(entry),
                ex=park_entry_ttl_seconds(expiries.get(interaction_id)),
            )
        barrier = _barrier_key(thread_id, superstep_id)
        pipe.hset(barrier, mapping={"expected": json.dumps(expected)})
        pipe.expire(barrier, barrier_ttl_seconds)
        await pipe.execute()


async def detach_chained_parks(keys: Iterable[str]) -> None:
    """Write a resolved tombstone for each chained key a drive CLAIMED but never parked on.

    A chained key names a call whose nested run will still fire its terminal at it. When the
    claiming drive ends without recording a park — it errored, or it moved on without pausing —
    that fire would otherwise hunt an entry that never existed and be retried until the
    platform's delivery horizon gives up. The tombstone gives it the benign already-resolved
    landing instead, on the same key and with the same short TTL a cleanly-driven super-step
    leaves behind.

    Written ``NX``, so it can never overwrite live state: a key that DOES hold a park entry (a
    concurrent re-drive that reached the persist first) or an existing tombstone is left exactly
    as it is. Called with the drive's leftover claims, so a drive that parked on everything it
    claimed writes nothing.

    The tombstone carries NO ``thread_id`` / ``superstep_id`` and no resolution record is written:
    a detached chain never drove a super-step, so a terminal that lands on it reads no resolution
    and takes the benign no-op landing rather than replaying a stored outcome.
    """
    ttl = _resolved_tombstone_ttl_seconds()
    tombstone = json.dumps({_RESOLVED_TOMBSTONE_FIELD: True})
    async with _park_client() as client, client.pipeline(transaction=True) as pipe:
        for key in keys:
            pipe.set(_park_key(key), tombstone, ex=ttl, nx=True)
        await pipe.execute()


# Extend a park's keys to a LATER horizon in one round trip, and never shorten one. A park
# entry and its barrier are sized to a deadline; when that deadline moves out (a chained park
# whose nested run re-parked on a further ask) both must outlive the new one. A key with no
# expiry (-1) is already unbounded and left alone; a missing key (-2) has nothing to extend.
_EXTEND_HORIZON_SCRIPT: Final[str] = """
-- agent:park-horizon-extend
local extended = 0
for i = 1, #KEYS do
    local current = redis.call('ttl', KEYS[i])
    local wanted = tonumber(ARGV[i])
    if current >= 0 and current < wanted then
        redis.call('expire', KEYS[i], wanted)
        extended = extended + 1
    end
end
return extended
"""


async def extend_park_horizon(interaction_id: str, thread_id: str, superstep_id: str, expiry: datetime) -> bool:
    """Extend a park entry AND its super-step barrier to outlive ``expiry``, never shortening either.

    Returns whether anything was extended.

    Sized by the SAME :func:`park_entry_ttl_seconds` / :func:`barrier_ttl_seconds` the persist
    uses, so an extended park is indistinguishable from one persisted at the new deadline. The
    barrier is sized to the same deadline as the entry, keeping the invariant that it outlives
    every entry it coordinates.
    """
    async with _park_client() as client:
        extended = await _eval(
            client,
            _EXTEND_HORIZON_SCRIPT,
            2,
            _park_key(interaction_id),
            _barrier_key(thread_id, superstep_id),
            str(park_entry_ttl_seconds(expiry)),
            str(barrier_ttl_seconds([expiry])),
        )
    return bool(extended)


def is_resolved_tombstone(entry: dict[str, Any]) -> bool:
    """True when a park-key read returned a resolved tombstone rather than a live park entry.

    The super-step already drove to completion.
    """
    return entry.get(_RESOLVED_TOMBSTONE_FIELD) is True


def _output_field(interaction_id: str) -> str:
    return f"output:{interaction_id}"


async def read_barrier(thread_id: str, superstep_id: str) -> dict[str, Any] | None:
    """The super-step barrier as ``{"expected": {...}, "outputs": {interaction: answer}}``, or ``None`` when absent.

    ``outputs`` holds only the answers buffered so far (the buffered answer JSON is decoded back to its value).
    """
    from tai42_kit.clients.impl.redis import hgetall

    key = _barrier_key(thread_id, superstep_id)
    async with _park_client() as client:
        raw = await hgetall(client, key)
    if not raw:
        return None
    expected = json.loads(raw["expected"])
    outputs = {
        field[len("output:") :]: json.loads(value) for field, value in raw.items() if field.startswith("output:")
    }
    return {"expected": expected, "outputs": outputs}


async def buffer_answer(
    thread_id: str,
    superstep_id: str,
    interaction_id: str,
    answer: Any,
) -> tuple[int, int, list[str]]:
    """Buffer one answer into the super-step barrier and report progress.

    ``HSETNX`` makes the write idempotent — a redelivered answer for an interaction
    already answered is a no-op, so ``present`` is stable across redeliveries. Returns
    ``(present, total, remaining_ids)``: how many of the M expected interactions now hold an
    answer, M, and the still-unanswered interaction ids of this super-step (in the barrier's
    ``expected`` order). ``remaining_ids`` is what a buffered-but-not-complete resume reports as
    the ``ResumeBuffered`` partition. Raises ``AgentResumeBarrierNotFoundError`` when the barrier
    is gone, and ``KeyError`` (surfaced by the caller as a not-pending rejection) when the
    interaction is not one this super-step expects.
    """
    key = _barrier_key(thread_id, superstep_id)
    async with _park_client() as client:
        expected_raw = await _hget(client, key, "expected")
        if expected_raw is None:
            raise AgentResumeBarrierNotFoundError(thread_id, superstep_id)
        expected: dict[str, str] = json.loads(expected_raw)
        if interaction_id not in expected:
            raise KeyError(interaction_id)
        await _hsetnx(client, key, _output_field(interaction_id), json.dumps(answer))
        present_values = await _hmget(client, key, [_output_field(i) for i in expected])
    remaining_ids = [iid for iid, value in zip(expected, present_values, strict=True) if value is None]
    present = sum(1 for value in present_values if value is not None)
    return present, len(expected), remaining_ids


async def try_claim_drive(thread_id: str, superstep_id: str, token: str) -> bool:
    """Attempt to win the single drive of a completed barrier.

    ``SET NX EX`` grants the lease to exactly one caller; a stale (crashed-winner) lease has already expired, so a
    later redelivery reclaims here. ``True`` iff this caller won.
    """
    key = _claim_key(thread_id, superstep_id)
    async with _park_client() as client:
        won = await client.set(key, token, nx=True, ex=_DRIVE_LEASE_SECONDS)
    return bool(won)


async def holds_claim(thread_id: str, superstep_id: str, token: str) -> bool:
    """Whether the drive lease is still held by ``token`` right now — a pure read, no TTL change.

    The drive re-checks this after the drive returns and BEFORE its terminal chain fire: the lease
    heartbeat stops when the drive returns, so a whole-chain kill can reclaim a lapsed lease while
    the terminal cascade runs. A ``False`` means another writer owns the super-step's resolution, so
    the drive fires no chain routing for a run it no longer owns.
    """
    key = _claim_key(thread_id, superstep_id)
    async with _park_client() as client:
        held = await client.get(key)
    return held == token


async def renew_claim(thread_id: str, superstep_id: str, token: str) -> bool:
    """Heartbeat the drive lease: extend the TTL only while this caller still holds it.

    A single atomic compare-and-expire so a stale holder cannot bump a reclaimer's TTL.
    ``False`` (holder changed or lease gone) tells the heartbeat loop to stop — the drive was superseded.
    """
    key = _claim_key(thread_id, superstep_id)
    async with _park_client() as client:
        renewed = await _eval(client, _RENEW_CLAIM_SCRIPT, 1, key, token, str(_DRIVE_LEASE_SECONDS))
    return bool(renewed)


async def heartbeat_drive_claim(thread_id: str, superstep_id: str, token: str) -> None:
    """Renew the drive lease at a fixed interval while the winner drives, so a slow but live drive is never reclaimed.

    Exits when the lease is no longer held by this token
    (superseded) — a drive that lost its lease must not renew it. Run as a background task
    cancelled when the drive returns.
    """
    while True:
        await asyncio.sleep(_DRIVE_LEASE_HEARTBEAT_SECONDS)
        if not await renew_claim(thread_id, superstep_id, token):
            return


async def release_claim(thread_id: str, superstep_id: str, token: str) -> None:
    """Release the drive lease on a caught drive failure so a retry reclaims at once.

    A hard crash leaves the lease to expire instead. A single atomic compare-and-delete,
    token-checked so a reclaimer's fresh lease is never dropped.
    """
    key = _claim_key(thread_id, superstep_id)
    async with _park_client() as client:
        await _eval(client, _RELEASE_CLAIM_SCRIPT, 1, key, token)
