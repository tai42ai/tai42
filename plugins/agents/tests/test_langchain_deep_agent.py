"""Build-path + run-wiring tests for the ``langchain_deep_agent`` :class:`Agent`.

Covers the ``DeepAgent`` build path (subagents / inline-skills reach the factory, the
recursion cap and resume-checkpoint pinning land on the config, pending interrupts
are read from the paused snapshot) and the JSON tool-face ``run`` (tool names + JSON
subagents + rendered messages resolved, then drained through the shared streaming
core). The astream projection lives in ``test_deep_agent_astream.py``; subagent-spec
resolution in ``test_deep_agent_subagent_specs.py``; the run-door contract in
``test_run_input.py``. All with fakes (no live LLM, no real store); async code driven
with ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.agent import Agent
from tai42_contract.agent.base import AgentInterruptedError
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tests._deep_agent_fakes import (
    _client_tool,
    _drain_astream,
    _FakeCompiledGraph,
    _install_fake_graph,
    _install_fake_resolve,
    _scripted_chunks,
)
from tests._delivery_scope import assert_delivery_scoped, probe_tool

from tai42_agents.langchain_deep_agent import agent as agent_mod
from tai42_agents.langchain_deep_agent.agent import DeepAgent
from tai42_agents.langchain_deep_agent.spec import InlineSkill, ResolvedSubAgentSpec
from tai42_agents.langchain_deep_agent.tool_spec import DeepSubAgentSpec

# ===========================================================================
# DeepAgent build path
# ===========================================================================


class _FakeRegistry:
    async def get_checkpointer(self, **kwargs: object) -> None:
        return None

    async def get_store(self, **kwargs: object) -> None:
        return None


class _FakeProviderSettings:
    llm = "fake"
    checkpoint = "memory"
    checkpoint_conn_string = None
    store = "memory"
    store_conn_string = None


class _FakeLlmSettings:
    def with_fallbacks(self, kwargs: object) -> dict[str, Any]:
        return {}


def _patch_build(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    async def fake_build_deep_agent(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    async def fake_get_llm_async(provider: str, **kwargs: object) -> object:
        return object()

    monkeypatch.setattr(agent_mod, "build_langchain_deep_agent", fake_build_deep_agent)
    monkeypatch.setattr(agent_mod, "get_llm_async", fake_get_llm_async)
    monkeypatch.setattr(agent_mod, "checkpoint_registry", _FakeRegistry)
    monkeypatch.setattr(agent_mod, "store_registry", _FakeRegistry)
    monkeypatch.setattr(agent_mod, "llm_provider_settings", _FakeProviderSettings)
    monkeypatch.setattr(agent_mod, "llm_settings", _FakeLlmSettings)
    monkeypatch.setattr(agent_mod, "init_langgraph_config", lambda config=None: dict(config or {}))


def _build_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tools": [],
        "subagents": [],
        "skills": None,
        "inline_skills": None,
        "system_message": "",
        "response_format": None,
        "interrupt_on": None,
        "thread_id": "thread",
        "resume_checkpoint_id": None,
        "llm_provider": None,
        "checkpoint_provider": None,
        "store_provider": None,
        "llm_kwargs": None,
        "recursion_limit": None,
    }
    base.update(overrides)
    return base


def test_build_agent_passes_subagents_to_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _patch_build(monkeypatch, captured)

    spec = ResolvedSubAgentSpec(name="helper", description="d", system_prompt=TemplatedText(content="p"))
    agent: Any = DeepAgent()
    asyncio.run(agent._build_agent(**_build_kwargs(subagents=[spec])))
    assert captured["subagents"] == [spec]

    captured.clear()
    asyncio.run(agent._build_agent(**_build_kwargs(subagents=[])))
    assert captured["subagents"] is None


def test_build_agent_forwards_inline_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """inline_skills reach build_langchain_deep_agent on the stream path; empty → None."""
    captured: dict[str, Any] = {}
    _patch_build(monkeypatch, captured)

    agent: Any = DeepAgent()
    skill = InlineSkill(name="demo", content="# demo")
    asyncio.run(agent._build_agent(**_build_kwargs(inline_skills=[skill])))
    assert captured["inline_skills"] == [skill]

    captured.clear()
    asyncio.run(agent._build_agent(**_build_kwargs(inline_skills=[])))
    assert captured["inline_skills"] is None


def test_build_agent_caps_recursion_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_build(monkeypatch, {})
    agent: Any = DeepAgent()
    _, config = asyncio.run(agent._build_agent(**_build_kwargs(recursion_limit=4242)))
    assert config["recursion_limit"] == 4242


def test_build_agent_pins_resume_checkpoint_in_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_build(monkeypatch, {})
    agent: Any = DeepAgent()
    _, config = asyncio.run(agent._build_agent(**_build_kwargs(resume_checkpoint_id="cp-7")))
    assert config["configurable"]["checkpoint_id"] == "cp-7"


def test_build_agent_omits_thread_id_when_keyless(monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyless one-shot stream must not pin ``thread_id=None`` (which collides all
    keyless runs on the shared checkpoint thread); the config carries no thread_id
    key, so ``init_langgraph_config`` mints a fresh isolated one instead."""
    _patch_build(monkeypatch, {})
    agent: Any = DeepAgent()
    _, config = asyncio.run(agent._build_agent(**_build_kwargs(thread_id=None)))
    assert "thread_id" not in config["configurable"]


