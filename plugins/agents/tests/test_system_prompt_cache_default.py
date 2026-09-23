"""The default-on system-prompt cache marking and its per-node opt-out.

Every assertion drives the REAL compile / build seam of a face; the model,
checkpointer and store are the only stubs. The suite-wide autouse fixture
defaults the marking off, so each test here sets the setting itself (via the
``cache_mark`` seam) and names a real provider, since the mark form comes from
the kit provider capability.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import SystemMessage
from langchain_core.tools import StructuredTool
from tai42_contract.template import TemplatedText

from tai42_agents._internal import base_tool_agent as bta
from tai42_agents._internal import cache_mark
from tai42_agents.langchain_deep_agent import agent as deep_mod
from tai42_agents.refine_agent import agent as refine_mod
from tai42_agents.retrieval_tools_agent import agent as retrieval_mod
from tai42_agents.settings import AgentsLimitsSettings
from tai42_agents.voting_agent import agent as voting_mod

_MARK = {"cache_control": {"type": "ephemeral"}}


def _set_default(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    """Point the shared cache-mark seam at a settings double with the given default."""
    monkeypatch.setattr(cache_mark, "agents_limits_settings", lambda: SimpleNamespace(system_prompt_cache_default=on))


def _marked_block(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text, **_MARK}]


# --- the setting default and the shared decision seam -------------------------


def test_setting_defaults_on() -> None:
    """The server-wide default is on out of the box, no env needed."""
    assert AgentsLimitsSettings().system_prompt_cache_default is True


def test_default_mark_on_for_a_marking_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_default(monkeypatch, on=True)
    assert cache_mark.default_system_cache_mark("anthropic") == _MARK


def test_default_mark_none_for_a_non_marking_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_default(monkeypatch, on=True)
    assert cache_mark.default_system_cache_mark("openai") is None


def test_default_mark_none_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_default(monkeypatch, on=False)
    assert cache_mark.default_system_cache_mark("anthropic") is None


def test_default_mark_raises_on_an_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown provider propagates loudly from the capability, never a silent no-mark."""
    _set_default(monkeypatch, on=True)
    with pytest.raises(ValueError, match="Unsupported chat model provider"):
        cache_mark.default_system_cache_mark("nope")


# --- the tools-agent chokepoint (covers invoke, SSE, and presets over agents) -


def _tool(name: str) -> StructuredTool:
    async def _run(**_: Any) -> str:
        return "ok"

    return StructuredTool.from_function(func=None, coroutine=_run, name=name, description="d")


def _compile_capture(monkeypatch: pytest.MonkeyPatch, *, provider: str, **compile_kwargs: Any) -> Any:
    """Run the REAL ``_compile_tools_agent`` with the model/checkpointer/middleware
    seams stubbed, and return the ``system_prompt`` it handed ``create_agent``."""
    monkeypatch.setattr(
        bta,
        "llm_provider_settings",
        lambda: SimpleNamespace(llm=provider, checkpoint="cp", checkpoint_conn_string=None),
    )
    monkeypatch.setattr(bta, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))
    monkeypatch.setattr(bta, "get_llm_async", AsyncMock(return_value="llm"))
    monkeypatch.setattr(
        bta, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=AsyncMock(return_value="cp"))
    )
    monkeypatch.setattr(bta, "context_overflow_middlewares", AsyncMock(return_value=[]))
    monkeypatch.setattr(bta, "logging_settings", lambda: SimpleNamespace(is_enabled_for=lambda level: False))
    captured: dict[str, Any] = {}

    def fake_create_agent(llm: Any, *, system_prompt: Any = None, **_: Any) -> Any:
        captured["system_prompt"] = system_prompt
        return MagicMock()

    monkeypatch.setattr(bta, "create_agent", fake_create_agent)
    asyncio.run(bta._compile_tools_agent([_tool("s")], system_message="sys", **compile_kwargs))
    return captured["system_prompt"]


def test_chokepoint_marks_by_default_for_a_marking_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_default(monkeypatch, on=True)
    system_prompt = _compile_capture(monkeypatch, provider="anthropic")
    assert isinstance(system_prompt, SystemMessage)
    assert system_prompt.content == _marked_block("sys")


