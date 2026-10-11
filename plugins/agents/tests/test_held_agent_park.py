"""Every agent kind that declares it parks can park under a holder and send its caller ask up.

A neutral holder driver here does on the agent side exactly what a holding flow's visit does: it
binds a chained resume routing (a delivery tool and a fresh chained key) and its own resume
continuation, then starts the kind's run face with ``tool_names=["ask"]``, where ``ask`` is a
synthetic parking tool that parks a ``to="caller"`` ask. For each parking kind (``tools_agent``,
``langchain_deep_agent``, ``claude_code``) it proves:

1. the run returns a :class:`SuspendedInteraction` whose ``caller_interaction_ids`` name the ask —
   the material a holder classifies as caller asks and a ``send_up`` holder hands to its caller;
2. :func:`resolve_park_adoption` over that sentinel, under the holder's bindings, returns the bound
   chained key — the park reaches the holder as a chained park;
3. answering the ask resumes the kind, and its terminal is delivered to the holder's delivery tool
   carrying the chained key — the re-entry into the holder;
4. the kind's class declares ``parks``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr
from tai42_contract.access_control.context import reset_request_user_id, set_request_user_id
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    CHAINED_PARK_TOKEN_KEY,
    PARK_COMPLETION_SUCCEEDED,
    ChainedResume,
    SuspendedInteraction,
    get_resume_continuation_tool,
    reset_chained_resume,
    reset_resume_continuation_tool,
    set_chained_resume,
    set_resume_continuation_tool,
)
from tai42_contract.template import TemplatedText
from tai42_kit.interactions.park_adoption import chained_park_claims, new_chained_park_key, resolve_park_adoption
from tests._claude_app import build_local_app
from tests._claude_stubs import RESUME_ONCE, TOOL_CALL_PARK, payload_for
from tests._client_tool import client_tool
from tests._tools_agent_park_support import (
    ScriptedChatModel,
    _ask_call,
    _LlmSettings,
    _ProviderSettings,
    _Registry,
    _wire_tools_build,
)
from tests.conftest import APP, bind_park_index, fake_run_trace

import tai42_agents._internal.park.lease as lease_mod
import tai42_agents.claude_code.agent as claude_agent_module
import tai42_agents.claude_code.workspace as claude_workspace_module
from tai42_agents._internal.park import agent_resume
from tai42_agents._internal.park import capability as park_capability
from tai42_agents.claude_code.agent import ClaudeCodeAgent
from tai42_agents.claude_code.settings import ClaudeCodeSettings
from tai42_agents.langchain_deep_agent import agent as deep_agent_module
from tai42_agents.langchain_deep_agent import run_thread as deep_run_thread
from tai42_agents.langchain_deep_agent.agent import DeepAgent
from tai42_agents.tools_agent import ToolsAgent

#: The holder's delivery tool: a held run's terminal re-enters the holder through it.
_DELIVERY_TOOL = "held_chain_deliver"
#: The resume continuation the holding driver binds around its own drive.
_HOLDER_CONTINUATION = "held_flow_resume"
#: The interaction id the synthetic caller ask parks.
_ASK_ID = "i-held-ask"
_DIGEST = "registry.example/claude@sha256:" + "c" * 64


class _DeliveryProbe:
    """The holder's delivery tool: records every fire and answers with the holder's own outcome."""

    def __init__(self) -> None:
        self.fired: list[dict[str, Any]] = []

    def __call__(self, **payload: Any) -> Any:
        self.fired.append(payload)
        return {"holder": "settled"}


def _caller_ask() -> SuspendedInteraction:
    """A ``to="caller"`` async ask: parked under the continuation bound where it runs, its own id the caller subset."""
    owner = get_resume_continuation_tool()
    if owner is None:
        raise RuntimeError("async ask requires a resuming driver (no resume_continuation_tool is bound)")
    return SuspendedInteraction(
        interaction_id=_ASK_ID,
        expiry_at=datetime.now(UTC) + timedelta(hours=1),
        resume_owner=owner,
        interaction_ids=[_ASK_ID],
        caller_interaction_ids=[_ASK_ID],
    )


@contextlib.contextmanager
def _holder_bindings() -> Iterator[str]:
    """Bind what a holding flow's visit binds around the held run: its continuation and a chained routing."""
    chain_key = new_chained_park_key()
    continuation = set_resume_continuation_tool(_HOLDER_CONTINUATION)
    routing = set_chained_resume(ChainedResume(delivery_tool=_DELIVERY_TOOL, chain_key=chain_key, asked_by=()))
    try:
        with chained_park_claims():
            yield chain_key
    finally:
        reset_chained_resume(routing)
        reset_resume_continuation_tool(continuation)


async def _held_run(run: Any) -> tuple[SuspendedInteraction, str, tuple[str, str | None]]:
    """Start the held run under the holder's bindings; resolve its park as the holder does."""
    with _holder_bindings() as chain_key:
        receipt = await run()
        assert isinstance(receipt, SuspendedInteraction), receipt
        adopted = resolve_park_adoption(receipt.resume_owner, interaction_id=receipt.interaction_id, tool_name="held")
    return receipt, chain_key, adopted


