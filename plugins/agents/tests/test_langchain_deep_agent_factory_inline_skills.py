"""``langchain_deep_agent`` factory inline skills: collection across agents,
auto-loading, collision handling, and per-turn cache marking.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from tai42_contract.template import TemplatedText
from tai42_kit.llm.runtime import build_agent_input
from tests._langchain_deep_agent_factory_support import (
    _FAKE_LLM,
    _inline,
    _mark_count,
    _RecordingChatModel,
)

from tai42_agents.langchain_deep_agent import factory
from tai42_agents.langchain_deep_agent.backend import SKILLS_ROOT
from tai42_agents.langchain_deep_agent.factory import (
    _collect_inline_skills,
    _resolve_subagent,
    _skills_with_inline,
    build_langchain_deep_agent,
)
from tai42_agents.langchain_deep_agent.spec import ResolvedSubAgentSpec


def test_collect_inline_skills_spans_all_agents() -> None:
    """Inline skills from the main agent + subagent + nested merge into one map."""
    child = ResolvedSubAgentSpec(
        name="c", description="cd", system_prompt=TemplatedText(content="cp"), inline_skills=[_inline("child", "C")]
    )
    parent = ResolvedSubAgentSpec(
        name="a",
        description="d",
        system_prompt=TemplatedText(content="p"),
        inline_skills=[_inline("parent", "P")],
        subagents=[child],
    )
    collected = _collect_inline_skills([_inline("main", "M")], [parent])
    assert collected == {"main": "M", "parent": "P", "child": "C"}


def test_collect_inline_skills_shared_name_identical_content_ok() -> None:
    """A name reused across agents with identical content collapses to one mount."""
    parent = ResolvedSubAgentSpec(
        name="a", description="d", system_prompt=TemplatedText(content="p"), inline_skills=[_inline("shared", "X")]
    )
    collected = _collect_inline_skills([_inline("shared", "X")], [parent])
    assert collected == {"shared": "X"}


def test_collect_inline_skills_name_collision_differing_content_raises() -> None:
    parent = ResolvedSubAgentSpec(
        name="a", description="d", system_prompt=TemplatedText(content="p"), inline_skills=[_inline("dup", "B")]
    )
    with pytest.raises(ValueError, match="different content"):
        _collect_inline_skills([_inline("dup", "A")], [parent])


def test_skills_with_inline_auto_loads_inline_paths() -> None:
    """Inline skill names become /skills/<name>/ sources, alongside reference skills."""
    sources = _skills_with_inline([f"{SKILLS_ROOT}ref/"], [_inline("demo", "x")])
    assert sources == [f"{SKILLS_ROOT}ref/", f"{SKILLS_ROOT}demo/"]


def test_skills_with_inline_none_when_no_skills() -> None:
    assert _skills_with_inline(None, None) is None


def test_skills_with_inline_no_duplicate_when_already_listed() -> None:
    """Naming an inline skill in `skills` too does not duplicate its source."""
    sources = _skills_with_inline([f"{SKILLS_ROOT}demo/"], [_inline("demo", "x")])
    assert sources == [f"{SKILLS_ROOT}demo/"]


def test_resolve_subagent_auto_loads_its_inline_skills() -> None:
    """A subagent's inline skills are mounted via its loaded skill sources."""
    spec = ResolvedSubAgentSpec(
        name="b",
        description="d",
        system_prompt=TemplatedText(content="p"),
        skills=[f"{SKILLS_ROOT}ref/"],
        inline_skills=[_inline("demo", "x")],
    )
    sub = cast(dict[str, Any], asyncio.run(_resolve_subagent(spec)))
    assert sub["skills"] == [f"{SKILLS_ROOT}ref/", f"{SKILLS_ROOT}demo/"]


