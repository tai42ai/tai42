"""The provider-free park persist seam: the expiry-vs-retention gate, chained-key horizon
clamping, and the atomic super-step write.

The agents' park index is bound to an in-memory fakeredis; a directly-constructed
:class:`ParkIdentity` carries the retention bound the generalized persist gate reads.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from tai42_kit.interactions.park_index import BARRIER_TTL_MARGIN_SECONDS, ENTRY_TTL_FLOOR_SECONDS, superstep_id
from tests.conftest import bind_park_index

from tai42_agents._internal.park import ParkIdentity, persist_park
from tai42_agents._internal.park import persist as persist_mod
from tai42_agents._internal.park.errors import ParkExpiryExceedsRetentionError
from tai42_agents._internal.park.park_binding import agents_park_index


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    """Route the park index at a shared in-memory fakeredis and report the park Redis as
    configured (so a run is judged park-capable)."""
    redis = aioredis.FakeRedis(decode_responses=True)
    bind_park_index(monkeypatch, redis)
    return redis


# ---- expiry-vs-retention gate ---------------------------------------------


def _park_identity(retention_bound: datetime | None = None) -> ParkIdentity:
    """A directly-constructed provider-free identity: the caller passes the retention bound
    (a datetime, or ``None`` for keep-forever) that the generalized persist gate reads."""
    return ParkIdentity(
        agent_name="langchain_deep_agent",
        thread_id="t-gate",
        rebuild_kwargs={},
        bind=True,
        retention_bound=retention_bound,
    )


def _iso_in(minutes: float) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def _bound_in(minutes: float) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


def test_park_persist_allows_expiry_within_retention(fake_park_redis: Any) -> None:
    async def go() -> None:
        interactions = {"i1": _iso_in(30), "i2": _iso_in(10)}
        await persist_park(_park_identity(_bound_in(60)), [("int1", interactions)])
        assert await agents_park_index().read_entry("i1") is not None
        assert await agents_park_index().read_entry("i2") is not None

    asyncio.run(go())


def test_park_persist_refuses_expiry_beyond_retention_all_or_nothing(fake_park_redis: Any) -> None:
    async def go() -> None:
        # ``i2``'s deadline outlives the 60-minute retention bound.
        interactions = {"i1": _iso_in(30), "i2": _iso_in(120)}
        with pytest.raises(ParkExpiryExceedsRetentionError) as excinfo:
            await persist_park(_park_identity(_bound_in(60)), [("int1", interactions)])
        assert excinfo.value.interaction_id == "i2"
        # All-or-nothing: not a single index key was written.
        assert await agents_park_index().read_entry("i1") is None
        assert await agents_park_index().read_entry("i2") is None

    asyncio.run(go())


def test_park_persist_refuses_mixed_within_and_beyond_all_or_nothing(fake_park_redis: Any) -> None:
    async def go() -> None:
        # Offender first, a valid sibling after it: the whole super-step still fails with no writes.
        interactions = {"i_bad": _iso_in(9999), "i_ok": _iso_in(5)}
        with pytest.raises(ParkExpiryExceedsRetentionError) as excinfo:
            await persist_park(_park_identity(_bound_in(60)), [("int1", interactions)])
        assert excinfo.value.interaction_id == "i_bad"
        assert await agents_park_index().read_entry("i_bad") is None
        assert await agents_park_index().read_entry("i_ok") is None

    asyncio.run(go())


def test_park_persist_refuses_missing_expiry_under_bounded_retention(fake_park_redis: Any) -> None:
    async def go() -> None:
        # A park with no deadline under a bounded retention is unresumable — refuse it loudly.
        interactions: dict[str, Any] = {"i1": _iso_in(10), "i2": None}
        with pytest.raises(ParkExpiryExceedsRetentionError) as excinfo:
            await persist_park(_park_identity(_bound_in(60)), [("int1", interactions)])
        assert excinfo.value.interaction_id == "i2"
        assert excinfo.value.expiry_at is None
        assert await agents_park_index().read_entry("i1") is None
        assert await agents_park_index().read_entry("i2") is None

    asyncio.run(go())


def test_park_persist_allows_any_expiry_under_keep_forever_redis(fake_park_redis: Any) -> None:
    async def go() -> None:
        # A ``None`` retention bound = keep-forever: a far-future deadline and a deadline-less
        # park both pass.
        interactions: dict[str, Any] = {"i1": _iso_in(10_000_000), "i2": None}
        await persist_park(_park_identity(None), [("int1", interactions)])
        assert await agents_park_index().read_entry("i1") is not None
        assert await agents_park_index().read_entry("i2") is not None

    asyncio.run(go())


# ---- chained parks: the inherited horizon, and dead chains --------------------------------


_CHAIN = "tai42:chained-park:k1"


def test_a_chained_key_clamps_its_inherited_horizon_into_retention(fake_park_redis: Any) -> None:
    async def go() -> None:
        # A chained key waits on a nested CALL: its deadline is INHERITED, not its own ask's, so
        # one beyond the retention bound is CLAMPED into it rather than failing the park —
        # nothing fires at a chained deadline, so a shortened one costs nothing, and the park
        # stays inside the window its own state survives.
        bound = _bound_in(60)
        written = await persist_park(_park_identity(bound), [("int1", {_CHAIN: _iso_in(9999)})])
        assert datetime.fromisoformat(written[_CHAIN]) == bound
        assert await agents_park_index().read_entry(_CHAIN) is not None

    asyncio.run(go())


def test_a_chained_key_with_no_inherited_deadline_takes_the_cap(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def go() -> None:
        # Never unbounded: a nested run that carried no deadline at all still leaves the waiting
        # caller with a bounded park — an interaction id in the same position would be refused
        # under a bounded retention, because nothing could resume it.
        monkeypatch.setattr(
            persist_mod, "agents_limits_settings", lambda: SimpleNamespace(chained_park_horizon_cap_hours=2)
        )
        written = await persist_park(_park_identity(None), [("int1", {_CHAIN: None})])
        capped = datetime.fromisoformat(written[_CHAIN])
        assert capped <= datetime.now(UTC) + timedelta(hours=2)
        entry = await agents_park_index().read_entry(_CHAIN)
        assert entry is not None
        # The bound the persist gated against rides the entry, so a later extension re-clamps
        # against the same one instead of guessing.
        assert entry["retention_bound"] is None

    asyncio.run(go())


def test_an_interaction_id_keeps_its_own_ask_deadline(fake_park_redis: Any) -> None:
    async def go() -> None:
        # Only chained keys are clamped: a real ask's deadline is its own and rides through
        # untouched, still meeting the retention gate on its own terms.
        deadline = _iso_in(30)
        written = await persist_park(_park_identity(_bound_in(60)), [("int1", {"i1": deadline})])
        assert written == {"i1": deadline}

    asyncio.run(go())


def test_extending_a_park_horizon_never_shortens_it(fake_park_redis: Any) -> None:
    async def go() -> None:
        await persist_park(_park_identity(None), [("int1", {_CHAIN: _iso_in(60)})])
        entry = await agents_park_index().read_entry(_CHAIN)
        assert entry is not None
        before = await fake_park_redis.ttl(f"agent:park:{_CHAIN}")
        index = agents_park_index()
        # A NEARER deadline than the park already holds: an extension is not a re-sizing, so it
        # leaves both keys alone rather than cutting a park short.
        assert await index.extend_horizon(_CHAIN, _bound_in(1)) is True
        assert await fake_park_redis.ttl(f"agent:park:{_CHAIN}") == before
        # A LATER one moves both the entry and the barrier out.
        assert await index.extend_horizon(_CHAIN, datetime.now(UTC) + timedelta(days=90)) is True
        assert await fake_park_redis.ttl(f"agent:park:{_CHAIN}") > before
        assert await fake_park_redis.ttl(f"agent:park:step:t-gate:{entry['superstep_id']}") > before

    asyncio.run(go())


def test_detaching_a_dead_chain_leaves_a_benign_tombstone(fake_park_redis: Any) -> None:
    async def go() -> None:
        await agents_park_index().detach([_CHAIN])
        entry = await agents_park_index().read_entry(_CHAIN)
        assert entry is not None
        assert agents_park_index().tombstone_kind(entry) == "detached"

    asyncio.run(go())


def test_detaching_never_overwrites_a_live_park(fake_park_redis: Any) -> None:
    async def go() -> None:
        # A key that DOES hold a park (a concurrent re-drive that reached the persist first) is
        # left exactly as it is — the detach is written NX, so it can only fill an empty slot.
        await persist_park(_park_identity(None), [("int1", {_CHAIN: None})])
        await agents_park_index().detach([_CHAIN])
        entry = await agents_park_index().read_entry(_CHAIN)
        assert entry is not None
        assert agents_park_index().tombstone_kind(entry) == "live"
        assert entry["agent_name"] == "langchain_deep_agent"

    asyncio.run(go())


def test_park_persist_records_multiple_parks_as_one_superstep(fake_park_redis: Any) -> None:
    async def go() -> None:
        # Two distinct park interrupts (parallel subagent parks) persist into ONE super-step:
        # each entry carries ITS interaction's own interrupt, both share the super-step id, and
        # the barrier covers the union.
        # A keep-forever (None) bound lets the None-expiry parks pass the retention gate.
        parks = [("intA", {"iA": None}), ("intB", {"iB": None})]
        await persist_park(_park_identity(None), parks)
        entry_a = await agents_park_index().read_entry("iA")
        entry_b = await agents_park_index().read_entry("iB")
        assert entry_a is not None
        assert entry_b is not None
        assert entry_a["interrupt_id"] == "intA"
        assert entry_b["interrupt_id"] == "intB"
        assert entry_a["superstep_id"] == entry_b["superstep_id"]
        step = superstep_id(["iA", "iB"])
        assert entry_a["superstep_id"] == step
        barrier = await agents_park_index().read_barrier("t-gate", step)
        assert barrier is not None
        assert set(barrier.expected) == {"iA", "iB"}

    asyncio.run(go())


def test_park_persist_allows_any_expiry_under_postgres_keep_forever(fake_park_redis: Any) -> None:
    async def go() -> None:
        # A keep-forever run (postgres checkpoint) computes a ``None`` retention bound, so any
        # deadline passes — the generalized gate reads the bound off the identity, not a provider.
        interactions: dict[str, Any] = {"i1": _iso_in(10_000_000), "i2": None}
        await persist_park(_park_identity(None), [("int1", interactions)])
        assert await agents_park_index().read_entry("i1") is not None
        assert await agents_park_index().read_entry("i2") is not None

    asyncio.run(go())


def test_park_persist_is_atomic_all_or_nothing(fake_park_redis: Any) -> None:
    async def go() -> None:
        parks = [("int1", {"iA": None, "iB": None})]

        # A crash before the single EXEC flushes: every entry and the barrier are buffered in
        # the pipeline, never written incrementally, so the post-state is ABSENT — no partial set.
        real_pipeline = fake_park_redis.pipeline

        def crashing_pipeline(*args: Any, **kwargs: Any) -> Any:
            pipe = real_pipeline(*args, **kwargs)

            async def boom() -> None:
                raise RuntimeError("crash before EXEC")

            pipe.execute = boom
            return pipe

        index = agents_park_index()
        step = superstep_id(["iA", "iB"])
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(fake_park_redis, "pipeline", crashing_pipeline)
            with pytest.raises(RuntimeError, match="crash before EXEC"):
                await persist_park(_park_identity(None), parks)
        assert await index.read_entry("iA") is None
        assert await index.read_entry("iB") is None
        assert await index.read_barrier("t-gate", step) is None

        # A clean persist flushes the whole set in one EXEC: every entry AND the barrier land.
        await persist_park(_park_identity(None), parks)
        assert await index.read_entry("iA") is not None
        assert await index.read_entry("iB") is not None
        barrier = await index.read_barrier("t-gate", step)
        assert barrier is not None
        assert set(barrier.expected) == {"iA", "iB"}

    asyncio.run(go())


def test_park_entry_ttl_scales_to_the_ask_deadline(fake_park_redis: Any) -> None:
    async def go() -> None:
        # Keep-forever retention (postgres) lets a far-future deadline persist. The entry TTL must
        # scale to that deadline, not TTL out at the 30-day floor while the barrier and checkpoint
        # survive to it — else a valid in-window answer would find no entry and storm to give-up.
        far = (datetime.now(UTC) + timedelta(days=40)).isoformat()
        near = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        await persist_park(_park_identity(None), [("int1", {"i_far": far, "i_near": near})])

        index = agents_park_index()
        far_ttl = await fake_park_redis.ttl(index.entry_key("i_far"))
        near_ttl = await fake_park_redis.ttl(index.entry_key("i_near"))
        # The far entry outlasts its 40-day deadline plus the margin — above the 30-day floor.
        assert far_ttl > ENTRY_TTL_FLOOR_SECONDS
        assert far_ttl >= 40 * 24 * 60 * 60 + BARRIER_TTL_MARGIN_SECONDS - 5
        # A short-horizon entry keeps the 30-day backstop floor.
        assert near_ttl == ENTRY_TTL_FLOOR_SECONDS
        # The barrier floors at or above every entry it coordinates.
        barrier_ttl = await fake_park_redis.ttl(index.barrier_key("t-gate", superstep_id(["i_far", "i_near"])))
        assert barrier_ttl >= far_ttl

    asyncio.run(go())


def _is_resolved(entry: Any) -> bool:
    return agents_park_index().tombstone_kind(entry) == "resolved"
