"""``langchain_deep_agent`` factory nested subagents: two-level rejection,
name/skill validation, and nested compilation and inheritance.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from tai42_contract.template import TemplatedText
from tests._langchain_deep_agent_factory_support import (
    _FAKE_LLM,
    _FakeRunnable,
    _nested_pair,
    _tool,
)

from tai42_agents._internal.recovery import _tool_error_middleware
from tai42_agents.langchain_deep_agent import factory
from tai42_agents.langchain_deep_agent.backend import SKILLS_ROOT
from tai42_agents.langchain_deep_agent.factory import (
    _resolve_subagent,
    _validate,
    build_langchain_deep_agent,
)
from tai42_agents.langchain_deep_agent.spec import ResolvedSubAgentSpec


def test_validate_rejects_two_level_nesting() -> None:
    grandchild = ResolvedSubAgentSpec(name="g", description="gd", system_prompt=TemplatedText(content="gp"))
    child = ResolvedSubAgentSpec(
        name="c", description="cd", system_prompt=TemplatedText(content="cp"), subagents=[grandchild]
    )
    parent = ResolvedSubAgentSpec(
        name="a", description="d", system_prompt=TemplatedText(content="p"), subagents=[child]
    )
    with pytest.raises(ValueError, match="one level deep"):
        _validate([], [parent], None)


def test_validate_rejects_duplicate_nested_names() -> None:
    children = [
        ResolvedSubAgentSpec(name="c", description="cd", system_prompt=TemplatedText(content="cp")),
        ResolvedSubAgentSpec(name="c", description="cd", system_prompt=TemplatedText(content="cp")),
    ]
    parent = ResolvedSubAgentSpec(
        name="a", description="d", system_prompt=TemplatedText(content="p"), subagents=children
    )
    with pytest.raises(ValueError, match="duplicate nested subagent names"):
        _validate([], [parent], None)


def test_validate_rejects_nested_builtin_name() -> None:
    child = ResolvedSubAgentSpec(name="task", description="cd", system_prompt=TemplatedText(content="cp"))
    parent = ResolvedSubAgentSpec(
        name="a", description="d", system_prompt=TemplatedText(content="p"), subagents=[child]
    )
    with pytest.raises(ValueError, match="built-in tool names"):
        _validate([], [parent], None)


def test_validate_rejects_nested_offroot_skill() -> None:
    child = ResolvedSubAgentSpec(
        name="c", description="cd", system_prompt=TemplatedText(content="cp"), skills=["/nope/"]
    )
    parent = ResolvedSubAgentSpec(
        name="a", description="d", system_prompt=TemplatedText(content="p"), subagents=[child]
    )
    with pytest.raises(ValueError, match="must start with"):
        _validate([], [parent], None)


def test_validate_accepts_clean_nested_config() -> None:
    child = ResolvedSubAgentSpec(
        name="c", description="cd", system_prompt=TemplatedText(content="cp"), skills=[f"{SKILLS_ROOT}finder/"]
    )
    parent = ResolvedSubAgentSpec(
        name="a", description="d", system_prompt=TemplatedText(content="p"), subagents=[child]
    )
    _validate([], [parent], None)  # no raise


def test_resolve_subagent_with_nested_requires_main_pieces() -> None:
    parent, _ = _nested_pair()
    with pytest.raises(ValueError, match="requires"):
        asyncio.run(_resolve_subagent(parent))


def test_resolve_subagent_compiles_nested_into_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    def fake_create(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return _FakeRunnable()

    monkeypatch.setattr(factory, "create_deep_agent", fake_create)
    parent, _ = _nested_pair(child_tools=[_tool("search")], parent_tools=[_tool("x")])
    backend = object()
    sub = cast(
        dict[str, Any],
        asyncio.run(_resolve_subagent(parent, llm=_FAKE_LLM, tools=[], store=InMemoryStore(), backend=backend)),
    )
    # The nested child compiled as its own deep agent, inheriting the parent model.
    assert captured[0]["model"] == "LLM"
    assert [t.name for t in captured[0]["tools"]] == ["search"]
    assert captured[0]["system_prompt"] == "cp"
    assert captured[0]["backend"] is backend
    # ...and attached to the parent through a SubAgentMiddleware, between the leading
    # async-park hook and the shared tool-error middleware every subagent stack carries.
    async_park, sub_middleware, tool_error = sub["middleware"]
    assert async_park is factory._async_park_middleware
    assert isinstance(sub_middleware, factory.SubAgentMiddleware)
    assert tool_error is _tool_error_middleware


def test_nested_child_inherits_parent_tools_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    def fake_create(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return _FakeRunnable()

    monkeypatch.setattr(factory, "create_deep_agent", fake_create)
    parent, _ = _nested_pair(parent_tools=[_tool("x")])
    asyncio.run(_resolve_subagent(parent, llm=_FAKE_LLM, tools=[], store=InMemoryStore(), backend=object()))
    assert [t.name for t in captured[0]["tools"]] == ["x"]


def test_nested_child_resolves_own_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    def fake_create(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return _FakeRunnable()

    async def fake_get_llm_async(provider: str, **kwargs: object) -> str:
        return f"LLM:{provider}"

    monkeypatch.setattr(factory, "create_deep_agent", fake_create)
    monkeypatch.setattr(factory, "get_llm_async", fake_get_llm_async)
    child = ResolvedSubAgentSpec(
        name="finder", description="cd", system_prompt=TemplatedText(content="cp"), llm_provider="openai"
    )
    parent = ResolvedSubAgentSpec(
        name="advisor", description="d", system_prompt=TemplatedText(content="p"), subagents=[child]
    )
    asyncio.run(_resolve_subagent(parent, llm=_FAKE_LLM, tools=[], store=InMemoryStore(), backend=object()))
    assert captured[0]["model"] == "LLM:openai"


def test_build_deep_agent_passes_nested_subagents_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _FakeRunnable()

    monkeypatch.setattr(factory, "create_deep_agent", fake_create)
    parent, _ = _nested_pair(child_tools=[_tool("search")])
    asyncio.run(
        build_langchain_deep_agent(
            llm=_FAKE_LLM,
            store=InMemoryStore(),
            checkpointer=InMemorySaver(),
            subagents=[parent],
        )
    )
    main_call = calls[-1]
    # The caller's subagent passes through alongside the explicit general-purpose
    # subagent now supplied so its tool node carries the shared middleware.
    subs = {s["name"]: s for s in main_call["subagents"]}
    assert set(subs) == {"general-purpose", "advisor"}
    # The advisor's stack leads with the async-park hook, then its nested SubAgentMiddleware.
    assert subs["advisor"]["middleware"][0] is factory._async_park_middleware
    assert isinstance(subs["advisor"]["middleware"][1], factory.SubAgentMiddleware)
    assert _tool_error_middleware in subs["general-purpose"]["middleware"]
