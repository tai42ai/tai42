"""Tests for the deep-agent factory: subagent resolution + config validation guards."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, cast

import pytest
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel, PrivateAttr, TypeAdapter, ValidationError
from tai42_kit.llm.middleware.leading_user import LeadingUserMiddleware
from tai42_kit.llm.middleware.rolling_cache_mark import RollingCacheMarkMiddleware
from tai42_kit.llm.middleware.system_purge import SystemPurgeMiddleware
from tai42_kit.llm.runtime import build_agent_input

from tai42_agents._internal.recovery import _tool_error_middleware
from tai42_agents.deep_agent import factory
from tai42_agents.deep_agent.backend import SKILLS_ROOT
from tai42_agents.deep_agent.factory import (
    _collect_inline_skills,
    _resolve_subagent,
    _skills_with_inline,
    _validate,
    build_deep_agent,
)
from tai42_agents.deep_agent.spec import InlineSkill, ResolvedSubAgentSpec

_FAKE_LLM = cast(BaseChatModel, "LLM")

_INT64_OVERFLOW = 2**63


def _assert_bound_to_model(strategy: ToolStrategy[Any], model: type[BaseModel]) -> None:
    """A subagent whose response_format was ``model`` emits a bounded ToolStrategy:
    its schema (validated exactly as the tool-calling parse does, via a pydantic
    ``TypeAdapter``) accepts a conforming value, rejects an oversized int under the
    int64 bound, and binds under the model's name so structured tool-name stream
    suppression still matches."""
    adapter = TypeAdapter(strategy.schema)
    assert adapter.validate_python({"x": 1}) == {"x": 1}
    with pytest.raises(ValidationError):
        adapter.validate_python({"x": _INT64_OVERFLOW})
    assert getattr(strategy.schema, "__name__", None) == model.__name__


class _FakeRunnable:
    """A minimal runnable stand-in for a compiled nested subagent.

    ``deepagents``' ``SubAgentMiddleware`` eagerly compiles each nested runnable
    at construction (calling ``with_config``), so the fake ``create_deep_agent``
    must return an object exposing it rather than a bare string.
    """

    def with_config(self, *args: object, **kwargs: object) -> _FakeRunnable:
        return self


def _tool(name: str) -> StructuredTool:
    async def call_tool(**kwargs: object) -> object:
        return kwargs

    return StructuredTool.from_function(
        func=None,
        coroutine=call_tool,
        name=name,
        description="d",
        args_schema={"type": "object", "properties": {}, "required": []},
    )


# --- _resolve_subagent -----------------------------------------------------


def test_resolve_subagent_emits_only_set_keys() -> None:
    """Inheritance relies on optional keys being ABSENT, not None; the shared
    tool-error middleware is the one always-present stack entry every subagent gets."""
    spec = ResolvedSubAgentSpec(name="b", description="d", system_prompt="p")
    sub = cast(dict[str, Any], asyncio.run(_resolve_subagent(spec)))
    assert sub == {"name": "b", "description": "d", "system_prompt": "p", "middleware": [_tool_error_middleware]}


def test_resolve_subagent_resolves_model_when_provider_set(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_llm_async(provider: str, **kwargs: object) -> str:
        return f"LLM:{provider}"

    monkeypatch.setattr(factory, "get_llm_async", fake_get_llm_async)
    spec = ResolvedSubAgentSpec(name="b", description="d", system_prompt="p", llm_provider="openai")
    sub = cast(dict[str, Any], asyncio.run(_resolve_subagent(spec)))
    assert sub["model"] == "LLM:openai"


def test_resolve_subagent_passes_through_tools_skills_interrupt() -> None:
    spec = ResolvedSubAgentSpec(
        name="b",
        description="d",
        system_prompt="p",
        tools=[_tool("x")],
        skills=[f"{SKILLS_ROOT}jq/"],
        interrupt_on={"edit_file": True},
    )
    sub = cast(dict[str, Any], asyncio.run(_resolve_subagent(spec)))
    assert [t.name for t in sub["tools"]] == ["x"]
    assert sub["skills"] == [f"{SKILLS_ROOT}jq/"]
    assert sub["interrupt_on"] == {"edit_file": True}


# --- _validate guards ------------------------------------------------------


def test_validate_rejects_duplicate_subagents() -> None:
    specs = [
        ResolvedSubAgentSpec(name="x", description="d", system_prompt="p"),
        ResolvedSubAgentSpec(name="x", description="d", system_prompt="p"),
    ]
    with pytest.raises(ValueError, match="duplicate subagent"):
        _validate([], specs, None)


def test_validate_rejects_duplicate_tools() -> None:
    with pytest.raises(ValueError, match="duplicate tool"):
        _validate([_tool("a"), _tool("a")], [], None)


def test_validate_rejects_builtin_tool_collision() -> None:
    with pytest.raises(ValueError, match="built-in"):
        _validate([_tool("task")], [], None)


def test_validate_rejects_subagent_named_like_builtin() -> None:
    spec = ResolvedSubAgentSpec(name="task", description="d", system_prompt="p")
    with pytest.raises(ValueError, match="built-in tool names"):
        _validate([], [spec], None)


def test_validate_rejects_offroot_skill() -> None:
    with pytest.raises(ValueError, match="must start with"):
        _validate([], [], ["/wrong/x/"])


def test_validate_rejects_offroot_subagent_skill() -> None:
    spec = ResolvedSubAgentSpec(name="b", description="d", system_prompt="p", skills=["/nope/"])
    with pytest.raises(ValueError, match="must start with"):
        _validate([], [spec], None)


def test_validate_accepts_clean_config() -> None:
    spec = ResolvedSubAgentSpec(name="b", description="d", system_prompt="p", skills=[f"{SKILLS_ROOT}jq/"])
    _validate([_tool("search")], [spec], [f"{SKILLS_ROOT}flow/"])  # no raise


# --- build_deep_agent wiring ----------------------------------------------


def test_build_deep_agent_runs_validation_before_create(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation must reject bad config before reaching create_deep_agent."""
    called = {"create": False}

    def fake_create(*args: object, **kwargs: object) -> str:
        called["create"] = True
        return "AGENT"

    monkeypatch.setattr(factory, "create_deep_agent", fake_create)
    with pytest.raises(ValueError, match="must start with"):
        asyncio.run(
            build_deep_agent(
                llm=_FAKE_LLM,
                store=InMemoryStore(),
                checkpointer=InMemorySaver(),
                skills=["/bad/"],
            )
        )
    assert called["create"] is False


