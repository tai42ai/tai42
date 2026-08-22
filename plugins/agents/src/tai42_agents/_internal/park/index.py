"""The agents-plugin durable index that resolves an async-parked agent run by interaction id.

When an async ``ask_user`` parks a park-capable agent run, the flow-blind platform keeps only
the interaction id — never any agent, thread, or graph state. This index is how the
agents plugin reverses that id back to the parked run: at park it records
``interaction_id -> {agent_name, thread_id, superstep_id, interrupt_id, rebuild_kwargs, completion_tool}``
(any engine fact — a LangGraph checkpoint provider / recursion limit — rides INSIDE
``rebuild_kwargs``, never a top-level field, so the index stays provider-free);
when the platform later fires the continuation with ``{interaction_id, answer}`` the
resume entrypoint reads the entry to locate the thread, the interrupt the answer
targets, and the rebuild kwargs needed to recompile the same graph. Once the
super-step completes, each entry is replaced with a short-TTL resolved tombstone —
never deleted — so a redelivered answer resolves benignly instead of raising on an
absent key.

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

from tai42_agents._internal.park.errors import AgentResumeBarrierNotFoundError
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

# Marker written under a park key IN PLACE of the entry once its super-step drove cleanly. A
# lapped redelivery of a losing sibling's still-open due-record reads this and clears benignly,
# instead of finding an absent key (a genuine ordering race that must retry).
_RESOLVED_TOMBSTONE_FIELD: Final[str] = "__park_resolved__"

# TTL on a resolved tombstone. Sized comfortably above the platform's redelivery horizon for an
# answer's durable due-record — the interaction group ages out at its idle TTL (24h) after which
# no redelivery fires — so a lapped redelivery always lands on the tombstone rather than an
# absent key, while staying far below the 30-day park-entry backstop so a resolved slot never
# lingers.
_RESOLVED_TOMBSTONE_TTL_SECONDS: Final[int] = 60 * 60 * 48


def _park_key(interaction_id: str) -> str:
    return f"{_PARK_KEY_PREFIX}{interaction_id}"


def _barrier_key(thread_id: str, superstep_id: str) -> str:
    return f"{_STEP_KEY_PREFIX}{thread_id}:{superstep_id}"


def _claim_key(thread_id: str, superstep_id: str) -> str:
    return f"{_STEP_KEY_PREFIX}{thread_id}:{superstep_id}:claim"


def compute_superstep_id(interaction_ids: Iterable[str]) -> str:
    """Deterministic id for a parked super-step from its interaction ids.

    Sorting makes the id insensitive to enumeration order, so every one of the M
    concurrent continuations derives the same id and routes to the same barrier."""
    joined = ",".join(sorted(interaction_ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def barrier_ttl_seconds(expiries: Iterable[datetime | None]) -> int:
    """TTL for a super-step barrier: at least the park-entry backstop, extended to cover
    the latest ask deadline plus a margin. Flooring at the park-entry backstop keeps the
    barrier alive for at least as long as the park entries it coordinates, so a late
    answer never finds an entry with no barrier. Because it sizes to the LATEST deadline
    with the same margin every per-entry TTL uses, the barrier outlives every entry."""
    deadlines = [e for e in expiries if e is not None]
    if not deadlines:
        return _PARK_ENTRY_TTL_SECONDS
    horizon = int((max(deadlines) - datetime.now(UTC)).total_seconds()) + _BARRIER_TTL_MARGIN_SECONDS
    return max(_PARK_ENTRY_TTL_SECONDS, horizon)


def park_entry_ttl_seconds(expiry: datetime | None) -> int:
    """TTL for one park entry: the backstop floor, extended to outlast this ask's OWN deadline
    plus the same margin the barrier uses. A deadline-less entry keeps the bare floor. Sizing to
    the deadline keeps the entry alive at least as long as its answer/expiry continuation can
    fire, so under keep-forever checkpoint retention a valid in-window answer always finds it."""
    if expiry is None:
        return _PARK_ENTRY_TTL_SECONDS
    horizon = int((expiry - datetime.now(UTC)).total_seconds()) + _BARRIER_TTL_MARGIN_SECONDS
    return max(_PARK_ENTRY_TTL_SECONDS, horizon)


async def read_park_entry(interaction_id: str) -> dict[str, Any] | None:
    """The park entry for ``interaction_id``, or ``None`` when no entry exists (never
    parked here, already resumed, or its thread ended)."""
    async with _park_client() as client:
        raw = await client.get(_park_key(interaction_id))
    if raw is None:
        return None
    return json.loads(raw)


async def finalize_resolved_superstep(
    thread_id: str,
    superstep_id: str,
    item_interaction_ids: Iterable[str],
) -> None:
    """Finalize a cleanly-driven super-step in ONE Redis MULTI/EXEC: replace every one of its M
    park entries with a short-TTL resolved tombstone AND drop the barrier plus its drive-claim
    lease, all-or-nothing. Atomicity closes the crash window a per-key loop would leave — a hard
    crash mid-loop could tombstone only some siblings, and a redelivery of an un-tombstoned
    sibling would re-claim and storm on a not-pending resume until the interaction group's 24h
    give-up. Each tombstone keeps its own ``_RESOLVED_TOMBSTONE_TTL_SECONDS`` TTL so a resolved
    slot never lingers. A lapped redelivery of any sibling's orphaned due-record reads a resolved
    marker and clears benignly instead of mistaking an absent key for a permanently dropped
    resume. Called only after a successful drive; a crash mid-drive skips this and leaves every
    entry LIVE for a normal reclaim."""
    tombstone = json.dumps({_RESOLVED_TOMBSTONE_FIELD: True})
    async with _park_client() as client, client.pipeline(transaction=True) as pipe:
        for interaction_id in item_interaction_ids:
            pipe.set(_park_key(interaction_id), tombstone, ex=_RESOLVED_TOMBSTONE_TTL_SECONDS)
        pipe.delete(_barrier_key(thread_id, superstep_id))
        pipe.delete(_claim_key(thread_id, superstep_id))
        await pipe.execute()


async def persist_superstep(
    entries: dict[str, dict[str, Any]],
    thread_id: str,
    superstep_id: str,
    expected: dict[str, Any],
    expiries: dict[str, datetime | None],
    barrier_ttl_seconds: int,
) -> None:
    """Persist a suspended super-step in ONE Redis MULTI/EXEC: every one of its M park entries
    AND the barrier they converge on, all-or-nothing. Atomicity closes the crash window a
    per-key loop would leave — a hard crash mid-loop could write some entries without the
    barrier, or the barrier without every entry, stranding a resume that finds an entry with no
    barrier (buffer raises not-found) or a barrier expecting an interaction whose entry is
    missing. Each entry's TTL is ``park_entry_ttl_seconds`` of ITS ask's deadline (``expiries``,
    keyed by interaction id) — the backstop floor extended to outlast that deadline; the barrier's
    TTL floors above the LATEST of them so it outlives every entry it coordinates. The entries are
    written whole and keyed by interaction id, so a re-run super-step re-parking the same
    interaction rewrites identically rather than corrupting the index."""
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


def is_resolved_tombstone(entry: dict[str, Any]) -> bool:
    """True when a park-key read returned a resolved tombstone rather than a live park entry —
    the super-step already drove to completion."""
    return entry.get(_RESOLVED_TOMBSTONE_FIELD) is True


def _output_field(interaction_id: str) -> str:
    return f"output:{interaction_id}"


async def read_barrier(thread_id: str, superstep_id: str) -> dict[str, Any] | None:
    """The super-step barrier as ``{"expected": {...}, "outputs": {interaction: answer}}``,
    or ``None`` when no barrier exists. ``outputs`` holds only the answers buffered so far
    (the buffered answer JSON is decoded back to its value)."""
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
) -> tuple[int, int]:
    """Buffer one answer into the super-step barrier and report progress.

    ``HSETNX`` makes the write idempotent — a redelivered answer for an interaction
    already answered is a no-op, so ``present`` is stable across redeliveries. Returns
    ``(present, total)``: how many of the M expected interactions now hold an answer, and
    M. Raises ``AgentResumeBarrierNotFoundError`` when the barrier is gone, and
    ``KeyError`` (surfaced by the caller as a not-pending rejection) when the interaction
    is not one this super-step expects."""
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
    present = sum(1 for value in present_values if value is not None)
    return present, len(expected)


async def try_claim_drive(thread_id: str, superstep_id: str, token: str) -> bool:
    """Attempt to win the single drive of a completed barrier. ``SET NX EX`` grants the
    lease to exactly one caller; a stale (crashed-winner) lease has already expired, so a
    later redelivery reclaims here. ``True`` iff this caller won."""
    key = _claim_key(thread_id, superstep_id)
    async with _park_client() as client:
        won = await client.set(key, token, nx=True, ex=_DRIVE_LEASE_SECONDS)
    return bool(won)


async def renew_claim(thread_id: str, superstep_id: str, token: str) -> bool:
    """Heartbeat the drive lease: extend the TTL only while this caller still holds it, in
    a single atomic compare-and-expire so a stale holder cannot bump a reclaimer's TTL.
    ``False`` (holder changed or lease gone) tells the heartbeat loop to stop — the drive
    was superseded."""
    key = _claim_key(thread_id, superstep_id)
    async with _park_client() as client:
        renewed = await _eval(client, _RENEW_CLAIM_SCRIPT, 1, key, token, str(_DRIVE_LEASE_SECONDS))
    return bool(renewed)


async def heartbeat_drive_claim(thread_id: str, superstep_id: str, token: str) -> None:
    """Renew the drive lease at a fixed interval while the winner drives, so a slow but
    live drive is never reclaimed. Exits when the lease is no longer held by this token
    (superseded) — a drive that lost its lease must not renew it. Run as a background task
    cancelled when the drive returns."""
    while True:
        await asyncio.sleep(_DRIVE_LEASE_HEARTBEAT_SECONDS)
        if not await renew_claim(thread_id, superstep_id, token):
            return


async def release_claim(thread_id: str, superstep_id: str, token: str) -> None:
    """Release the drive lease on a caught drive failure so a retry reclaims at once (a
    hard crash leaves the lease to expire instead). A single atomic compare-and-delete,
    token-checked so a reclaimer's fresh lease is never dropped."""
    key = _claim_key(thread_id, superstep_id)
    async with _park_client() as client:
        await _eval(client, _RELEASE_CLAIM_SCRIPT, 1, key, token)
