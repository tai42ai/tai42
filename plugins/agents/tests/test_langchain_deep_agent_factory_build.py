"""``langchain_deep_agent`` factory: subagent resolution, config validation, build
ordering, middleware wiring, and general-purpose subagent injection.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from langchain.agents.structured_output import ToolStrategy
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel
from tai42_contract.template import TemplatedText
from tai42_kit.llm.middleware.leading_user import LeadingUserMiddleware
from tai42_kit.llm.middleware.rolling_cache_mark import RollingCacheMarkMiddleware
from tai42_kit.llm.middleware.system_purge import SystemPurgeMiddleware
from tests._langchain_deep_agent_factory_support import (
    _FAKE_LLM,
    _assert_bound_to_model,
    _FakeRunnable,
    _injected_gp,
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
from tai42_agents.langchain_deep_agent.spec import InlineSkill, ResolvedSubAgentSpec


def test_resolve_subagent_emits_only_set_keys() -> None:
    """Inheritance relies on optional keys being ABSENT, not None; the shared async-park
    hook + tool-error middleware are the always-present stack entries every subagent gets."""
    spec = ResolvedSubAgentSpec(name="b", description="d", system_prompt=TemplatedText(content="p"))
    sub = cast(dict[str, Any], asyncio.run(_resolve_subagent(spec)))
    assert sub == {
        "name": "b",
        "description": "d",
        "system_prompt": "p",
        "middleware": [factory._async_park_middleware, _tool_error_middleware],
    }


def test_resolve_subagent_resolves_model_when_provider_set(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_llm_async(provider: str, **kwargs: object) -> str:
        return f"LLM:{provider}"

    monkeypatch.setattr(factory, "get_llm_async", fake_get_llm_async)
    spec = ResolvedSubAgentSpec(
        name="b", description="d", system_prompt=TemplatedText(content="p"), llm_provider="openai"
    )
    sub = cast(dict[str, Any], asyncio.run(_resolve_subagent(spec)))
    assert sub["model"] == "LLM:openai"


def test_resolve_subagent_passes_through_tools_skills_interrupt() -> None:
    spec = ResolvedSubAgentSpec(
        name="b",
        description="d",
        system_prompt=TemplatedText(content="p"),
        tools=[_tool("x")],
        skills=[f"{SKILLS_ROOT}jq/"],
        interrupt_on={"edit_file": True},
    )
    sub = cast(dict[str, Any], asyncio.run(_resolve_subagent(spec)))
    assert [t.name for t in sub["tools"]] == ["x"]
    assert sub["skills"] == [f"{SKILLS_ROOT}jq/"]
    assert sub["interrupt_on"] == {"edit_file": True}


def test_resolve_subagent_renders_a_stored_system_prompt(resource_manager: Any) -> None:
    """A by-id system prompt resolves through the manager at the point deepagents consumes it."""
    resource_manager.templates["sp-id"] = "stored instructions"
    spec = ResolvedSubAgentSpec(name="b", description="d", system_prompt=TemplatedText(id="sp-id"))
    sub = cast(dict[str, Any], asyncio.run(_resolve_subagent(spec)))
    assert sub["system_prompt"] == "stored instructions"


def test_resolve_subagent_missing_system_prompt_id_raises(resource_manager: Any) -> None:
    """A by-id system prompt whose resource is absent fails loudly at the point of use,
    never renders as empty text."""
    spec = ResolvedSubAgentSpec(name="b", description="d", system_prompt=TemplatedText(id="absent"))
    with pytest.raises(RuntimeError):
        asyncio.run(_resolve_subagent(spec))


def test_validate_rejects_duplicate_subagents() -> None:
    specs = [
        ResolvedSubAgentSpec(name="x", description="d", system_prompt=TemplatedText(content="p")),
        ResolvedSubAgentSpec(name="x", description="d", system_prompt=TemplatedText(content="p")),
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
    spec = ResolvedSubAgentSpec(name="task", description="d", system_prompt=TemplatedText(content="p"))
    with pytest.raises(ValueError, match="built-in tool names"):
        _validate([], [spec], None)


def test_validate_rejects_offroot_skill() -> None:
    with pytest.raises(ValueError, match="must start with"):
        _validate([], [], ["/wrong/x/"])


def test_validate_rejects_offroot_subagent_skill() -> None:
    spec = ResolvedSubAgentSpec(name="b", description="d", system_prompt=TemplatedText(content="p"), skills=["/nope/"])
    with pytest.raises(ValueError, match="must start with"):
        _validate([], [spec], None)


def test_validate_accepts_clean_config() -> None:
    spec = ResolvedSubAgentSpec(
        name="b", description="d", system_prompt=TemplatedText(content="p"), skills=[f"{SKILLS_ROOT}jq/"]
    )
    _validate([_tool("search")], [spec], [f"{SKILLS_ROOT}flow/"])  # no raise


def test_build_deep_agent_runs_validation_before_create(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation must reject bad config before reaching create_deep_agent."""
    called = {"create": False}

    def fake_create(*args: object, **kwargs: object) -> str:
        called["create"] = True
        return "AGENT"

    monkeypatch.setattr(factory, "create_deep_agent", fake_create)
    with pytest.raises(ValueError, match="must start with"):
        asyncio.run(
            build_langchain_deep_agent(
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

    spec = ResolvedSubAgentSpec(name="b", description="d", system_prompt=TemplatedText(content="p"), response_format=M)
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
        build_langchain_deep_agent(
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


def test_build_deep_agent_leads_with_async_park_then_system_purge_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The async-park hook leads the main agent's stack (the loop's first before_model
    # hook, so it recognizes a park before any compaction), followed by the system purge
    # so a thread whose stored history carries a system message runs cleanly: state never
    # reaches the model with one alongside the per-run prompt.
    captured: dict[str, Any] = {}
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kwargs: captured.update(kwargs) or "AGENT")
    asyncio.run(build_langchain_deep_agent(llm=_FAKE_LLM, store=InMemoryStore(), checkpointer=InMemorySaver()))
    assert captured["middleware"][0] is factory._async_park_middleware
    assert isinstance(captured["middleware"][1], SystemPurgeMiddleware)


def test_build_deep_agent_wires_rolling_cache_mark_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    # langchain_deep_agent honors user_content_kwargs (the mark rides its main user turn), so its
    # main-agent stack carries the rolling-cache-mark middleware too — a per-turn-marked
    # thread sends one breakpoint at the model call, same as every tools-agent face.
    captured: dict[str, Any] = {}
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kwargs: captured.update(kwargs) or "AGENT")
    asyncio.run(build_langchain_deep_agent(llm=_FAKE_LLM, store=InMemoryStore(), checkpointer=InMemorySaver()))
    assert any(isinstance(mw, RollingCacheMarkMiddleware) for mw in captured["middleware"])


def test_build_deep_agent_pins_main_middleware_order(monkeypatch: pytest.MonkeyPatch) -> None:
    # The main agent's stack is order-pinned: the async-park hook leads (the sole
    # before_model hook, so it sees a park before any compaction), system purge clears a
    # stored system message next, leading-user keeps the thread user-first,
    # rolling-cache-mark rolls the breakpoint at the call, and the shared tool-error
    # middleware trails last (mirroring every subagent stack's tool-error tail).
    captured: dict[str, Any] = {}
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kwargs: captured.update(kwargs) or "AGENT")
    asyncio.run(build_langchain_deep_agent(llm=_FAKE_LLM, store=InMemoryStore(), checkpointer=InMemorySaver()))
    stack = captured["middleware"]
    assert len(stack) == 5
    assert stack[0] is factory._async_park_middleware
    assert isinstance(stack[1], SystemPurgeMiddleware)
    assert isinstance(stack[2], LeadingUserMiddleware)
    assert isinstance(stack[3], RollingCacheMarkMiddleware)
    assert stack[4] is _tool_error_middleware