def test_resolve_subagent_emits_response_format() -> None:
    class M(BaseModel):
        x: int

    spec = ResolvedSubAgentSpec(name="b", description="d", system_prompt="p", response_format=M)
    sub = cast(dict[str, Any], asyncio.run(_resolve_subagent(spec)))
    # The schema is pinned to the tool-calling strategy, never provider-dependent
    # auto-routing.
    assert isinstance(sub["response_format"], ToolStrategy)
    # ...and bounded: an oversized int is a retryable parse failure, and the schema
    # binds under the model's name.
    _assert_bound_to_model(sub["response_format"], M)


def test_build_deep_agent_passes_response_format(monkeypatch: pytest.MonkeyPatch) -> None:
    class M(BaseModel):
        x: int

    captured: dict[str, Any] = {}

    def fake_create(*args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return "AGENT"

    monkeypatch.setattr(factory, "create_deep_agent", fake_create)
    asyncio.run(
        build_deep_agent(
            llm=_FAKE_LLM,
            store=InMemoryStore(),
            checkpointer=InMemorySaver(),
            response_format=M,
        )
    )
    # The schema is pinned to the tool-calling strategy, never provider-dependent
    # auto-routing.
    assert isinstance(captured["response_format"], ToolStrategy)
    # ...and bounded: an oversized int is a retryable parse failure, and the schema
    # binds under the model's name.
    _assert_bound_to_model(cast(ToolStrategy[Any], captured["response_format"]), M)


def test_build_deep_agent_leads_with_system_purge_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    # The purge middleware leads the main agent's stack, so a thread whose stored
    # history carries a system message (written by another face) runs cleanly:
    # state never reaches the model with one alongside the per-run prompt.
    captured: dict[str, Any] = {}
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kwargs: captured.update(kwargs) or "AGENT")
    asyncio.run(build_deep_agent(llm=_FAKE_LLM, store=InMemoryStore(), checkpointer=InMemorySaver()))
    assert isinstance(captured["middleware"][0], SystemPurgeMiddleware)


