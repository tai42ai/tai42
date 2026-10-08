"""The compiled tools-agent graph cache: one graph per matching run inputs, per event loop.

``resolve_tools`` and ``_compile_tools_agent`` are replaced by counting doubles, so every case
counts exactly which calls compiled and which were served from the loop's cache.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr
from tai42_contract.agent.base import PresetSpec
from tai42_contract.secrets import SecretValue
from tai42_kit.settings import reset_all_settings

from tai42_agents._internal import graph_cache
from tai42_agents._internal.graph_cache import ToolsAgentGraphSpec, tools_agent_graph

from ._tools_agent_support import make_tool


class _Counts:
    def __init__(self) -> None:
        self.resolved = 0
        self.compiled = 0
        self.fail_next = False


@pytest.fixture
def counts(monkeypatch: pytest.MonkeyPatch, app_tools: Any) -> Iterator[_Counts]:
    counts = _Counts()

    async def fake_resolve(app_tools_: Any, names: list[str], tools: list[Any], presets: list[Any]) -> list[Any]:
        counts.resolved += 1
        return [*tools, *(make_tool(name) for name in names)]

    async def fake_compile(tools: list[Any], **kwargs: Any) -> tuple[Any, Any]:
        counts.compiled += 1
        if counts.fail_next:
            counts.fail_next = False
            raise RuntimeError("compile failed")
        # A compiled graph holds its loop's checkpointer, so the double keeps the loop referenced.
        return SimpleNamespace(loop=asyncio.get_running_loop()), SimpleNamespace(kind="strategy")

    monkeypatch.setattr(graph_cache, "resolve_tools", fake_resolve)
    monkeypatch.setattr(graph_cache, "_compile_tools_agent", fake_compile)
    graph_cache.reset_tools_agent_graphs()
    yield counts
    graph_cache.reset_tools_agent_graphs()


def _spec(**overrides: Any) -> ToolsAgentGraphSpec:
    fields: dict[str, Any] = {
        "tool_names": ("search",),
        "presets": (PresetSpec(name="p", base_tool="search", fixed_kwargs={"k": 1}),),
        "system_message": "be brief",
        "llm_provider": "llm-a",
        "llm_kwargs": {"temperature": 0},
        "checkpoint_provider": "memory",
    }
    fields.update(overrides)
    return ToolsAgentGraphSpec(**fields)


def _twice(first: ToolsAgentGraphSpec, second: ToolsAgentGraphSpec | None = None) -> tuple[Any, Any]:
    async def both() -> tuple[Any, Any]:
        return await tools_agent_graph(first), await tools_agent_graph(second or first)

    return asyncio.run(both())


def test_a_hit_returns_the_same_graph_and_skips_resolve_and_compile(counts: _Counts) -> None:
    one, two = _twice(_spec())

    assert one is two
    assert (counts.resolved, counts.compiled) == (1, 1)
    assert [tool.name for tool in one.tools] == ["search"]
    assert one.strategy.kind == "strategy"


def test_a_tool_surface_change_misses(counts: _Counts, app_tools: Any) -> None:
    async def run() -> tuple[Any, Any]:
        first = await tools_agent_graph(_spec())
        app_tools.client_tools["another"] = make_tool("another")
        return first, await tools_agent_graph(_spec())

    one, two = asyncio.run(run())

    assert one is not two
    assert counts.compiled == 2


def test_a_tool_removal_misses(counts: _Counts, app_tools: Any) -> None:
    app_tools.client_tools["search"] = make_tool("search")

    async def run() -> tuple[Any, Any]:
        first = await tools_agent_graph(_spec())
        del app_tools.client_tools["search"]
        return first, await tools_agent_graph(_spec())

    one, two = asyncio.run(run())

    assert one is not two
    assert counts.compiled == 2


def test_a_settings_reset_misses(counts: _Counts) -> None:
    async def run() -> tuple[Any, Any]:
        first = await tools_agent_graph(_spec())
        reset_all_settings()
        return first, await tools_agent_graph(_spec())

    one, two = asyncio.run(run())

    assert one is not two
    assert counts.compiled == 2


def test_a_client_epoch_advance_misses(counts: _Counts, monkeypatch: pytest.MonkeyPatch) -> None:
    epoch = {"value": 7}
    monkeypatch.setattr(graph_cache, "current_client_epoch", lambda: epoch["value"])

    async def run() -> tuple[Any, Any]:
        first = await tools_agent_graph(_spec())
        epoch["value"] += 1
        return first, await tools_agent_graph(_spec())

    one, two = asyncio.run(run())

    assert one is not two
    assert counts.compiled == 2


def test_the_debug_flag_is_part_of_the_key(counts: _Counts, monkeypatch: pytest.MonkeyPatch) -> None:
    debug = {"on": False}
    monkeypatch.setattr(
        graph_cache, "logging_settings", lambda: SimpleNamespace(is_enabled_for=lambda level: debug["on"])
    )

    async def run() -> tuple[Any, Any]:
        first = await tools_agent_graph(_spec())
        debug["on"] = True
        return first, await tools_agent_graph(_spec())

    one, two = asyncio.run(run())

    assert one is not two


@pytest.mark.parametrize(
    "change",
    [
        {"tool_names": ("other",)},
        {"presets": (PresetSpec(name="p", base_tool="search", fixed_kwargs={"k": 2}),)},
        {"system_message": "be verbose"},
        {"system_content_kwargs": {"cache_control": {"type": "ephemeral"}}},
        {"response_format": {"title": "Answer", "type": "object"}},
        {"llm_provider": "llm-b"},
        {"llm_kwargs": {"temperature": 1}},
        {"checkpoint_provider": "redis"},
    ],
)
def test_every_input_is_part_of_the_key(counts: _Counts, change: dict[str, Any]) -> None:
    one, two = _twice(_spec(), _spec(**change))

    assert one is not two
    assert counts.compiled == 2


def test_inputs_that_render_alike_as_json_stay_distinct(counts: _Counts) -> None:
    one, two = _twice(_spec(llm_kwargs={"stop": ["x"]}), _spec(llm_kwargs={"stop": ("x",)}))
    assert one is not two
    three, four = _twice(_spec(llm_kwargs={"n": 1}), _spec(llm_kwargs={"n": True}))
    assert three is not four


def test_a_pydantic_response_format_keys_on_the_class(counts: _Counts) -> None:
    class Answer(BaseModel):
        value: int

    class OtherAnswer(BaseModel):
        value: int

    one, two = _twice(_spec(response_format=Answer))
    three, _ = _twice(_spec(response_format=OtherAnswer))

    assert one is two
    assert three is not one
    assert one.response_format is Answer


def test_live_tools_bypass_the_cache(counts: _Counts) -> None:
    live = make_tool("live")
    one, two = _twice(_spec(live_tools=(live,)))

    assert one is not two
    assert counts.compiled == 2
    assert one.tools[0] is live


def test_a_dict_with_a_non_string_key_bypasses_the_cache(counts: _Counts) -> None:
    one, two = _twice(_spec(llm_kwargs={"logit_bias": {50256: -100}}))

    assert one is not two
    assert counts.compiled == 2


def test_an_unkeyable_input_bypasses_the_cache(counts: _Counts) -> None:
    one, two = _twice(_spec(llm_kwargs={"client": object()}))

    assert one is not two
    assert counts.compiled == 2


def test_a_failing_build_leaves_no_entry_and_the_next_call_builds_again(counts: _Counts) -> None:
    counts.fail_next = True

    async def run() -> tuple[Any, Any]:
        with pytest.raises(RuntimeError, match="compile failed"):
            await tools_agent_graph(_spec())
        first = await tools_agent_graph(_spec())
        return first, await tools_agent_graph(_spec())

    one, two = asyncio.run(run())

    assert one is two
    assert counts.compiled == 2


def test_two_loops_get_two_graphs(counts: _Counts) -> None:
    one = asyncio.run(tools_agent_graph(_spec()))
    two = asyncio.run(tools_agent_graph(_spec()))

    assert one is not two
    assert counts.compiled == 2


def test_a_closed_loops_graphs_are_not_retained(counts: _Counts) -> None:
    asyncio.run(tools_agent_graph(_spec()))
    asyncio.run(tools_agent_graph(_spec()))

    assert len(graph_cache._graphs) == 1


def test_the_oldest_graph_is_evicted_past_the_size(counts: _Counts, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph_cache, "agents_limits_settings", lambda: SimpleNamespace(graph_cache_size=2))

    async def run() -> list[Any]:
        a = await tools_agent_graph(_spec(system_message="a"))
        await tools_agent_graph(_spec(system_message="b"))
        await tools_agent_graph(_spec(system_message="a"))  # a becomes the newest
        await tools_agent_graph(_spec(system_message="c"))  # evicts b
        again_a = await tools_agent_graph(_spec(system_message="a"))
        await tools_agent_graph(_spec(system_message="b"))
        return [a, again_a]

    a, again_a = asyncio.run(run())

    assert a is again_a
    assert counts.compiled == 4


def test_secrets_enter_the_key_as_digests(counts: _Counts) -> None:
    secret = "sk-plaintext-value"
    key = graph_cache._spec_key(_spec(llm_kwargs={"api_key": SecretStr(secret), "token": SecretValue(secret)}))

    assert key is not None
    assert secret not in repr(key)
    one, two = _twice(_spec(llm_kwargs={"api_key": SecretStr(secret)}))
    assert one is two
    three, four = _twice(
        _spec(llm_kwargs={"api_key": SecretStr(secret)}), _spec(llm_kwargs={"api_key": SecretStr("x")})
    )
    assert three is not four


def test_the_default_size_setting_is_sixty_four() -> None:
    from tai42_agents.settings import AgentsLimitsSettings

    assert AgentsLimitsSettings().graph_cache_size == 64


def test_two_runs_of_the_run_face_on_one_loop_compile_once(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class _ToolBindingFake(FakeMessagesListChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

    from langgraph.checkpoint.memory import InMemorySaver
    from tai42_contract.template import TemplatedText

    from tai42_agents._internal import base_tool_agent as bta
    from tai42_agents.tools_agent import ToolsAgent

    from .conftest import fake_run_trace

    app_tools.client_tools["search"] = make_tool("search")
    model = _ToolBindingFake(responses=[AIMessage(content="first"), AIMessage(content="second")])
    saver = InMemorySaver()

    async def fake_llm(*, provider: str, **kwargs: Any) -> Any:
        return model

    async def fake_checkpointer(*, provider: str, conn_string: Any) -> Any:
        return saver

    async def no_overflow(*, system_prompt: Any) -> list[Any]:
        return []

    monkeypatch.setattr(bta, "get_llm_async", fake_llm)
    monkeypatch.setattr(bta, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=fake_checkpointer))
    monkeypatch.setattr(bta, "context_overflow_middlewares", no_overflow)
    monkeypatch.setattr(bta, "init_langgraph_config", lambda config: fake_run_trace(config))
    compiles: list[int] = []
    real_compile = graph_cache._compile_tools_agent

    async def counting_compile(*args: Any, **kwargs: Any) -> Any:
        compiles.append(1)
        return await real_compile(*args, **kwargs)

    monkeypatch.setattr(graph_cache, "_compile_tools_agent", counting_compile)
    agent = ToolsAgent()

    async def two_runs() -> list[Any]:
        return [
            await agent.run(
                tool_names=["search"],
                system_message=TemplatedText(content="be brief"),
                user_message=TemplatedText(content=f"m{i}"),
                thread_id=f"t{i}",
            )
            for i in range(2)
        ]

    assert asyncio.run(two_runs()) == ["first", "second"]
    assert len(compiles) == 1
