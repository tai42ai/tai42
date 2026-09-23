"""The driver contract of the agents resume face: the outcome conversion table, the two faces'
resume authorisation, the whole-chain kill teardown, and the horizon-derived resolution TTL.

The park index is routed at an in-memory fakeredis; the bound recording app supplies the platform
facets the driver calls (``assert_resume_authorized`` / ``redelivery_horizon_seconds`` and
``run_tool`` for a cross-driver chain fire).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from tai42_contract.interactions import (
    ParkResumeFailed,
    ParkResumeUnauthorizedError,
    ResumeBuffered,
    SuspendedInteraction,
)

from tai42_agents._internal.park import capability as cap
from tai42_agents._internal.park import index as idx
from tai42_agents._internal.park import resume as res
from tai42_agents._internal.park.chain import deliver_chained_park
from tai42_agents._internal.park.errors import AgentSuperstepLeaseLostError, ParkKillNotReadyError
from tai42_agents._internal.park.resume import (
    agent_resume,
    encode_outcome,
    to_contract_outcome,
)
from tai42_agents._internal.park.resume_tool import agent_park_kill_handler, agent_resume_tool


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def fake_park_client() -> AsyncIterator[Any]:
        yield redis

    settings = SimpleNamespace(redis_url="redis://fake")
    monkeypatch.setattr(idx, "_park_client", fake_park_client)
    monkeypatch.setattr(idx, "agents_park_redis_settings", lambda: settings)
    monkeypatch.setattr(cap, "agents_park_redis_settings", lambda: settings)
    return redis


async def _write_park(
    interaction_ids: list[str],
    *,
    thread_id: str = "t",
    completion_tool: str | None = None,
    completion_context: dict[str, Any] | None = None,
) -> str:
    superstep_id = idx.compute_superstep_id(interaction_ids)
    entries = {
        iid: {
            "agent_name": "tools_agent",
            "thread_id": thread_id,
            "superstep_id": superstep_id,
            "interrupt_id": "int1",
            "rebuild_kwargs": {"checkpoint_provider": "redis", "recursion_limit": 50},
            "completion_tool": completion_tool,
            "completion_context": completion_context,
        }
        for iid in interaction_ids
    }
    expected: dict[str, Any] = dict.fromkeys(interaction_ids)
    await idx.persist_superstep(entries, thread_id, superstep_id, expected, dict.fromkeys(interaction_ids), 100)
    return superstep_id


async def _seed_finalized(thread_id: str, superstep_id: str, ids: list[str], *, resolution: str, value: Any) -> None:
    """Claim the drive lease and finalize the super-step under it — the token guard the production
    drive and kill both satisfy, so a test seeding a tombstone holds a lease exactly as they do."""
    token = "seed-token"
    assert await idx.try_claim_drive(thread_id, superstep_id, token)
    await idx.finalize_resolved_superstep(thread_id, superstep_id, ids, resolution=resolution, value=value, token=token)


# ---- the outcome conversion table (one per row) -------------------------------------------


def test_conversion_buffered_becomes_resume_buffered() -> None:
    out = to_contract_outcome({"status": "buffered", "remaining_ids": ["iB", "iC"]})
    assert isinstance(out, ResumeBuffered)
    assert out.remaining_ids == ["iB", "iC"]


def test_conversion_suspended_receipt_becomes_a_sentinel_with_both_id_lists() -> None:
    receipt = {
        "status": "suspended",
        "interaction_ids": ["i1", "i2"],
        "caller_interaction_ids": ["i2"],
        "expiry_at": None,
    }
    out = to_contract_outcome(receipt)
    assert isinstance(out, SuspendedInteraction)
    assert out.interaction_id == "i1"
    assert out.interaction_ids == ["i1", "i2"]
    assert out.caller_interaction_ids == ["i2"]
    # A re-park sentinel names no resume owner — the platform re-normalises it, never adopts it.
    assert out.resume_owner is None


def test_conversion_a_clean_terminal_value_passes_through() -> None:
    assert to_contract_outcome("the final answer") == "the final answer"
    assert to_contract_outcome({"decision": "approved"}) == {"decision": "approved"}


def test_conversion_passes_an_already_contract_typed_value_through() -> None:
    # A value that rode UP a cross-driver chain fire is already a contract type; convert it again
    # would be wrong, so it passes through unchanged.
    sentinel = SuspendedInteraction(interaction_id="i9", interaction_ids=["i9"], caller_interaction_ids=[])
    assert to_contract_outcome(sentinel) is sentinel
    buffered = ResumeBuffered(remaining_ids=["i9"])
    assert to_contract_outcome(buffered) is buffered


def test_conversion_a_none_no_op_landing_passes_through() -> None:
    assert to_contract_outcome(None) is None


# ---- resume authorisation at both faces -----------------------------------------------------


def test_agent_resume_tool_refuses_an_unauthorised_caller(fake_park_redis: Any, app_interactions: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(["i1"])
        # A resolved super-step exists, so a leak past the assert would disclose an outcome; the
        # assert refuses BEFORE any park state is read.
        await _seed_finalized("t", superstep_id, ["i1"], resolution="terminal", value=encode_outcome("secret"))
        app_interactions.resume_authorized = False
        with pytest.raises(ParkResumeUnauthorizedError):
            await agent_resume_tool("i1", "x")
        # The face asserted with the id it was about to resume, before touching the index.
        assert app_interactions.resume_auth_calls == ["i1"]

    asyncio.run(go())


def test_deliver_chained_park_refuses_an_unauthorised_caller(fake_park_redis: Any, app_interactions: Any) -> None:
    async def go() -> None:
        app_interactions.resume_authorized = False
        with pytest.raises(ParkResumeUnauthorizedError):
            await deliver_chained_park(chain_token="tai42:chained-park:x", result="y")
        assert app_interactions.resume_auth_calls == ["tai42:chained-park:x"]

    asyncio.run(go())


def test_agent_resume_tool_authorised_returns_a_contract_typed_outcome(fake_park_redis: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(["iA", "iB"])
        # Authorised (default): a buffered resume returns a contract ``ResumeBuffered``.
        out = await agent_resume_tool("iA", "answer-a")
        assert isinstance(out, ResumeBuffered)
        assert out.remaining_ids == ["iB"]
        assert superstep_id  # the barrier is still live

    asyncio.run(go())


# ---- the whole-chain kill teardown -------------------------------------------------------


def test_kill_handler_is_a_noop_for_an_interaction_it_never_parked(fake_park_redis: Any) -> None:
    async def go() -> None:
        # No entry: another driver owns it, so the handler does nothing (and never raises).
        await agent_park_kill_handler("not-ours", "cancelled")

    asyncio.run(go())


def test_kill_handler_is_a_noop_when_the_park_index_is_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    # The handler is registered globally and fires for EVERY driver's kill. A deployment that loaded
    # the agents plugin but configured no durable park index owns no parks: the handler answers "not
    # ours" WITHOUT reading the index, so a kill another driver owns is not crashed by the
    # unconfigured-redis raise an index read would otherwise throw.
    monkeypatch.setattr(cap, "agents_park_redis_settings", lambda: SimpleNamespace(redis_url=None))

    @contextlib.asynccontextmanager
    async def _forbidden() -> AsyncIterator[Any]:
        raise AssertionError("the kill handler read the park index though it is unconfigured")
        yield  # pragma: no cover — keeps this a generator; the raise fires on first entry

    monkeypatch.setattr(idx, "_park_client", _forbidden)

    asyncio.run(agent_park_kill_handler("not-ours", "cancelled"))


def test_kill_handler_drops_a_resolved_super_steps_stored_outcome(fake_park_redis: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(["i1"])
        await _seed_finalized("t", superstep_id, ["i1"], resolution="terminal", value=encode_outcome("held outcome"))
        assert await idx.read_superstep_resolution("t", superstep_id) is not None
        # A person erase reaches the killed interaction: its stored outcome (which can hold person
        # data) and this tombstone are dropped.
        await agent_park_kill_handler("i1", "erased")
        assert await idx.read_superstep_resolution("t", superstep_id) is None
        assert await idx.read_park_entry("i1") is None

    asyncio.run(go())


def test_kill_handler_aborts_a_live_park_and_fires_the_chain_upward(fake_park_redis: Any, app_tools: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(
            ["i1"],
            completion_tool="deliver_ancestor_chain",
            completion_context={"chain_key": "tai42:chained-park:anc", "asked_by": ["caller"]},
        )
        fired: list[dict[str, Any]] = []

        def deliver_ancestor_chain(**kwargs: Any) -> None:
            fired.append(kwargs)

        app_tools.tool_runners["deliver_ancestor_chain"] = deliver_ancestor_chain

        await agent_park_kill_handler("i1", "thread deleted")

        # The captured cross-driver chain was fired FAILED, carrying the ancestor's chain and the
        # aborted outcome, so an ancestor that waited on this run tears down too.
        assert len(fired) == 1
        assert fired[0]["chain_token"] == "tai42:chained-park:anc"
        assert fired[0]["status"] == "failed"
        assert fired[0]["result"] == {"status": "aborted", "reason": "thread deleted"}
        (call,) = app_tools.run_tool_calls
        assert call["key"] == "deliver_ancestor_chain"
        assert call["continues_chain"] == ("caller",)

        # The super-step is finalized ``aborted``: a redrive of any still-open sibling due-record
        # RAISES ParkResumeFailed (deduped against the kill's own FAILED).
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)
        record = await idx.read_superstep_resolution("t", superstep_id)
        assert record is not None
        assert record["resolution"] == "aborted"

    asyncio.run(go())


def test_kill_handler_chain_fire_propagates_so_the_kill_redelivers(fake_park_redis: Any, app_tools: Any) -> None:
    async def go() -> None:
        await _write_park(
            ["i1"],
            completion_tool="deliver_ancestor_chain",
            completion_context={"chain_key": "tai42:chained-park:anc", "asked_by": []},
        )

        def deliver_ancestor_chain(**_kwargs: Any) -> None:
            raise RuntimeError("ancestor unreachable")

        app_tools.tool_runners["deliver_ancestor_chain"] = deliver_ancestor_chain

        # The cross-driver teardown fire is NOT best-effort: its failure PROPAGATES so kill_park
        # keeps the kill-due record and the reaper redelivers.
        with pytest.raises(RuntimeError, match="ancestor unreachable"):
            await agent_park_kill_handler("i1", "cancelled")
        # The super-step is NOT finalized before the fire lands, so a redelivery re-drives.
        assert not idx.is_resolved_tombstone(await idx.read_park_entry("i1") or {})

    asyncio.run(go())


# ---- the horizon-derived resolution TTL --------------------------------------------------


def test_resolution_ttl_is_twice_the_platform_redelivery_horizon(fake_park_redis: Any, app_interactions: Any) -> None:
    async def go() -> None:
        app_interactions.redelivery_horizon = 1000
        superstep_id = await _write_park(["i1"])
        await _seed_finalized("t", superstep_id, ["i1"], resolution="terminal", value=encode_outcome("v"))
        # The resolution record and its tombstone hold the run's outcome, so their TTL is derived
        # from the platform horizon: 2x 1000 = 2000, never a hard-coded guess.
        assert await fake_park_redis.ttl(idx._resolution_key("t", superstep_id)) == 2000
        assert await fake_park_redis.ttl(idx._park_key("i1")) == 2000

    asyncio.run(go())


def test_kill_handler_drops_the_whole_runs_resolution_records(fake_park_redis: Any, app_tools: Any) -> None:
    async def go() -> None:
        # Two PRIOR resolved super-steps of one run (thread "t"), each holding a delivered outcome,
        # plus a live park being killed.
        ss1 = await _write_park(["p1"])
        await _seed_finalized("t", ss1, ["p1"], resolution="terminal", value=encode_outcome("out-1"))
        ss2 = await _write_park(["p2"])
        await _seed_finalized("t", ss2, ["p2"], resolution="terminal", value=encode_outcome("out-2"))
        ss_live = await _write_park(["live"])

        await agent_park_kill_handler("live", "person erased")

        # Both prior super-steps' records, their tombstones (the interaction keys), and their run
        # index entries are gone — the delivered outcomes (which can hold person data) are erased.
        for ss, pid in ((ss1, "p1"), (ss2, "p2")):
            assert await idx.read_superstep_resolution("t", ss) is None
            assert await idx.read_park_entry(pid) is None
        # The killed super-step: its live park entry is gone (an aborted tombstone), its drive claim
        # (the kill's own, held while it finalized) is released, and a redrive RAISES ParkResumeFailed.
        assert idx.is_resolved_tombstone(await idx.read_park_entry("live") or {})
        assert await fake_park_redis.get(idx._claim_key("t", ss_live)) is None
        live_record = await idx.read_superstep_resolution("t", ss_live)
        assert live_record is not None
        assert live_record["resolution"] == "aborted"
        # The run index now holds only the killed super-step.
        assert set(await idx.read_run_resolutions("t")) == {ss_live}

        # A second kill of the same id is idempotent — it drops the aborted super-step and no-ops.
        await agent_park_kill_handler("live", "person erased")
        assert await idx.read_run_resolutions("t") == {}

    asyncio.run(go())


def test_run_resolution_index_expires_with_the_records(fake_park_redis: Any, app_interactions: Any) -> None:
    async def go() -> None:
        app_interactions.redelivery_horizon = 500
        ss = await _write_park(["p1"])
        await _seed_finalized("t", ss, ["p1"], resolution="terminal", value=encode_outcome("v"))
        # The run index shares the resolution TTL (2x the horizon), so it outlives the records it
        # points at and no longer.
        assert await fake_park_redis.ttl(idx._run_resolution_index_key("t")) == 1000

    asyncio.run(go())


def test_kill_handler_aborted_tombstone_replays_as_park_resume_failed(fake_park_redis: Any, app_tools: Any) -> None:
    async def go() -> None:
        await _write_park(["i1"])
        await agent_park_kill_handler("i1", "cancelled")
        # A redrive of the killed super-step's due-record RAISES ParkResumeFailed carrying the
        # aborted outcome, so the platform delivers FAILED (once, deduped against the kill).
        with pytest.raises(ParkResumeFailed) as exc:
            await agent_resume_tool("i1", "late")
        assert exc.value.outcome == {"status": "aborted", "reason": "cancelled"}

    asyncio.run(go())


# ---- the kill coordinates with the drive lease (a whole-chain kill never stomps a live drive) ----


def test_kill_handler_defers_while_a_live_drive_holds_the_lease(fake_park_redis: Any, app_tools: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(
            ["i1"],
            completion_tool="deliver_ancestor_chain",
            completion_context={"chain_key": "tai42:chained-park:anc", "asked_by": ["caller"]},
        )
        app_tools.tool_runners["deliver_ancestor_chain"] = lambda **_kwargs: None
        # A live resume drive holds the super-step's drive lease.
        assert await idx.try_claim_drive("t", superstep_id, "live-drive")

        # The kill cannot claim the lease, so it defers (redelivers) rather than stomping the drive.
        with pytest.raises(ParkKillNotReadyError):
            await agent_park_kill_handler("i1", "thread deleted")

        # It wrote NOTHING: no chain fire, the live drive's lease is untouched, the park entry stays
        # live (not a tombstone), the barrier stands, and no resolution record exists.
        assert app_tools.run_tool_calls == []
        assert await fake_park_redis.get(idx._claim_key("t", superstep_id)) == "live-drive"
        assert not idx.is_resolved_tombstone(await idx.read_park_entry("i1") or {})
        assert await idx.read_barrier("t", superstep_id) is not None
        assert await idx.read_superstep_resolution("t", superstep_id) is None

    asyncio.run(go())


def test_kill_handler_finalizes_aborted_after_the_drive_released_its_lease(
    fake_park_redis: Any, app_tools: Any
) -> None:
    async def go() -> None:
        superstep_id = await _write_park(["i1"])
        # A drive claimed the lease, then released it (a caught drive failure / a crash whose TTL
        # lapsed): the super-step is free again.
        assert await idx.try_claim_drive("t", superstep_id, "drive")
        await idx.release_claim("t", superstep_id, "drive")

        # The redelivered kill now claims the lease and finalizes the super-step aborted.
        await agent_park_kill_handler("i1", "thread deleted")

        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)
        record = await idx.read_superstep_resolution("t", superstep_id)
        assert record is not None
        assert record["resolution"] == "aborted"
        # The kill released its own lease as part of the finalize.
        assert await fake_park_redis.get(idx._claim_key("t", superstep_id)) is None

    asyncio.run(go())


def test_finalize_refuses_when_the_lease_token_no_longer_holds_the_claim(fake_park_redis: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(["i1"])
        assert await idx.try_claim_drive("t", superstep_id, "drive-token")
        # The drive's lease lapsed and a whole-chain kill reclaimed the super-step under its token.
        await idx.release_claim("t", superstep_id, "drive-token")
        assert await idx.try_claim_drive("t", superstep_id, "kill-token")

        # The drive's terminal finalize under its stale token is refused — it never overwrites the
        # holder's claim, and writes nothing.
        with pytest.raises(AgentSuperstepLeaseLostError):
            await idx.finalize_resolved_superstep(
                "t", superstep_id, ["i1"], resolution="terminal", value=encode_outcome("late"), token="drive-token"
            )
        assert await idx.read_superstep_resolution("t", superstep_id) is None
        assert not idx.is_resolved_tombstone(await idx.read_park_entry("i1") or {})
        assert await fake_park_redis.get(idx._claim_key("t", superstep_id)) == "kill-token"

    asyncio.run(go())


def test_drive_whose_lease_a_kill_took_does_not_fire_its_chain_and_raises(
    fake_park_redis: Any, app_tools: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def go() -> None:
        superstep_id = await _write_park(
            ["i1"],
            completion_tool="deliver_ancestor_chain",
            completion_context={"chain_key": "tai42:chained-park:anc", "asked_by": ["caller"]},
        )
        app_tools.tool_runners["deliver_ancestor_chain"] = lambda **_kwargs: "ancestor result"

        async def drive_then_lose_the_lease(
            entry: Any, thread_id: str, superstep_id_: str, token: str
        ) -> tuple[Any, dict[str, Any]]:
            # The drive produced a clean terminal, but while it ran its lease lapsed and a
            # whole-chain kill reclaimed the super-step and finalized it aborted.
            await idx.release_claim(thread_id, superstep_id_, token)
            assert await idx.try_claim_drive(thread_id, superstep_id_, "kill-token")
            await idx.finalize_resolved_superstep(
                thread_id,
                superstep_id_,
                ["i1"],
                resolution="aborted",
                value=encode_outcome({"status": "aborted", "reason": "thread deleted"}),
                token="kill-token",
            )
            return "leaf terminal", {"i1": None}

        monkeypatch.setattr(res, "_drive_completed_barrier", drive_then_lose_the_lease)

        # The drive observes the lost lease at the re-check before its chain fire: it raises and
        # fires NO chain routing (no SUCCEEDED cascade up to the waiting ancestor).
        with pytest.raises(AgentSuperstepLeaseLostError):
            await agent_resume("i1", "the answer")
        assert app_tools.run_tool_calls == []

        # The kill's aborted resolution stands, unmodified by the drive; a redrive replays it FAILED.
        record = await idx.read_superstep_resolution("t", superstep_id)
        assert record is not None
        assert record["resolution"] == "aborted"
        with pytest.raises(ParkResumeFailed) as exc:
            await agent_resume("i1", "the answer")
        assert exc.value.outcome == {"status": "aborted", "reason": "thread deleted"}

    asyncio.run(go())