def test_pending_interrupts_reads_paused_snapshot() -> None:
    class FakeAgent:
        async def aget_state(self, config: object) -> SimpleNamespace:
            return SimpleNamespace(interrupts=[SimpleNamespace(id="i-9", value={"q": "?"})])

    agent: Any = DeepAgent()
    (interrupt,) = asyncio.run(agent._pending_interrupts(FakeAgent(), {}))
    assert (interrupt.interrupt_id, interrupt.payload) == ("i-9", {"q": "?"})


def test_astream_requires_exactly_one_of_message_or_resume() -> None:
    agent = DeepAgent()

    async def drain(gen: Any) -> None:
        async for _ in gen:
            pass

    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(drain(agent.astream(thread_id="t")))
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(drain(agent.astream(thread_id="t", user_message=TemplatedText(content="hi"), resume={"x": 1})))


# ===========================================================================
# DeepAgent.run wiring (JSON tool-face -> drained streaming core)
# ===========================================================================


def test_deep_agent_registers() -> None:
    agent = tai42_app.agents.get_agent("langchain_deep_agent")
    assert isinstance(agent, DeepAgent)
    assert isinstance(agent, Agent)


def test_deep_agent_astream_rejects_response_format_without_title() -> None:
    """The streaming face — the one the public run door drives — rejects an untitled
    ``response_format`` up front, exactly as the invoke face does."""
    with pytest.raises(ValueError, match="top-level 'title'"):
        _drain_astream(
            DeepAgent().astream(
                tool_names=[],
                user_message=TemplatedText(content="go"),
                response_format={"type": "object", "properties": {"x": {"type": "string"}}},
            )
        )


def test_deep_agent_rejects_response_format_without_title() -> None:
    with pytest.raises(ValueError, match="top-level 'title'"):
        asyncio.run(
            DeepAgent().run(
                tool_names=[],
                user_message=TemplatedText(content="go"),
                response_format={"type": "object", "properties": {"x": {"type": "string"}}},
            )
        )


def test_deep_agent_astream_rejects_oneof_response_format_with_untitled_variant() -> None:
    """A ``oneOf`` ``response_format`` whose variants lack titles (each binds its own
    structured-output name) is rejected up front, even though the container is
    titled."""
    schema = {"title": "Top", "oneOf": [{"title": "A", "type": "object"}, {"type": "object"}]}
    with pytest.raises(ValueError, match="oneOf variants must each"):
        _drain_astream(
            DeepAgent().astream(tool_names=[], user_message=TemplatedText(content="go"), response_format=schema)
        )