def test_build_deep_agent_mounts_and_auto_loads_inline_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_langchain_deep_agent feeds inline content to the backend and auto-loads the path."""
    captured: dict[str, Any] = {}
    built_backends: list[Any] = []

    def fake_create(*args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return "AGENT"

    def fake_build_backend(inline_skills: object = None) -> object:
        built_backends.append(inline_skills)
        return object()

    monkeypatch.setattr(factory, "create_deep_agent", fake_create)
    monkeypatch.setattr(factory, "build_backend", fake_build_backend)
    asyncio.run(
        build_langchain_deep_agent(
            llm=_FAKE_LLM,
            store=InMemoryStore(),
            checkpointer=InMemorySaver(),
            inline_skills=[_inline("inline-demo", "# inline-demo\nbody")],
        )
    )
    # Content reached the backend as a name -> content map...
    assert built_backends[-1] == {"inline-demo": "# inline-demo\nbody"}
    # ...and the inline skill's source path is auto-loaded for the main agent.
    assert captured["skills"] == [f"{SKILLS_ROOT}inline-demo/"]


def test_build_deep_agent_inline_collision_raises_before_create(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"create": False}

    def fake_create(*args: object, **kwargs: object) -> str:
        called["create"] = True
        return "AGENT"

    monkeypatch.setattr(factory, "create_deep_agent", fake_create)
    sub = ResolvedSubAgentSpec(
        name="b", description="d", system_prompt=TemplatedText(content="p"), inline_skills=[_inline("dup", "B")]
    )
    with pytest.raises(ValueError, match="different content"):
        asyncio.run(
            build_langchain_deep_agent(
                llm=_FAKE_LLM,
                store=InMemoryStore(),
                checkpointer=InMemorySaver(),
                inline_skills=[_inline("dup", "A")],
                subagents=[sub],
            )
        )
    assert called["create"] is False


def test_build_deep_agent_no_inline_skills_keeps_skills_none(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_create(*args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return "AGENT"

    monkeypatch.setattr(factory, "create_deep_agent", fake_create)
    asyncio.run(
        build_langchain_deep_agent(
            llm=_FAKE_LLM,
            store=InMemoryStore(),
            checkpointer=InMemorySaver(),
        )
    )
    assert captured["skills"] is None


def test_deep_agent_per_turn_marking_sends_one_breakpoint_on_a_reused_thread() -> None:
    # Two turns on one thread through the REAL deepagents factory graph (a scripted
    # fake chat model + in-memory checkpointer/store, no LLM or network): every turn
    # marks its user message and the marks persist into the reused thread's history.
    # Without rolling, the second turn's model call would carry two breakpoints (turn-1
    # and turn-2) and grow unbounded; the rolling-cache-mark middleware on the main
    # agent's stack strips every older mark at the model call, so each call sends
    # exactly one — the newest.
    model = _RecordingChatModel([AIMessage(content="first"), AIMessage(content="second")])
    kwargs = {"cache_control": {"type": "ephemeral"}}
    config: RunnableConfig = {"configurable": {"thread_id": "t-deep-roll"}}

    async def run_two_turns() -> list[BaseMessage]:
        agent = await build_langchain_deep_agent(
            llm=model,
            store=InMemoryStore(),
            checkpointer=InMemorySaver(),
            tools=[],
        )
        await agent.ainvoke(build_agent_input("hi", user_content_kwargs=kwargs), config)
        await agent.ainvoke(build_agent_input("again", user_content_kwargs=kwargs), config)
        snapshot = await agent.aget_state(config)
        return snapshot.values.get("messages", [])

    stored = asyncio.run(run_two_turns())

    # First model call: only the turn-1 user mark exists — inert, one breakpoint.
    assert _mark_count(model._seen[0]) == 1
    # Second model call: history holds turn-1 + turn-2 user marks; the older one is
    # stripped, so the outgoing request carries exactly one breakpoint.
    second_call = model._seen[1]
    assert _mark_count(second_call) == 1
    # The surviving mark is the newest turn's user message, not the older one.
    user_turns = [m for m in second_call if isinstance(m, HumanMessage)]
    assert user_turns[-1].content == [{"type": "text", "text": "again", "cache_control": {"type": "ephemeral"}}]
    assert user_turns[0].content == "hi"
    # The rewrite is request-scoped: the checkpointed thread still holds both marks,
    # so the next turn re-rolls from the same history rather than losing the record.
    assert _mark_count(stored) == 2