def test_build_deep_agent_wires_rolling_cache_mark_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    # deep_agent honors user_content_kwargs (the mark rides its main user turn), so its
    # main-agent stack carries the rolling-cache-mark middleware too — a per-turn-marked
    # thread sends one breakpoint at the model call, same as every tools-agent face.
    captured: dict[str, Any] = {}
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kwargs: captured.update(kwargs) or "AGENT")
    asyncio.run(build_deep_agent(llm=_FAKE_LLM, store=InMemoryStore(), checkpointer=InMemorySaver()))
    assert any(isinstance(mw, RollingCacheMarkMiddleware) for mw in captured["middleware"])


def test_build_deep_agent_pins_main_middleware_order(monkeypatch: pytest.MonkeyPatch) -> None:
    # The main agent's stack is order-pinned: system purge clears a stored system message
    # first, leading-user keeps the thread user-first, rolling-cache-mark rolls the
    # breakpoint at the call, and the shared tool-error middleware trails last (mirroring
    # every subagent stack's tool-error tail).
    captured: dict[str, Any] = {}
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kwargs: captured.update(kwargs) or "AGENT")
    asyncio.run(build_deep_agent(llm=_FAKE_LLM, store=InMemoryStore(), checkpointer=InMemorySaver()))
    stack = captured["middleware"]
    assert len(stack) == 4
    assert isinstance(stack[0], SystemPurgeMiddleware)
    assert isinstance(stack[1], LeadingUserMiddleware)
    assert isinstance(stack[2], RollingCacheMarkMiddleware)
    assert stack[3] is _tool_error_middleware


def test_compile_nested_subagent_pins_response_format_to_tool_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    class M(BaseModel):
        x: int

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kwargs: calls.append(kwargs) or _FakeRunnable())
    child = ResolvedSubAgentSpec(name="leaf", description="l", system_prompt="sp", response_format=M)
    asyncio.run(
        factory._compile_nested_subagent(
            child, parent_model=_FAKE_LLM, parent_tools=[], store=InMemoryStore(), backend=object()
        )
    )
    # The nested leaf's schema is pinned to the tool-calling strategy, never
    # provider-dependent auto-routing.
    threaded = calls[-1]["response_format"]
    assert isinstance(threaded, ToolStrategy)
    # ...and bounded: an oversized int is a retryable parse failure, and the schema
    # binds under the model's name.
    _assert_bound_to_model(cast(ToolStrategy[Any], threaded), M)


def test_build_deep_agent_collapses_empty_subagents(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_create(*args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return "AGENT"

    monkeypatch.setattr(factory, "create_deep_agent", fake_create)
    agent = asyncio.run(
        build_deep_agent(
            llm=_FAKE_LLM,
            store=InMemoryStore(),
            checkpointer=InMemorySaver(),
            tools=[],
        )
    )
    assert agent == "AGENT"
    # No caller subagents: the general-purpose subagent is supplied explicitly so its
    # own tool node carries the shared tool-error middleware (deepagents would otherwise
    # auto-add it without that middleware).
    (gp,) = captured["subagents"]
    assert gp["name"] == "general-purpose"
    assert _tool_error_middleware in gp["middleware"]


def test_build_deep_agent_does_not_double_add_general_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_create(*args: object, **kwargs: object) -> str:
        captured.update(kwargs)
        return "AGENT"

    monkeypatch.setattr(factory, "create_deep_agent", fake_create)
    gp_spec = ResolvedSubAgentSpec(name="general-purpose", description="d", system_prompt="p")
    asyncio.run(
        build_deep_agent(
            llm=_FAKE_LLM, store=InMemoryStore(), checkpointer=InMemorySaver(), tools=[], subagents=[gp_spec]
        )
    )
    names = [s["name"] for s in captured["subagents"]]
    assert names.count("general-purpose") == 1
    # The caller's own general-purpose subagent already carries the shared middleware
    # (via _resolve_subagent), so no second one is injected.
    (caller_gp,) = [s for s in captured["subagents"] if s["name"] == "general-purpose"]
    assert _tool_error_middleware in caller_gp["middleware"]


def _injected_gp(captured: dict[str, Any]) -> dict[str, Any]:
    (gp,) = [s for s in captured["subagents"] if s["name"] == "general-purpose"]
    return cast(dict[str, Any], gp)


def test_general_purpose_subagent_inherits_skill_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    # deepagents' auto-added GP builds a SkillsMiddleware from the ``skills`` sources;
    # the explicit GP has no parent fallback, so it must carry the SAME sources the
    # level passes to create_deep_agent (an empty/None skill set sets no key).
    captured: dict[str, Any] = {}
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kwargs: captured.update(kwargs) or "AGENT")

    asyncio.run(build_deep_agent(llm=_FAKE_LLM, store=InMemoryStore(), checkpointer=InMemorySaver(), tools=[]))
    assert captured["skills"] is None
    assert "skills" not in _injected_gp(captured)

    captured.clear()
    skills = [f"{SKILLS_ROOT}jq/"]
    asyncio.run(
        build_deep_agent(llm=_FAKE_LLM, store=InMemoryStore(), checkpointer=InMemorySaver(), tools=[], skills=skills)
    )
    assert _injected_gp(captured)["skills"] == captured["skills"] == skills


def test_general_purpose_subagent_inherits_inline_skill_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    # inline_skills have no deepagents equivalent — they are flattened into
    # ``SKILLS_ROOT<name>/`` skill sources, and the GP inherits them exactly.
    captured: dict[str, Any] = {}
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kwargs: captured.update(kwargs) or "AGENT")

    asyncio.run(
        build_deep_agent(
            llm=_FAKE_LLM,
            store=InMemoryStore(),
            checkpointer=InMemorySaver(),
            tools=[],
            inline_skills=[InlineSkill(name="helper", content="# helper")],
        )
    )
    gp_skills = _injected_gp(captured)["skills"]
    assert gp_skills == captured["skills"]
    assert f"{SKILLS_ROOT}helper/" in gp_skills