def test_compile_nested_subagent_pins_response_format_to_tool_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    class M(BaseModel):
        x: int

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kwargs: calls.append(kwargs) or _FakeRunnable())
    child = ResolvedSubAgentSpec(
        name="leaf", description="l", system_prompt=TemplatedText(content="sp"), response_format=M
    )
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
        build_langchain_deep_agent(
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
    gp_spec = ResolvedSubAgentSpec(name="general-purpose", description="d", system_prompt=TemplatedText(content="p"))
    asyncio.run(
        build_langchain_deep_agent(
            llm=_FAKE_LLM, store=InMemoryStore(), checkpointer=InMemorySaver(), tools=[], subagents=[gp_spec]
        )
    )
    names = [s["name"] for s in captured["subagents"]]
    assert names.count("general-purpose") == 1
    # The caller's own general-purpose subagent already carries the shared middleware
    # (via _resolve_subagent), so no second one is injected.
    (caller_gp,) = [s for s in captured["subagents"] if s["name"] == "general-purpose"]
    assert _tool_error_middleware in caller_gp["middleware"]


def test_general_purpose_subagent_inherits_skill_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    # deepagents' auto-added GP builds a SkillsMiddleware from the ``skills`` sources;
    # the explicit GP has no parent fallback, so it must carry the SAME sources the
    # level passes to create_deep_agent (an empty/None skill set sets no key).
    captured: dict[str, Any] = {}
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kwargs: captured.update(kwargs) or "AGENT")

    asyncio.run(
        build_langchain_deep_agent(llm=_FAKE_LLM, store=InMemoryStore(), checkpointer=InMemorySaver(), tools=[])
    )
    assert captured["skills"] is None
    assert "skills" not in _injected_gp(captured)

    captured.clear()
    skills = [f"{SKILLS_ROOT}jq/"]
    asyncio.run(
        build_langchain_deep_agent(
            llm=_FAKE_LLM, store=InMemoryStore(), checkpointer=InMemorySaver(), tools=[], skills=skills
        )
    )
    assert _injected_gp(captured)["skills"] == captured["skills"] == skills


def test_general_purpose_subagent_inherits_inline_skill_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    # inline_skills have no deepagents equivalent — they are flattened into
    # ``SKILLS_ROOT<name>/`` skill sources, and the GP inherits them exactly.
    captured: dict[str, Any] = {}
    monkeypatch.setattr(factory, "create_deep_agent", lambda **kwargs: captured.update(kwargs) or "AGENT")

    asyncio.run(
        build_langchain_deep_agent(
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
        system_prompt=TemplatedText(content="sp"),
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