def test_chokepoint_does_not_mark_for_a_non_marking_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_default(monkeypatch, on=True)
    system_prompt = _compile_capture(monkeypatch, provider="openai")
    assert isinstance(system_prompt, SystemMessage)
    assert system_prompt.content == "sys"


def test_empty_system_content_kwargs_opts_the_node_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """``{}`` is the explicit per-node opt-out — no mark even for a marking provider,
    while an unset value takes the default."""
    _set_default(monkeypatch, on=True)
    opted_out = _compile_capture(monkeypatch, provider="anthropic", system_content_kwargs={})
    assert opted_out.content == "sys"

    defaulted = _compile_capture(monkeypatch, provider="anthropic", system_content_kwargs=None)
    assert defaulted.content == _marked_block("sys")


def test_explicit_mark_passes_through_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_default(monkeypatch, on=True)
    explicit = {"cache_control": {"type": "ephemeral"}, "extra": "x"}
    system_prompt = _compile_capture(monkeypatch, provider="openai", system_content_kwargs=explicit)
    assert system_prompt.content == [{"type": "text", "text": "sys", **explicit}]


def test_setting_off_marks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_default(monkeypatch, on=False)
    system_prompt = _compile_capture(monkeypatch, provider="anthropic")
    assert system_prompt.content == "sys"


# --- refine: default at each role's model build -------------------------------


def _refine_role_prompt(monkeypatch: pytest.MonkeyPatch, *, provider: str) -> Any:
    captured: dict[str, Any] = {}

    def fake_create_agent(llm: Any, *, system_prompt: Any = None, **_: Any) -> Any:
        captured["system_prompt"] = system_prompt
        return MagicMock()

    monkeypatch.setattr(refine_mod, "create_agent", fake_create_agent)
    monkeypatch.setattr(refine_mod, "context_overflow_middlewares", AsyncMock(return_value=[]))
    asyncio.run(
        refine_mod._build_role_agent(
            MagicMock(), [], "role-prompt", MagicMock(), provider=provider, is_enabled_for_debug=False
        )
    )
    return captured["system_prompt"]


def test_refine_role_marks_by_default_for_a_marking_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_default(monkeypatch, on=True)
    system_prompt = _refine_role_prompt(monkeypatch, provider="anthropic")
    assert isinstance(system_prompt, SystemMessage)
    assert system_prompt.content == _marked_block("role-prompt")


def test_refine_role_unmarked_for_a_non_marking_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_default(monkeypatch, on=True)
    system_prompt = _refine_role_prompt(monkeypatch, provider="openai")
    assert system_prompt.content == "role-prompt"


# --- deep agent: default at the model build -----------------------------------


def _deep_system_prompt(monkeypatch: pytest.MonkeyPatch, *, provider: str) -> Any:
    captured: dict[str, Any] = {}

    async def fake_build(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(deep_mod, "build_langchain_deep_agent", fake_build)
    monkeypatch.setattr(deep_mod, "get_llm_async", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        deep_mod, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=AsyncMock(return_value=object()))
    )
    monkeypatch.setattr(deep_mod, "store_registry", lambda: SimpleNamespace(get_store=AsyncMock(return_value=object())))
    monkeypatch.setattr(
        deep_mod,
        "llm_provider_settings",
        lambda: SimpleNamespace(
            llm=provider, checkpoint="cp", store="st", checkpoint_conn_string=None, store_conn_string=None
        ),
    )
    monkeypatch.setattr(deep_mod, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: {}))
    asyncio.run(
        deep_mod.DeepAgent()._resolve_and_build(
            tools=[],
            subagents=[],
            skills=None,
            inline_skills=None,
            system_message="deep-sys",
            response_format=None,
            interrupt_on=None,
            llm_provider=provider,
            checkpoint_provider=None,
            store_provider=None,
            llm_kwargs=None,
        )
    )
    return captured["system_prompt"]


def test_deep_agent_marks_by_default_for_a_marking_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_default(monkeypatch, on=True)
    system_prompt = _deep_system_prompt(monkeypatch, provider="anthropic")
    assert isinstance(system_prompt, SystemMessage)
    assert system_prompt.content == _marked_block("deep-sys")