def test_deep_agent_run_rejects_oneof_response_format_with_untitled_variant() -> None:
    schema = {"title": "Top", "oneOf": [{"title": "A", "type": "object"}, {"type": "object"}]}
    with pytest.raises(ValueError, match="oneOf variants must each"):
        asyncio.run(DeepAgent().run(tool_names=[], user_message=TemplatedText(content="go"), response_format=schema))


def test_run_drains_streaming_core_with_resolved_inputs(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """run resolves tool names + JSON subagents + rendered messages, then drains
    the shared streaming core (the same one ``astream`` yields) to a final value."""
    for name in ("calc", "search"):
        app_tools.client_tools[name] = _client_tool(name)

    captured: dict[str, Any] = {}
    graph = _FakeCompiledGraph(_scripted_chunks(), interrupts=[])

    async def fake_resolve_and_build(**kwargs: Any) -> _FakeCompiledGraph:
        captured.update(kwargs)
        return graph

    agent: Any = DeepAgent()
    monkeypatch.setattr(agent, "_resolve_and_build", fake_resolve_and_build)

    subagents = [
        DeepSubAgentSpec(
            name="researcher",
            description="does research",
            system_prompt=TemplatedText(content="research"),
            tools=["search"],
        )
    ]

    result = asyncio.run(
        agent.run(
            tool_names=["calc"],
            subagents=subagents,
            skills=["/skills/foo"],
            inline_skills=[InlineSkill(name="demo", content="# demo")],
            system_message=TemplatedText(content="SYS"),
            user_message=TemplatedText(content="go"),
            interrupt_on={"calc": True},
            response_format={"title": "N", "type": "object", "properties": {"n": {"type": "integer"}}},
            langgraph_config={"configurable": {"thread_id": "t"}},
        )
    )

    # The scripted run emits a StructuredFinal, so the drained value is the
    # structured object (response_format was requested).
    assert result == {"answer": "ok"}

    # tool names -> live tools; JSON subagents -> resolved core specs.
    assert [tool.name for tool in captured["tools"]] == ["calc"]
    assert len(captured["subagents"]) == 1
    sub = captured["subagents"][0]
    assert isinstance(sub, ResolvedSubAgentSpec)
    assert [tool.name for tool in sub.tools] == ["search"]

    # Rendered system prompt + skills/inline-skills/interrupt_on reach the builder.
    assert captured["system_message"] == "SYS"
    assert captured["skills"] == ["/skills/foo"]
    assert [s.name for s in captured["inline_skills"]] == ["demo"]
    assert captured["interrupt_on"] == {"calc": True}

    # A fresh turn feeds the rendered user message as the graph input.
    assert graph.received_input == {"messages": [{"role": "user", "content": "go"}]}


def test_run_raises_agent_interrupted_on_paused_run(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """A run that pauses on an interrupt raises AgentInterruptedError (the terminal
    rule), even with response_format set — never a pre-interrupt value or a
    misleading RuntimeError about missing structured output."""
    graph = _FakeCompiledGraph(_scripted_chunks(), interrupts=[SimpleNamespace(id="i-run", value={"q": "pick"})])
    agent: Any = DeepAgent()
    _install_fake_resolve(monkeypatch, agent, graph)

    with pytest.raises(AgentInterruptedError) as excinfo:
        asyncio.run(
            agent.run(
                user_message=TemplatedText(content="go"),
                interrupt_on={"task": True},
                response_format={"title": "N", "type": "object", "properties": {"n": {"type": "integer"}}},
            )
        )
    assert [intr.interrupt_id for intr in excinfo.value.interrupts] == ["i-run"]


def test_run_resume_feeds_a_command(monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any) -> None:
    """run with a resume payload feeds a langgraph Command(resume=...) — no user
    message rendered — and drains the resumed run to its value."""
    from langgraph.types import Command

    graph = _FakeCompiledGraph([], interrupts=[])
    agent: Any = DeepAgent()
    _install_fake_resolve(monkeypatch, agent, graph)

    asyncio.run(agent.run(resume={"answer": 1}, thread_id="t"))

    assert isinstance(graph.received_input, Command)
    assert graph.received_input.resume == {"answer": 1}


def test_run_threads_user_content_kwargs_into_graph_input(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """``user_content_kwargs`` marks the fresh-turn user message as a content block
    in the graph input (the deepagents system prompt takes no such keys)."""
    graph = _FakeCompiledGraph(_scripted_chunks(), interrupts=[])
    agent: Any = DeepAgent()
    _install_fake_resolve(monkeypatch, agent, graph)

    asyncio.run(
        agent.run(
            user_message=TemplatedText(content="go"), user_content_kwargs={"cache_control": {"type": "ephemeral"}}
        )
    )

    assert graph.received_input == {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "go", "cache_control": {"type": "ephemeral"}}]}
        ]
    }