def _assert_held_park(
    receipt: SuspendedInteraction, chain_key: str, adopted: tuple[str, str | None], probe: _DeliveryProbe
) -> None:
    # (1) the caller ask reaches the holder as a caller ask.
    assert receipt.caller_interaction_ids == [_ASK_ID]
    # (2) the holder parks on its chained key, owned by its own continuation.
    assert adopted == (chain_key, _HOLDER_CONTINUATION)
    # (3) the resumed run's terminal re-entered the holder through its delivery tool.
    assert len(probe.fired) == 1
    assert probe.fired[0][CHAINED_PARK_TOKEN_KEY] == chain_key
    assert probe.fired[0]["status"] == PARK_COMPLETION_SUCCEEDED


@pytest.fixture
def park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    """The agents' park index and the workspace lease on one in-process Redis."""
    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def _client() -> AsyncIterator[Any]:
        yield redis

    bind_park_index(monkeypatch, redis)
    monkeypatch.setattr(lease_mod, "_lease_client", _client)
    return redis


@pytest.mark.parametrize("kind", [ToolsAgent, DeepAgent, ClaudeCodeAgent], ids=lambda k: k.__name__)
def test_each_parking_kind_declares_it_parks(kind: type[Any]) -> None:
    # (4) the declaration a holder's validation reads off the registered agent.
    assert kind.parks is True


@pytest.mark.usefixtures("park_redis")
def test_a_held_tools_agent_parks_and_sends_its_caller_ask_up(monkeypatch: pytest.MonkeyPatch, app_tools: Any) -> None:
    probe = _DeliveryProbe()
    model = ScriptedChatModel([_ask_call(), AIMessage(content="the held answer")])
    _wire_tools_build(monkeypatch, model, InMemorySaver())
    app_tools.client_tools["ask"] = client_tool("ask", _caller_ask, description="Ask the caller and park.")
    app_tools.tool_runners[_DELIVERY_TOOL] = probe
    agent = tai42_app.agents.get_agent("tools_agent")

    async def go() -> None:
        receipt, chain_key, adopted = await _held_run(
            lambda: agent.run(
                tool_names=["ask"],
                checkpoint_provider="redis",
                user_message=TemplatedText(content="go"),
                thread_id="t-held-tools",
            )
        )
        assert await agent_resume(_ASK_ID, "the caller's answer") == {"holder": "settled"}
        assert probe.fired[0]["result"] == "the held answer"
        _assert_held_park(receipt, chain_key, adopted, probe)

    asyncio.run(go())


@pytest.mark.usefixtures("park_redis")
def test_a_held_deep_agent_parks_and_sends_its_caller_ask_up(monkeypatch: pytest.MonkeyPatch, app_tools: Any) -> None:
    probe = _DeliveryProbe()
    saver = InMemorySaver()
    model = ScriptedChatModel([_ask_call(), AIMessage(content="the held answer")])

    async def scripted_llm(provider: str, **_kwargs: Any) -> Any:
        return model

    monkeypatch.setattr(deep_agent_module, "get_llm_async", scripted_llm)
    monkeypatch.setattr(deep_agent_module, "checkpoint_registry", lambda: _Registry(saver))
    monkeypatch.setattr(deep_agent_module, "llm_provider_settings", _ProviderSettings)
    monkeypatch.setattr(deep_agent_module, "llm_settings", _LlmSettings)
    monkeypatch.setattr(deep_run_thread, "init_langgraph_config", lambda config=None: fake_run_trace(config))
    monkeypatch.setattr(park_capability, "llm_provider_settings", _ProviderSettings)
    app_tools.client_tools["ask"] = client_tool("ask", _caller_ask, description="Ask the caller and park.")
    app_tools.tool_runners[_DELIVERY_TOOL] = probe
    agent = APP.agents.get_agent("langchain_deep_agent")

    async def go() -> None:
        receipt, chain_key, adopted = await _held_run(
            lambda: agent.run(
                tool_names=["ask"],
                checkpoint_provider="redis",
                user_message=TemplatedText(content="go"),
                thread_id="t-held-deep",
            )
        )
        assert await agent_resume(_ASK_ID, "the caller's answer") == {"holder": "settled"}
        assert probe.fired[0]["result"] == "the held answer"
        _assert_held_park(receipt, chain_key, adopted, probe)

    asyncio.run(go())


@pytest.mark.usefixtures("park_redis")
def test_a_held_claude_code_run_parks_and_sends_its_caller_ask_up(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _DeliveryProbe()
    settings = ClaudeCodeSettings(session_image=_DIGEST, api_key=SecretStr("modelkey"))
    monkeypatch.setattr(claude_agent_module, "claude_code_settings", lambda: settings)
    # The runner calls its first proxied tool — here the caller ask — and stops on the park.
    monkeypatch.setattr(claude_workspace_module, "runner_payload_files", payload_for(TOOL_CALL_PARK))
    agent = ClaudeCodeAgent()
    app = build_local_app(tool_runners={"ask": _caller_ask, _DELIVERY_TOOL: probe}, agents={"claude_code": agent})

    async def go() -> None:
        with tai42_app.bound(app):
            receipt, chain_key, adopted = await _held_run(
                lambda: agent.run(
                    user_message=TemplatedText(content="go"), thread_id="t-held-claude", tool_names=["ask"]
                )
            )
            # The resumed session runs to its terminal (the resume stub answers and completes).
            monkeypatch.setattr(claude_workspace_module, "runner_payload_files", payload_for(RESUME_ONCE))
            assert await agent_resume(_ASK_ID, "the caller's answer") == {"holder": "settled"}
        assert probe.fired[0]["result"] == "done-once"
        _assert_held_park(receipt, chain_key, adopted, probe)

    token = set_request_user_id("user-1")
    try:
        asyncio.run(go())
    finally:
        reset_request_user_id(token)