def test_nested_general_purpose_subagent_inherits_child_skill_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    # The nested leaf's own auto-added GP inherits the child's skill sources too.
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kwargs: calls.append(kwargs) or _FakeRunnable())
    child = ResolvedSubAgentSpec(
        name="leaf",
        description="l",
        system_prompt="sp",
        skills=[f"{SKILLS_ROOT}jq/"],
        inline_skills=[InlineSkill(name="helper", content="# helper")],
    )
    asyncio.run(
        factory._compile_nested_subagent(
            child, parent_model=_FAKE_LLM, parent_tools=[], store=InMemoryStore(), backend=object()
        )
    )
    (gp,) = [s for s in calls[-1]["subagents"] if s["name"] == "general-purpose"]
    assert gp["skills"] == calls[-1]["skills"]
    assert f"{SKILLS_ROOT}jq/" in gp["skills"]
    assert f"{SKILLS_ROOT}helper/" in gp["skills"]


# --- nested subagents --------------------------------------------------------


def _nested_pair(
    child_tools: list[StructuredTool] | None = None,
    parent_tools: list[StructuredTool] | None = None,
) -> tuple[ResolvedSubAgentSpec, ResolvedSubAgentSpec]:
    child = ResolvedSubAgentSpec(name="finder", description="cd", system_prompt="cp", tools=child_tools or [])
    parent = ResolvedSubAgentSpec(
        name="advisor",
        description="d",
        system_prompt="p",
        tools=parent_tools or [],
        subagents=[child],
    )
    return parent, child


def test_validate_rejects_two_level_nesting() -> None:
    grandchild = ResolvedSubAgentSpec(name="g", description="gd", system_prompt="gp")
    child = ResolvedSubAgentSpec(name="c", description="cd", system_prompt="cp", subagents=[grandchild])
    parent = ResolvedSubAgentSpec(name="a", description="d", system_prompt="p", subagents=[child])
    with pytest.raises(ValueError, match="one level deep"):
        _validate([], [parent], None)


def test_validate_rejects_duplicate_nested_names() -> None:
    children = [
        ResolvedSubAgentSpec(name="c", description="cd", system_prompt="cp"),
        ResolvedSubAgentSpec(name="c", description="cd", system_prompt="cp"),
    ]
    parent = ResolvedSubAgentSpec(name="a", description="d", system_prompt="p", subagents=children)
    with pytest.raises(ValueError, match="duplicate nested subagent names"):
        _validate([], [parent], None)


def test_validate_rejects_nested_builtin_name() -> None:
    child = ResolvedSubAgentSpec(name="task", description="cd", system_prompt="cp")
    parent = ResolvedSubAgentSpec(name="a", description="d", system_prompt="p", subagents=[child])
    with pytest.raises(ValueError, match="built-in tool names"):
        _validate([], [parent], None)


