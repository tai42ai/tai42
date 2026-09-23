"""``tools_agent`` park refusals and ``bind_resume_per_step`` context scoping:
non-durable/live-tools/no-completion refusals, marker-ownership guards, and the
per-step binding boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from tai42_contract.agent.events import SuspendedFinal
from tai42_contract.interactions import (
    SUSPENDED_INTERACTION_MARKER_KEY,
    get_resume_continuation_tool,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
    suspended_interaction_marker,
)
from tai42_contract.template import TemplatedText
from tai42_contract.tools import tool_call_frame
from tests._tools_agent_park_support import (
    ScriptedChatModel,
    _agent,
    _ask_call,
    _AskStandIn,
    _park,
    _RawMarkerTool,
    _relay_call,
    _tool_messages,
    _wire_tools_build,
)

from tai42_agents._internal.park import capability as park_capability
from tai42_agents._internal.park import drive as park_drive_mod
from tai42_agents._internal.park import index as idx
from tai42_agents._internal.park.resume import AGENT_RESUME_TOOL_NAME


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


def test_park_continuation_binds_none_when_not_park_capable_shadowing_the_ambient() -> None:
    # A non-park-capable run's drive wrapper binds ``None``, SHADOWING any ambient resume
    # continuation a park-capable caller left bound. Without it, a nested non-capable run inherits
    # the ambient binding, its ask mints a park it can never resume, the claim point adopts it, and
    # the run answers with the raw marker (or the stream ends with no terminal).
    outer = set_resume_continuation_tool(AGENT_RESUME_TOOL_NAME)
    try:
        assert get_resume_continuation_tool() == AGENT_RESUME_TOOL_NAME
        with park_drive_mod.park_continuation(None):
            # The ambient binding is shadowed for the duration of the non-capable drive.
            assert get_resume_continuation_tool() is None
        # And restored when the wrapper exits.
        assert get_resume_continuation_tool() == AGENT_RESUME_TOOL_NAME
    finally:
        reset_resume_continuation_tool(outer)


def test_tools_agent_under_ambient_binding_does_not_answer_with_a_marker_when_not_hostable(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # Probe shape, run face: a memory-checkpoint run is not park-capable, so its drive wrapper
    # binds None even under an ambient resume continuation a park-capable caller left bound. A
    # relayed marker naming that ambient continuation is then REFUSED at the claim point rather than
    # adopted, so the run never answers with the marker JSON.
    saver = InMemorySaver()
    relay = _RawMarkerTool(suspended_interaction_marker("i-relayed", None, AGENT_RESUME_TOOL_NAME))
    model = ScriptedChatModel([_relay_call(), AIMessage(content="cannot host that here")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["relay"] = relay.tool()

    agent = _agent()

    async def go() -> None:
        # Stand in for being nested under a park-capable caller that bound the resume continuation.
        outer = set_resume_continuation_tool(AGENT_RESUME_TOOL_NAME)
        try:
            result = await agent.run(
                tool_names=["relay"],
                checkpoint_provider="memory",
                user_message=TemplatedText(content="go"),
                thread_id="t-f1",
            )
        finally:
            reset_resume_continuation_tool(outer)
        assert isinstance(result, str)
        assert SUSPENDED_INTERACTION_MARKER_KEY not in result
        assert "i-relayed" not in result
        assert result == "cannot host that here"
        # Nothing was parked under this non-hostable run.
        assert await idx.read_park_entry("i-relayed") is None

    asyncio.run(go())


def test_tools_agent_run_refuses_non_durable_checkpoint(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A memory checkpoint is not park-capable: the async ask has no resume path bound, so the
    # ask stand-in RAISES loudly pre-persist rather than parking with nowhere to resume.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        with pytest.raises(Exception, match="resuming driver"):
            await agent.run(
                tool_names=["ask"],
                checkpoint_provider="memory",
                user_message=TemplatedText(content="go"),
                thread_id="t-nondurable",
            )
        assert await idx.read_park_entry("i1") is None

    asyncio.run(go())


def test_tools_agent_run_refuses_live_tools(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A run carrying a LIVE tool is not rebuildable from names, so it is not park-capable:
    # the resume continuation is not bound and the async ask refuses loudly pre-persist.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)

    agent = _agent()

    async def go() -> None:
        with pytest.raises(Exception, match="resuming driver"):
            await agent.run(
                tools=[ask.tool()],
                checkpoint_provider="redis",
                user_message=TemplatedText(content="go"),
                thread_id="t-livetools",
            )
        assert await idx.read_park_entry("i1") is None

    asyncio.run(go())


def test_tools_agent_astream_refuses_async_ask_without_a_run_delivery_context(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A stream driven under NO run-delivery context has no receiver at all, so the astream face
    # binds no resume path and an async ask refuses loudly pre-persist. (No frame is opened here,
    # so no run-delivery id is ambient.)
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        with pytest.raises(Exception, match="resuming driver"):
            async for _event in agent.astream(
                tool_names=["ask"],
                checkpoint_provider="redis",
                user_message=TemplatedText(content="go"),
                thread_id="t-astream",
            ):
                pass
        assert await idx.read_park_entry("i1") is None

    asyncio.run(go())


def test_tools_agent_astream_parks_under_a_run_delivery_context_without_an_address(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # Under a run-delivery context (the door opened the minting call frame) but NO out-of-band
    # completion address, the astream face is park-capable: a live receiver takes the outcome
    # inline, so an async ask PARKS instead of refusing. The park stores no delivery tool.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        # The frame mints a run-delivery id with NO address bound (no ``set_park_completion``).
        with tool_call_frame(name="agent"):
            events = [
                event
                async for event in agent.astream(
                    tool_names=["ask"],
                    checkpoint_provider="redis",
                    user_message=TemplatedText(content="go"),
                    thread_id="t-astream-ctx",
                )
            ]
        # The ask ran exactly once and parked; the stream reached the suspended terminal.
        assert ask.calls == 1
        assert [event for event in events if isinstance(event, SuspendedFinal)], events
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        # Receiver-less: no out-of-band delivery tool is stored on the park.
        assert entry["completion_tool"] is None

    asyncio.run(go())


@pytest.mark.parametrize(
    ("marker", "why"),
    [
        # A nested driver's park, relayed as content by a middle agent that could not park.
        (suspended_interaction_marker("i-relayed", None, "nested_driver_resume"), "different run's resume binding"),
        # An older wire form / a nested RUN's park: no owner rides it at all.
        ({SUSPENDED_INTERACTION_MARKER_KEY: {"interaction_id": "i-relayed", "expiry_at": None}}, "no adoptable owner"),
        # Content a MODEL shaped to look like a park, reserved key and all.
        (
            {SUSPENDED_INTERACTION_MARKER_KEY: {"interaction_id": "i-relayed", "expiry_at": None, "resume_owner": ""}},
            "no adoptable owner",
        ),
    ],
    ids=["foreign", "ownerless", "injected"],
)
def test_tools_agent_refuses_to_claim_a_marker_it_does_not_own(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any, marker: dict[str, Any], why: str
) -> None:
    # THE CLAIM POINT. The park arrives as tool-result CONTENT — no sentinel object crossed a
    # seam — so the object-seam guards were never on this path. Claiming it would write this
    # run's park entry over the owning run's, then wait for a resume fired only at that run.
    saver = InMemorySaver()
    relay = _RawMarkerTool(marker)
    model = ScriptedChatModel([_relay_call(), AIMessage(content="I cannot run that here")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["relay"] = relay.tool()

    agent = _agent()

    async def go() -> None:
        result = await agent.run(
            tool_names=["relay"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-claim",
        )
        # The turn COMPLETED on the refusal: not a park receipt, not a raised run.
        assert result == "I cannot run that here"
        assert relay.calls == 1
        # The refusal reached the MODEL as this tool call's error result — never the raw
        # marker JSON as if it were the tool's answer, and never a dropped tool_call.
        refusals = _tool_messages(model.seen[1])
        assert len(refusals) == 1
        assert refusals[0].status == "error"
        assert why in refusals[0].content
        assert SUSPENDED_INTERACTION_MARKER_KEY not in refusals[0].content
        # The refusal never names the bound resume continuation (the forgery string).
        assert "agent_resume" not in refusals[0].content
        # Nothing of the relayed park was claimed: no index entry under this run.
        assert await idx.read_park_entry("i-relayed") is None

    asyncio.run(go())


def test_tools_agent_run_never_answers_with_a_park_marker(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # Probe shape (run face): a park this run cannot host must never escape as the run's
    # ANSWER — the marker's JSON is a suspend SIGNAL, and reading as text it would be an
    # answer no one wrote.
    saver = InMemorySaver()
    relay = _RawMarkerTool(suspended_interaction_marker("i-relayed", None, "nested_driver_resume"))
    model = ScriptedChatModel([_relay_call(), AIMessage(content="done without it")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["relay"] = relay.tool()

    agent = _agent()

    async def go() -> None:
        result = await agent.run(
            tool_names=["relay"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-escape",
        )
        assert isinstance(result, str)
        assert SUSPENDED_INTERACTION_MARKER_KEY not in result
        assert "i-relayed" not in result
        assert result == "done without it"

    asyncio.run(go())


def test_tools_agent_astream_still_reaches_a_terminal_event(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # Probe shape (streaming face): a park this run cannot host must not truncate the stream.
    # A drive that stops on an interrupt nothing classifies yields no terminal event, and a
    # consumer waiting for one waits forever.
    saver = InMemorySaver()
    relay = _RawMarkerTool(suspended_interaction_marker("i-relayed", None, "nested_driver_resume"))
    model = ScriptedChatModel([_relay_call(), AIMessage(content="done without it")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["relay"] = relay.tool()

    agent = _agent()

    async def go() -> None:
        events = [
            event
            async for event in agent.astream(
                tool_names=["relay"],
                checkpoint_provider="redis",
                user_message=TemplatedText(content="go"),
                thread_id="t-stream",
            )
        ]
        assert [event for event in events if getattr(event, "final", False)], events
        assert not [event for event in events if isinstance(event, SuspendedFinal)]

    asyncio.run(go())


def test_bind_resume_per_step_scopes_the_binding_to_the_step_not_the_consumer() -> None:
    # The resume continuation must be bound WHILE a drive step is computed, but never leak
    # across the yield into the consumer. PEP 568 is unimplemented, so a ``with`` in the yielding
    # generator's own body would land the ContextVar in the CONSUMER's task and persist it across
    # every yield. ``bind_resume_per_step`` re-enters the binding per ``__anext__`` instead.
    park = _park()
    seen_in_step: list[str | None] = []

    async def inner() -> AsyncIterator[int]:
        for i in range(3):
            # Inside __anext__ (the step): the binding is active, so the tool dispatch sees it.
            seen_in_step.append(get_resume_continuation_tool())
            yield i

    async def go() -> list[str | None]:
        seen_in_consumer: list[str | None] = []
        async for _item in park_drive_mod.bind_resume_per_step(lambda: park_drive_mod.park_continuation(park), inner()):
            # In the consumer, between steps, the binding must NOT be visible.
            seen_in_consumer.append(get_resume_continuation_tool())
        return seen_in_consumer

    assert get_resume_continuation_tool() is None
    seen_in_consumer = asyncio.run(go())
    assert seen_in_step == [AGENT_RESUME_TOOL_NAME] * 3
    assert seen_in_consumer == [None, None, None]
    # No binding leaked out after the stream drained.
    assert get_resume_continuation_tool() is None


def test_bind_resume_per_step_abandoned_midstream_leaves_a_clean_context() -> None:
    # Abandoning the stream mid-flight must close the inner generator and leave the consumer's
    # context clean — no binding stranded, and no ValueError from a foreign-context Token reset on
    # aclose (the failure the in-generator ``with`` produced).
    park = _park()
    closed = False

    async def inner() -> AsyncIterator[int]:
        nonlocal closed
        try:
            i = 0
            while True:
                yield i
                i += 1
        finally:
            closed = True

    async def go() -> None:
        agen = park_drive_mod.bind_resume_per_step(lambda: park_drive_mod.park_continuation(park), inner())
        first = await agen.__anext__()
        assert first == 0
        # Consumer context is clean the moment the item is handed over.
        assert get_resume_continuation_tool() is None
        # Abandon mid-stream: aclose must not raise and must leave the context clean.
        await agen.aclose()

    asyncio.run(go())
    assert closed is True
    assert get_resume_continuation_tool() is None


def test_binding_inside_a_generator_body_leaks_into_the_consumer() -> None:
    # The anti-pattern ``bind_resume_per_step`` exists to replace: a ``with park_continuation``
    # wrapping the ``yield`` lands the ContextVar in the CONSUMER's task and persists it across the
    # yield. This pins the leak so the fix is not silently reverted to the in-generator form.
    park = _park()

    async def leaky() -> AsyncIterator[int]:
        with park_drive_mod.park_continuation(park):
            for i in range(2):
                yield i

    async def go() -> list[str | None]:
        seen_in_consumer: list[str | None] = []
        async for _item in leaky():
            seen_in_consumer.append(get_resume_continuation_tool())
        return seen_in_consumer

    seen_in_consumer = asyncio.run(go())
    # The binding leaked: the consumer saw it bound across the yield (the defect).
    assert seen_in_consumer == [AGENT_RESUME_TOOL_NAME, AGENT_RESUME_TOOL_NAME]
