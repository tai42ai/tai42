"""Chained and nested ``tools_agent`` parks: a nested run's park chains and resumes
with its terminal, horizon inheritance and capping, re-park extension, and the
terminal-handling edge cases.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from tai42_contract.agent.base import PresetSpec
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_REPARKED,
    PARK_COMPLETION_SUCCEEDED,
    chained_park_context,
    is_chained_park_key,
)
from tai42_contract.template import TemplatedText
from tests._tools_agent_park_support import (
    ScriptedChatModel,
    _agent,
    _NestedDriverStandIn,
    _preset_call,
    _tool_messages,
    _wire_tools_build,
)

from tai42_agents._internal.park import capability as park_capability
from tai42_agents._internal.park import deliver_chained_park
from tai42_agents._internal.park import index as idx
from tai42_agents._internal.park import persist as park_persist
from tai42_agents._internal.park.errors import AgentResumeParkEntryNotFoundError


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    """Route the park index at a shared in-memory fakeredis and report the park Redis as
    configured (so a run is judged park-capable)."""
    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def fake_park_client() -> AsyncIterator[Any]:
        yield redis

    settings = SimpleNamespace(redis_url="redis://fake")
    monkeypatch.setattr(idx, "_park_client", fake_park_client)
    monkeypatch.setattr(idx, "agents_park_redis_settings", lambda: settings)
    monkeypatch.setattr(park_capability, "agents_park_redis_settings", lambda: settings)
    return redis


@pytest.mark.parametrize(
    "resume_owner",
    [
        # A nested DRIVER (a nested-driver preset) binds its own continuation for the ask.
        "nested_driver_resume",
        # A nested agent RUN parked at its tool face: the receipt names no adoptable owner.
        None,
    ],
    ids=[
        "a nested driver raised under a different run's resume binding",
        "a nested run's tool face, no adoptable owner",
    ],
)
def test_tools_agent_chains_a_nested_runs_park_and_resumes_with_its_terminal(
    fake_park_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
    app_tools: Any,
    resume_owner: str | None,
) -> None:
    # THE HEADLINE. A tool that drives a NESTED run which async-parks hands back a park the
    # platform resumes on that run's own path — ``agent_resume`` is never fired for it. The turn
    # neither adopts it nor gives up on it: it parks on the CALL, and when the nested run finally
    # reaches its terminal, the chain's delivery tool re-enters the loop with that terminal as
    # the tool's result and the turn finishes on it.
    saver = InMemorySaver()
    nested = _NestedDriverStandIn("i-nested", resume_owner)
    model = ScriptedChatModel([_preset_call(), AIMessage(content="the call approved it")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        receipt = await agent.run(
            presets=[preset],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-chained",
        )
        assert isinstance(receipt, dict)
        assert receipt["status"] == "suspended"
        # Parked on the CALL, not on the nested run's interaction: that park stays untouched and
        # resumes on its own path.
        (chain_token,) = receipt["interaction_ids"]
        assert is_chained_park_key(chain_token)
        assert nested.calls == 1
        assert await idx.read_park_entry("i-nested") is None
        entry = await idx.read_park_entry(chain_token)
        assert entry is not None
        assert entry["agent_name"] == "tools_agent"

        # The nested run reaches its terminal out of band and fires the chain's delivery tool
        # with the contract-generic completion payload.
        assert (
            await deliver_chained_park(
                chain_token=chain_token,
                result={"decision": "approved"},
                completion_id="c-1",
                status=PARK_COMPLETION_SUCCEEDED,
            )
            == "the call approved it"
        )
        # The model read the nested run's terminal AS the tool's result — a success result, not
        # an error — and answered on it.
        results = _tool_messages(model.seen[1])
        assert len(results) == 1
        assert results[0].status == "success"
        assert "approved" in results[0].content
        # The chained park finalizes like any other: a resolved tombstone, never an absent key.
        entry = await idx.read_park_entry(chain_token)
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

    asyncio.run(go())


def test_two_chained_calls_in_one_super_step_resume_together(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # Two parking calls in ONE super-step are two CALLS, so two chained keys — never one park
    # both terminals would land on. They converge on the same barrier the platform's own
    # siblings use: the first terminal buffers, and only the second drives the turn on.
    saver = InMemorySaver()
    first = _NestedDriverStandIn("i-a", "nested_driver_resume")
    second = _NestedDriverStandIn("i-b", "nested_driver_resume")
    model = ScriptedChatModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "c1", "name": "driver_a", "args": {}},
                    {"id": "c2", "name": "driver_b", "args": {}},
                ],
            ),
            AIMessage(content="both calls are in"),
        ]
    )
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["a"] = first.base_tool()
    app_tools.client_tools["b"] = second.base_tool()
    app_tools.tool_runners["a"] = first.run
    app_tools.tool_runners["b"] = second.run

    agent = _agent()
    presets = [
        PresetSpec(name="driver_a", base_tool="a", description="Run driver a.", fixed_kwargs={}),
        PresetSpec(name="driver_b", base_tool="b", description="Run driver b.", fixed_kwargs={}),
    ]

    async def go() -> None:
        receipt = await agent.run(
            presets=presets, checkpoint_provider="redis", user_message=TemplatedText(content="go"), thread_id="t-two"
        )
        keys = receipt["interaction_ids"]
        assert len(set(keys)) == 2
        assert set(keys) == {first.chained_keys[0], second.chained_keys[0]}

        buffered = await deliver_chained_park(
            chain_token=keys[0], result="a done", completion_id="c-a", status=PARK_COMPLETION_SUCCEEDED
        )
        assert buffered == {"status": "buffered", "remaining": 1}
        assert (
            await deliver_chained_park(
                chain_token=keys[1], result="b done", completion_id="c-b", status=PARK_COMPLETION_SUCCEEDED
            )
            == "both calls are in"
        )
        # Each call's own terminal became its own tool result.
        results = sorted(message.content for message in _tool_messages(model.seen[1]))
        assert results == ["a done", "b done"]

    asyncio.run(go())


def test_the_delivery_tool_accepts_the_whole_contract_fire_payload(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # What a nested driver actually fires is the BOUND CONTEXT merged with the terminal —
    # every field of it, not just the ones this tool reads. The context a chained dispatch
    # composes carries the embedded caller binding and (under a tool route) the reserved thread
    # field, so a fire carrying them must not be a signature error: that failure mode is a turn
    # that parks, resumes nowhere, and is retried until it is dropped.
    saver = InMemorySaver()
    nested = _NestedDriverStandIn("i-nested", "nested_driver_resume")
    model = ScriptedChatModel([_preset_call(), AIMessage(content="the call approved it")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        receipt = await agent.run(
            presets=[preset],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-payload",
        )
        (chain_token,) = receipt["interaction_ids"]
        fire = {
            **chained_park_context(chain_token, ("deliver_tool_completion", {"delivery_thread_id": "bridge:r:a"})),
            "result": "the call said yes",
            "completion_id": "c-1",
            "status": PARK_COMPLETION_SUCCEEDED,
        }
        assert await deliver_chained_park(**fire) == "the call approved it"

    asyncio.run(go())


def test_a_chained_park_inherits_the_nested_asks_horizon(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The caller's suspension should last exactly as long as the thing it waits on, so the
    # chained park's deadline is INHERITED from the ask the nested run is parked on.
    saver = InMemorySaver()
    deadline = datetime.now(UTC) + timedelta(hours=2)
    nested = _NestedDriverStandIn("i-nested", "nested_driver_resume", expiry_at=deadline)
    model = ScriptedChatModel([_preset_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        receipt = await agent.run(
            presets=[preset],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-horizon",
        )
        assert receipt["expiry_at"] == deadline.isoformat()

    asyncio.run(go())


def test_a_chained_park_horizon_is_capped(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # Inheritance is not blind trust: a chained park has no ask of its own and nothing fires at
    # its deadline, so a nested run asking for an answer years away cannot pin a suspended agent
    # open that long. The cap is what bounds it, and a nested run carrying NO deadline takes the
    # cap too — a chained park is never unbounded.
    saver = InMemorySaver()
    monkeypatch.setattr(
        park_persist, "agents_limits_settings", lambda: SimpleNamespace(chained_park_horizon_cap_hours=1)
    )
    far = datetime.now(UTC) + timedelta(days=365)
    nested = _NestedDriverStandIn("i-nested", "nested_driver_resume", expiry_at=far)
    model = ScriptedChatModel([_preset_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        receipt = await agent.run(
            presets=[preset], checkpoint_provider="redis", user_message=TemplatedText(content="go"), thread_id="t-cap"
        )
        capped = datetime.fromisoformat(receipt["expiry_at"])
        assert capped < far
        assert capped <= datetime.now(UTC) + timedelta(hours=1)

    asyncio.run(go())


def test_a_chained_park_horizon_reads_a_naive_inherited_deadline_as_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    # An inherited deadline crosses the delivery boundary (deliver_chained_park's expiry_at) as an
    # ISO string. Internal producers stamp UTC, but a malformed external fire could omit the offset,
    # and a naive value compared against the tz-aware clamp raises TypeError. It must be read as UTC
    # instead: the horizon only sizes how long the index holds the park (nothing fires AT it), so
    # the "clamped, never refused" contract holds and a cosmetically-off deadline never becomes an
    # endless redelivery of the re-park notice.
    monkeypatch.setattr(
        park_persist, "agents_limits_settings", lambda: SimpleNamespace(chained_park_horizon_cap_hours=24)
    )
    naive = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None)
    horizon = park_persist.chained_park_horizon(naive.isoformat(), None)
    parsed = datetime.fromisoformat(horizon)
    assert parsed.tzinfo is not None
    # Read as UTC and, being inside the cap, kept as the inherited deadline.
    assert parsed == naive.replace(tzinfo=UTC)


def test_a_re_park_extends_the_chained_parks_inherited_horizon(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The nested run answered its first ask and asked another one, further out. The waiting
    # caller's inherited horizon moves with it — the platform fires the re-park notice at the
    # chained binding, and the delivery tool extends the park's keys rather than resuming it.
    saver = InMemorySaver()
    monkeypatch.setattr(
        park_persist, "agents_limits_settings", lambda: SimpleNamespace(chained_park_horizon_cap_hours=24 * 365)
    )
    nested = _NestedDriverStandIn("i-nested", "nested_driver_resume", expiry_at=datetime.now(UTC) + timedelta(hours=1))
    model = ScriptedChatModel([_preset_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        receipt = await agent.run(
            presets=[preset],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-repark",
        )
        (chain_token,) = receipt["interaction_ids"]
        entry = await idx.read_park_entry(chain_token)
        assert entry is not None
        before = await fake_park_redis.ttl(f"agent:park:{chain_token}")

        # Past the park index's own backstop floor, so the extension is what sets the TTL.
        later = datetime.now(UTC) + timedelta(days=60)
        extended = await deliver_chained_park(
            chain_token=chain_token, status=PARK_COMPLETION_REPARKED, expiry_at=later.isoformat()
        )
        assert extended["status"] == "extended"
        # The park is still parked — a re-park notice resolves nothing — and now outlives the
        # NEW deadline. Its barrier moves with it, so the answer still finds both.
        assert await fake_park_redis.ttl(f"agent:park:{chain_token}") > before
        assert await fake_park_redis.ttl(f"agent:park:step:t-repark:{entry['superstep_id']}") > before
        assert not idx.is_resolved_tombstone(await idx.read_park_entry(chain_token) or {})

    asyncio.run(go())


def test_a_re_park_notice_for_an_unparked_chain_is_benign(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The FIRST park of a chained call always notifies before the waiting run has recorded its
    # own park (the nested run parks first, by construction), so a notice with nothing to extend
    # is the normal case, not an error.
    async def go() -> None:
        assert await deliver_chained_park(
            chain_token="tai42:chained-park:never-parked",
            status=PARK_COMPLETION_REPARKED,
            expiry_at=datetime.now(UTC).isoformat(),
        ) == {"status": "not_parked"}

    asyncio.run(go())


@pytest.mark.parametrize(
    ("status", "announced"),
    [
        # The explicit non-success terminal.
        (PARK_COMPLETION_FAILED, "non-success terminal"),
        # An UNSTAMPED fire: a driver that predates the status field, or one that omits it.
        (None, "carried NO status"),
        # A value outside the shared vocabulary — a driver/delivery version skew.
        ("mystery", "unrecognized status"),
    ],
)
def test_a_failed_nested_terminal_re_enters_as_a_model_visible_tool_error(
    fake_park_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
    app_tools: Any,
    caplog: pytest.LogCaptureFixture,
    status: str | None,
    announced: str,
) -> None:
    # The nested run ended without a result. The waiting turn is resumed either way — never left
    # parked — and what it is resumed WITH is a tool ERROR the model reads, never a silent empty
    # result and never a failure payload dressed as the answer.
    saver = InMemorySaver()
    nested = _NestedDriverStandIn("i-nested", "nested_driver_resume")
    model = ScriptedChatModel([_preset_call(), AIMessage(content="I could not run that call")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        receipt = await agent.run(
            presets=[preset],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-failed",
        )
        (chain_token,) = receipt["interaction_ids"]
        with caplog.at_level(logging.WARNING):
            assert (
                await deliver_chained_park(chain_token=chain_token, completion_id="c-1", status=status)
                == "I could not run that call"
            )
        errors = _tool_messages(model.seen[1])
        assert len(errors) == 1
        assert errors[0].status == "error"
        assert "ended without a result" in errors[0].content
        # Every non-success shape is ANNOUNCED naming WHICH arrived: a fire this tool cannot read
        # still resumes the run, so the log is the only detection a version skew has.
        assert announced in caplog.text

    asyncio.run(go())


def test_a_terminal_for_a_chain_the_drive_never_parked_on_lands_benignly(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The turn claimed a chained call and then DIED without parking on it — here the drive raises
    # after the nested run parked. The nested run will still fire its terminal at that key, so
    # the drive detaches it on the way out: the late fire lands on the benign already-resolved
    # path instead of hunting a park that will never exist.
    saver = InMemorySaver()
    nested = _NestedDriverStandIn("i-nested", "nested_driver_resume")
    model = ScriptedChatModel([_preset_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    async def _persist_died(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the drive died")

    monkeypatch.setattr(park_persist, "persist_superstep", _persist_died)

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        with pytest.raises(Exception, match="the drive died"):
            await agent.run(
                presets=[preset],
                checkpoint_provider="redis",
                user_message=TemplatedText(content="go"),
                thread_id="t-dead",
            )
        (chain_token,) = nested.chained_keys
        entry = await idx.read_park_entry(chain_token)
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)
        # A late terminal reads the tombstone and clears, instead of raising into an endless
        # redelivery against an absent key.
        assert await deliver_chained_park(
            chain_token=chain_token, result="too late", completion_id="c-1", status=PARK_COMPLETION_SUCCEEDED
        ) == {"status": "already_resolved"}

    asyncio.run(go())


def test_a_terminal_for_a_chain_still_being_recorded_keeps_its_retry(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The other side of the same coin: an ABSENT entry is not proof the caller is gone — it can
    # be a terminal that beat the waiting run's own park-persist. Raising keeps the platform's
    # at-least-once ticket so the redelivery lands, instead of dropping the answer.
    async def go() -> None:
        with pytest.raises(AgentResumeParkEntryNotFoundError):
            await deliver_chained_park(
                chain_token="tai42:chained-park:not-yet",
                result="the answer",
                completion_id="c-1",
                status=PARK_COMPLETION_SUCCEEDED,
            )

    asyncio.run(go())


def test_a_terminal_with_no_chain_token_is_dropped_loudly(caplog: pytest.LogCaptureFixture) -> None:
    # Unroutable: it names no waiting run and no retry could ever land it, so it is dropped with
    # an error rather than raising into an endless redelivery.
    async def go() -> None:
        with caplog.at_level(logging.ERROR):
            assert await deliver_chained_park(
                chain_token=None, result="orphan", completion_id="c-9", status=PARK_COMPLETION_SUCCEEDED
            ) == {"status": "dropped"}
        assert "no chain_token" in caplog.text

    asyncio.run(go())


def test_an_unchained_run_still_refuses_a_nested_runs_park(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # THE FALLBACK, unchanged. A run that cannot park binds no resume continuation, so its
    # dispatches chain nothing — there would be nothing to deliver a nested terminal to. The
    # nested run's park is refused to the MODEL as a tool error, the turn finishes on the model's
    # own next message, and this run records no park state at all.
    saver = InMemorySaver()
    nested = _NestedDriverStandIn("i-nested", "nested_driver_resume")
    model = ScriptedChatModel([_preset_call(), AIMessage(content="that call needs its own route")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        # The astream face with NO completion bound binds no resume path at all.
        events = [
            event
            async for event in agent.astream(
                presets=[preset],
                checkpoint_provider="redis",
                user_message=TemplatedText(content="go"),
                thread_id="t-unchained",
            )
        ]
        assert events
        assert nested.calls == 1
        refusals = _tool_messages(model.seen[1])
        assert len(refusals) == 1
        assert refusals[0].status == "error"
        # The refusal names the door that was shut — nothing chained this call — but stays
        # store-name-free: it never echoes the expected owner (a public continuation name would
        # hand the model the exact string to name for a claim). Both properties compose here.
        assert "raised under a different run's resume binding" in refusals[0].content
        assert "nothing chained this call" in refusals[0].content
        assert "agent_resume" not in refusals[0].content
        assert "'nested_driver_resume'" not in refusals[0].content
        # Nothing of THIS run is parked or orphaned, and no chained key was ever minted.
        assert await idx.read_park_entry("i-nested") is None
        assert nested.chained_keys == []

    asyncio.run(go())