def test_astream_threads_user_content_kwargs_into_graph_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parity with :func:`test_run_threads_user_content_kwargs_into_graph_input`: the
    streaming face marks the fresh-turn user message as a content block in the graph
    input (the deepagents system prompt takes no such keys)."""
    graph = _FakeCompiledGraph(_scripted_chunks(), interrupts=[])
    agent: Any = DeepAgent()
    _install_fake_graph(monkeypatch, agent, graph)

    async def collect() -> list[Any]:
        return [
            event
            async for event in agent.astream(
                user_message=TemplatedText(content="go"), user_content_kwargs={"cache_control": {"type": "ephemeral"}}
            )
        ]

    asyncio.run(collect())

    assert graph.received_input == {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "go", "cache_control": {"type": "ephemeral"}}]}
        ]
    }


def test_run_rejects_user_content_kwargs_with_resume(app_tools: Any, resource_manager: Any) -> None:
    """``user_content_kwargs`` has no user message to attach to on a resume, so the
    combination raises rather than silently dropping the keys."""
    with pytest.raises(ValueError, match="user_content_kwargs applies to a fresh user_message turn"):
        asyncio.run(DeepAgent().run(resume={"answer": 1}, user_content_kwargs={"cache_control": {"type": "ephemeral"}}))


def test_astream_rejects_user_content_kwargs_with_resume() -> None:
    """Same resume guard on the streaming face, in parity with :meth:`run`."""
    cache = {"cache_control": {"type": "ephemeral"}}
    with pytest.raises(ValueError, match="user_content_kwargs applies to a fresh user_message turn"):
        _drain_astream(DeepAgent().astream(resume={"answer": 1}, user_content_kwargs=cache))


def test_run_rejects_resume_with_user_message(app_tools: Any, resource_manager: Any) -> None:
    """run cannot both answer an interrupt and start a fresh turn — a resume with a
    user message raises loudly rather than silently dropping one."""
    with pytest.raises(ValueError, match="exactly one of user_message or resume"):
        asyncio.run(DeepAgent().run(resume={"x": 1}, user_message=TemplatedText(content="go")))


def test_run_rejects_neither_message_nor_resume() -> None:
    """run with neither a user_message nor resume raises in the
    caller's own vocabulary — the same exactly-one-of guard astream uses — rather
    than falling through to the resource manager's template-vocabulary message."""
    with pytest.raises(ValueError, match="exactly one of user_message or resume") as excinfo:
        asyncio.run(DeepAgent().run())
    assert "template" not in str(excinfo.value)