def test_validate_rejects_nested_offroot_skill() -> None:
    child = ResolvedSubAgentSpec(name="c", description="cd", system_prompt="cp", skills=["/nope/"])
    parent = ResolvedSubAgentSpec(name="a", description="d", system_prompt="p", subagents=[child])
    with pytest.raises(ValueError, match="must start with"):
        _validate([], [parent], None)


def test_validate_accepts_clean_nested_config() -> None:
    child = ResolvedSubAgentSpec(name="c", description="cd", system_prompt="cp", skills=[f"{SKILLS_ROOT}finder/"])
    parent = ResolvedSubAgentSpec(name="a", description="d", system_prompt="p", subagents=[child])
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
    # ...and attached to the parent through a SubAgentMiddleware, ahead of the shared
    # tool-error middleware every subagent stack carries.
    sub_middleware, tool_error = sub["middleware"]
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
    child = ResolvedSubAgentSpec(name="finder", description="cd", system_prompt="cp", llm_provider="openai")
    parent = ResolvedSubAgentSpec(name="advisor", description="d", system_prompt="p", subagents=[child])
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
        build_deep_agent(
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
    assert isinstance(subs["advisor"]["middleware"][0], factory.SubAgentMiddleware)
    assert _tool_error_middleware in subs["general-purpose"]["middleware"]


# --- inline skills -----------------------------------------------------------


def _inline(name: str, content: str) -> InlineSkill:
    return InlineSkill(name=name, content=content)


def test_collect_inline_skills_spans_all_agents() -> None:
    """Inline skills from the main agent + subagent + nested merge into one map."""
    child = ResolvedSubAgentSpec(name="c", description="cd", system_prompt="cp", inline_skills=[_inline("child", "C")])
    parent = ResolvedSubAgentSpec(
        name="a",
        description="d",
        system_prompt="p",
        inline_skills=[_inline("parent", "P")],
        subagents=[child],
    )
    collected = _collect_inline_skills([_inline("main", "M")], [parent])
    assert collected == {"main": "M", "parent": "P", "child": "C"}


def test_collect_inline_skills_shared_name_identical_content_ok() -> None:
    """A name reused across agents with identical content collapses to one mount."""
    parent = ResolvedSubAgentSpec(name="a", description="d", system_prompt="p", inline_skills=[_inline("shared", "X")])
    collected = _collect_inline_skills([_inline("shared", "X")], [parent])
    assert collected == {"shared": "X"}


def test_collect_inline_skills_name_collision_differing_content_raises() -> None:
    parent = ResolvedSubAgentSpec(name="a", description="d", system_prompt="p", inline_skills=[_inline("dup", "B")])
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
        system_prompt="p",
        skills=[f"{SKILLS_ROOT}ref/"],
        inline_skills=[_inline("demo", "x")],
    )
    sub = cast(dict[str, Any], asyncio.run(_resolve_subagent(spec)))
    assert sub["skills"] == [f"{SKILLS_ROOT}ref/", f"{SKILLS_ROOT}demo/"]


def test_build_deep_agent_mounts_and_auto_loads_inline_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_deep_agent feeds inline content to the backend and auto-loads the path."""
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
        build_deep_agent(
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
    sub = ResolvedSubAgentSpec(name="b", description="d", system_prompt="p", inline_skills=[_inline("dup", "B")])
    with pytest.raises(ValueError, match="different content"):
        asyncio.run(
            build_deep_agent(
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
        build_deep_agent(
            llm=_FAKE_LLM,
            store=InMemoryStore(),
            checkpointer=InMemorySaver(),
        )
    )
    assert captured["skills"] is None


# --- rolling cache mark: two-turn wire proof ---------------------------------


class _RecordingChatModel(BaseChatModel):
    """Returns a fixed message per call and records the exact message list each model
    call received; ``bind_tools`` is a no-op so deepagents can bind its built-ins."""

    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)
    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self._seen.append(list(messages))
        message = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self


def _mark_count(messages: list[BaseMessage]) -> int:
    """Number of messages carrying a ``cache_control`` block (a cache breakpoint)."""
    return sum(
        isinstance(m.content, list) and any(isinstance(b, dict) and "cache_control" in b for b in m.content)
        for m in messages
    )


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
    config = {"configurable": {"thread_id": "t-deep-roll"}}

    async def run_two_turns() -> list[BaseMessage]:
        agent = await build_deep_agent(
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