def test_deep_agent_unmarked_for_a_non_marking_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_default(monkeypatch, on=True)
    system_prompt = _deep_system_prompt(monkeypatch, provider="openai")
    assert system_prompt == "deep-sys"


# --- retrieval: documented exclusion ------------------------------------------


def test_retrieval_never_marks_its_system_prompt(monkeypatch: pytest.MonkeyPatch, resource_manager: Any) -> None:
    """Retrieval prepends the system prompt into the list it rolls to the newest mark,
    so it is excluded from the default: its composed prompt reaches the graph as a plain
    string even for a marking provider under the default on."""
    _set_default(monkeypatch, on=True)
    captured: dict[str, Any] = {}

    class _Graph:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def abuild(self) -> Any:
            return MagicMock()

    monkeypatch.setattr(retrieval_mod, "RetrievalToolsGraph", _Graph)
    monkeypatch.setattr(
        retrieval_mod,
        "llm_provider_settings",
        lambda: SimpleNamespace(
            llm="anthropic",
            embedding="emb",
            checkpoint="cp",
            store="st",
            store_conn_string=None,
            checkpoint_conn_string=None,
        ),
    )
    monkeypatch.setattr(retrieval_mod, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: {}))
    monkeypatch.setattr(retrieval_mod, "embedding_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: {}))
    monkeypatch.setattr(retrieval_mod, "get_llm_async", AsyncMock(return_value="llm"))
    monkeypatch.setattr(retrieval_mod, "get_embedding_async", AsyncMock(return_value="emb"))
    monkeypatch.setattr(retrieval_mod, "resolve_tools", AsyncMock(return_value=["t"]))
    monkeypatch.setattr(retrieval_mod, "_embedding_dims", AsyncMock(return_value=3))
    monkeypatch.setattr(retrieval_mod, "store_registry", lambda: SimpleNamespace(get_store=AsyncMock(return_value="s")))
    monkeypatch.setattr(
        retrieval_mod,
        "checkpoint_registry",
        lambda: SimpleNamespace(get_checkpointer=AsyncMock(return_value="cp")),
    )
    monkeypatch.setattr(retrieval_mod, "_repair_dangling_tool_calls", AsyncMock())
    monkeypatch.setattr(retrieval_mod, "init_langgraph_config", lambda config=None: dict(config or {}))

    agent = retrieval_mod.RetrievalToolsAgent()
    asyncio.run(agent._build(system_message=TemplatedText(content="rag"), user_message=TemplatedText(content="q")))
    assert isinstance(captured["system_prompt"], str)
    assert "cache_control" not in captured["system_prompt"]


# --- voting: inherits the chokepoint default (its voters/judge route through it) ---


def test_voting_voters_route_through_the_chokepoint_without_overriding_the_default(
    monkeypatch: pytest.MonkeyPatch, resource_manager: Any
) -> None:
    """Voting builds no own graph — each voter runs through ``ainvoke_tools_agent``,
    the tools-agent chokepoint, and passes no system_content_kwargs, so it inherits
    the default there."""
    from tai42_agents._internal.usage import AgentInvokeResult, CallUsage
    from tai42_agents.voting_agent.model import VoterSpec

    calls: list[dict[str, Any]] = []

    async def fake_invoke(**kwargs: Any) -> AgentInvokeResult:
        calls.append(kwargs)
        return AgentInvokeResult(output="v", usage=CallUsage(input_tokens=0, output_tokens=0, model=None))

    monkeypatch.setattr(voting_mod, "ainvoke_tools_agent", fake_invoke)
    asyncio.run(
        voting_mod._run_voters(
            judge_message=TemplatedText(content="judge"),
            voter_message=TemplatedText(content="vote"),
            judge_llm_provider="openai",
            judge_llm_kwargs=None,
            voters=[VoterSpec(provider="openai")],
            voter_tools=[],
            checkpoint_provider=None,
            voter_config=None,
        )
    )
    assert calls
    assert "system_content_kwargs" not in calls[0]