def test_run_honors_live_tools(monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any) -> None:
    """run honors live ``tools`` (as astream does): they reach the builder combined
    with the client tools resolved from ``tool_names`` — never silently dropped."""
    app_tools.client_tools["calc"] = _client_tool("calc")
    live = _client_tool("live")

    captured: dict[str, Any] = {}

    async def fake_resolve_and_build(**kwargs: Any) -> _FakeCompiledGraph:
        captured.update(kwargs)
        return _FakeCompiledGraph(_scripted_chunks(), interrupts=[])

    agent: Any = DeepAgent()
    monkeypatch.setattr(agent, "_resolve_and_build", fake_resolve_and_build)

    asyncio.run(agent.run(tools=[live], tool_names=["calc"], user_message=TemplatedText(content="go")))
    assert [tool.name for tool in captured["tools"]] == ["live", "calc"]


def test_run_dispatched_tools_are_delivery_scoped(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """A tool this agent dispatches is a STEP of its turn, never a second answerer of it: it
    must not read the completion binding addressing the agent's own deferred answer off the
    contextvar. The deep agent assembles its tool list at its own seam, not through
    ``resolve_tools``, so the rule is pinned here too."""
    probe, seen = probe_tool("calc")
    app_tools.client_tools["calc"] = probe

    captured: dict[str, Any] = {}

    async def fake_resolve_and_build(**kwargs: Any) -> _FakeCompiledGraph:
        captured.update(kwargs)
        return _FakeCompiledGraph(_scripted_chunks(), interrupts=[])

    agent: Any = DeepAgent()
    monkeypatch.setattr(agent, "_resolve_and_build", fake_resolve_and_build)

    asyncio.run(agent.run(tool_names=["calc"], user_message=TemplatedText(content="go")))
    assert_delivery_scoped(captured["tools"][0], seen)


def test_astream_dispatched_tools_are_delivery_scoped(monkeypatch: pytest.MonkeyPatch, app_tools: Any) -> None:
    """The astream seam carries the SAME rule as ``run`` above."""
    probe, seen = probe_tool("calc")
    app_tools.client_tools["calc"] = probe

    captured: dict[str, Any] = {}

    async def fake_build_agent(**kwargs: Any) -> tuple[_FakeCompiledGraph, dict[str, Any]]:
        captured.update(kwargs)
        return _FakeCompiledGraph([], interrupts=[]), {"configurable": {"thread_id": "t"}}

    agent: Any = DeepAgent()
    monkeypatch.setattr(agent, "_build_agent", fake_build_agent)

    async def collect() -> list[Any]:
        return [event async for event in agent.astream(user_message=TemplatedText(content="go"), tool_names=["calc"])]

    asyncio.run(collect())
    assert_delivery_scoped(captured["tools"][0], seen)


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("key", ["thread_id"])  # resume_checkpoint_id is not a memory key (unhonored)
def test_run_rejects_blank_memory_key(key: str, blank: str) -> None:
    """A present-but-blank memory key is malformed — it would silently share a
    checkpoint namespace across runs — so run raises rather than minting/overlaying it."""
    with pytest.raises(ValueError, match=rf"langchain_deep_agent\.run: {key} must be a non-empty string"):
        # The dynamic key spreads into a typed run parameter, so the arg-type mismatch is expected.
        asyncio.run(DeepAgent().run(user_message=TemplatedText(content="go"), **{key: blank}))  # type: ignore[arg-type]


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("key", ["thread_id"])  # resume_checkpoint_id is not a memory key (unhonored)
def test_astream_rejects_blank_memory_key(key: str, blank: str) -> None:
    """Parity with run: astream rejects a present-but-blank memory key loudly."""
    with pytest.raises(ValueError, match=rf"langchain_deep_agent\.astream: {key} must be a non-empty string"):
        # The dynamic key spreads into a typed astream parameter, so the arg-type mismatch is expected.
        _drain_astream(DeepAgent().astream(user_message=TemplatedText(content="go"), **{key: blank}))  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [123, ["x"]])
@pytest.mark.parametrize("key", ["thread_id"])  # resume_checkpoint_id is not a memory key (unhonored)
def test_run_rejects_non_string_memory_key(key: str, value: Any) -> None:
    """A non-string memory key is a type violation — it cannot name a checkpoint
    namespace — so run raises TypeError naming the offending param and the received
    type rather than probing a non-string for whitespace."""
    with pytest.raises(
        TypeError, match=rf"langchain_deep_agent\.run: {key} must be a string or None; got {type(value).__name__}"
    ):
        # The dynamic key spreads into a typed run parameter, so the arg-type mismatch is expected.
        asyncio.run(DeepAgent().run(user_message=TemplatedText(content="go"), **{key: value}))  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [123, ["x"]])
@pytest.mark.parametrize("key", ["thread_id"])  # resume_checkpoint_id is not a memory key (unhonored)
def test_astream_rejects_non_string_memory_key(key: str, value: Any) -> None:
    """Parity with run: astream rejects a non-string memory key with a TypeError naming
    the offending param and the received type."""
    with pytest.raises(
        TypeError, match=rf"langchain_deep_agent\.astream: {key} must be a string or None; got {type(value).__name__}"
    ):
        # The dynamic key spreads into a typed astream parameter, so the arg-type mismatch is expected.
        _drain_astream(DeepAgent().astream(user_message=TemplatedText(content="go"), **{key: value}))  # type: ignore[arg-type]


def test_run_config_pins_thread_and_checkpoint() -> None:
    """_run_config overlays an explicit thread_id / resume_checkpoint_id /
    recursion_limit onto a caller-supplied langgraph_config base."""
    config = DeepAgent._run_config({"configurable": {"extra": 1}}, "t", "cp-7", 99)
    assert config["configurable"]["thread_id"] == "t"
    assert config["configurable"]["checkpoint_id"] == "cp-7"
    assert config["configurable"]["extra"] == 1
    assert config["recursion_limit"] == 99


def test_astream_honors_langgraph_config_thread_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """astream honors a ``configurable.thread_id`` carried in the caller's
    ``langgraph_config`` — the same overlay run applies — so a pinned conversation
    thread reaches the compiled graph's checkpointer over the streaming face too.
    The config's other ``configurable`` keys survive the overlay."""
    graph = _FakeCompiledGraph(_scripted_chunks(), interrupts=[])
    agent: Any = DeepAgent()
    _install_fake_resolve(monkeypatch, agent, graph)

    async def collect() -> list[Any]:
        return [
            event
            async for event in agent.astream(
                user_message=TemplatedText(content="go"),
                langgraph_config={"configurable": {"thread_id": "T-42", "tenant": "acme"}},
            )
        ]

    asyncio.run(collect())
    assert graph.received_config["configurable"]["thread_id"] == "T-42"
    assert graph.received_config["configurable"]["tenant"] == "acme"


def test_astream_explicit_thread_id_wins_over_langgraph_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The explicit ``thread_id`` parameter overlays the caller's ``langgraph_config``
    on the streaming face, exactly as it does on run."""
    graph = _FakeCompiledGraph(_scripted_chunks(), interrupts=[])
    agent: Any = DeepAgent()
    _install_fake_resolve(monkeypatch, agent, graph)

    async def collect() -> list[Any]:
        return [
            event
            async for event in agent.astream(
                user_message=TemplatedText(content="go"),
                thread_id="explicit",
                langgraph_config={"configurable": {"thread_id": "from-config"}},
            )
        ]

    asyncio.run(collect())
    assert graph.received_config["configurable"]["thread_id"] == "explicit"


def test_run_config_mints_fresh_thread_when_keyless() -> None:
    """With no thread pinned, each run gets a fresh isolated thread_id (never a
    shared/None key that would collide runs on one checkpoint thread)."""
    first = DeepAgent._run_config(None, None, None, None)
    second = DeepAgent._run_config(None, None, None, None)
    t1 = first["configurable"]["thread_id"]
    t2 = second["configurable"]["thread_id"]
    assert t1
    assert t2
    assert t1 != t2
