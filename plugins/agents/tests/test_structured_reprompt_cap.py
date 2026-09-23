"""The structured-output re-prompt cap and the two typed, non-fatal run outcomes.

Every case drives a REAL compiled ``create_agent`` graph (the retry rail langchain
appends the re-prompt tool message on) with a scripted chat model, so the cap
counter installed by :func:`~tai42_agents._internal.structured.as_tool_strategy`
and the outcome each face surfaces are exercised through the true compile/invoke
paths — no mock of the rail itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr
from tai42_contract.agent.events import RecursionLimitFinal, StructuredOutputUnresolvedFinal

from tai42_agents._internal import base_tool_agent as bta
from tai42_agents._internal.stream_events import aproject_agent_events
from tai42_agents._internal.structured import as_tool_strategy
from tai42_agents._internal.usage import AgentInvokeResult

_SCHEMA = {
    "title": "Answer",
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
}


class _ScriptedModel(BaseChatModel):
    """Emits scripted AIMessages and counts each model call.

    ``bind_tools`` is a no-op so the graph binds its structured/loop tool yet the
    responses stay fixed. Once the script is exhausted the LAST message repeats, so a
    never-conforming run keeps producing the same off-schema structured tool call.
    """

    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    calls: int = 0

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=self._responses[index])])


def _answer_call(value: Any, call_id: str = "c") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "Answer", "args": {"value": value}}])


def _loop_call(call_id: str = "c") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "loop", "args": {}}])


def _loop_tool() -> StructuredTool:
    async def loop() -> str:
        return "again"

    return StructuredTool.from_function(func=None, coroutine=loop, name="loop", description="loops")


def _compile(model: BaseChatModel, tools: list[StructuredTool], schema: Any) -> tuple[Any, Any]:
    strategy = as_tool_strategy(schema)
    graph = create_agent(model, tools=tools, checkpointer=InMemorySaver(), response_format=strategy)
    return graph, strategy


def _run_config(recursion_limit: int = 50) -> dict[str, Any]:
    return {"configurable": {"thread_id": "t"}, "recursion_limit": recursion_limit}


async def _project(graph: Any, strategy: Any, schema: Any, config: dict[str, Any]) -> list[Any]:
    return [
        event
        async for event in aproject_agent_events(
            graph,
            {"messages": [HumanMessage(content="go")]},
            config,
            response_format=schema,
            structured_strategy=strategy,
        )
    ]


# --------------------------------------------------------------------------
# The cap counter (streaming projection — the seam every face shares)
# --------------------------------------------------------------------------


def test_never_conforming_run_yields_outcome_after_exactly_cap_reprompts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tai42_agents._internal.structured.agents_limits_settings",
        lambda: SimpleNamespace(structured_output_reprompt_cap=3),
    )
    model = _ScriptedModel([_answer_call("not-an-int")])
    graph, strategy = _compile(model, [], _SCHEMA)

    events = asyncio.run(_project(graph, strategy, _SCHEMA, _run_config()))

    terminal = events[-1]
    assert isinstance(terminal, StructuredOutputUnresolvedFinal)
    assert terminal.final is True
    assert terminal.schema_name == "Answer"
    # cap=3 re-prompts, so the 4th non-conforming model response trips the cap: the model
    # is called exactly cap + 1 times and the outcome names that same attempt count.
    assert model.calls == 4
    assert terminal.attempts == 4
    assert terminal.error


def test_conforming_on_second_try_succeeds_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tai42_agents._internal.structured.agents_limits_settings",
        lambda: SimpleNamespace(structured_output_reprompt_cap=3),
    )
    model = _ScriptedModel([_answer_call("nope"), _answer_call(7)])
    graph, strategy = _compile(model, [], _SCHEMA)

    events = asyncio.run(_project(graph, strategy, _SCHEMA, _run_config()))

    assert not any(isinstance(event, StructuredOutputUnresolvedFinal | RecursionLimitFinal) for event in events)
    assert model.calls == 2
    structured = [event for event in events if getattr(event, "type", "") == "structured_final"]
    assert structured
    assert structured[-1].data == {"value": 7}


def test_recursion_trip_yields_named_outcome() -> None:
    model = _ScriptedModel([_loop_call()])
    graph, strategy = _compile(model, [_loop_tool()], _SCHEMA)

    events = asyncio.run(_project(graph, strategy, _SCHEMA, _run_config(recursion_limit=4)))

    terminal = events[-1]
    assert isinstance(terminal, RecursionLimitFinal)
    assert terminal.final is True
    assert terminal.limit == 4


# --------------------------------------------------------------------------
# The invoke face (tools_agent run door)
# --------------------------------------------------------------------------


def _patch_invoke_seams(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, recursion_limit: int) -> None:
    monkeypatch.setattr(
        "tai42_agents._internal.structured.agents_limits_settings",
        lambda: SimpleNamespace(structured_output_reprompt_cap=3),
    )
    monkeypatch.setattr(
        bta, "llm_provider_settings", lambda: SimpleNamespace(llm="p", checkpoint="c", checkpoint_conn_string="s")
    )
    monkeypatch.setattr(bta, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))

    async def _get_llm(*, provider: str, **kwargs: Any) -> BaseChatModel:
        return model

    monkeypatch.setattr(bta, "get_llm_async", _get_llm)

    saver = InMemorySaver()

    async def _get_checkpointer(*, provider: str, conn_string: str) -> Any:
        return saver

    monkeypatch.setattr(bta, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=_get_checkpointer))

    async def _no_overflow(*, system_prompt: Any) -> list[Any]:
        return []

    monkeypatch.setattr(bta, "context_overflow_middlewares", _no_overflow)
    monkeypatch.setattr(bta, "logging_settings", lambda: SimpleNamespace(is_enabled_for=lambda level: False))
    monkeypatch.setattr(
        bta,
        "init_langgraph_config",
        lambda config: {"configurable": {"thread_id": "t"}, "recursion_limit": recursion_limit},
    )


def test_invoke_face_returns_reprompt_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _ScriptedModel([_answer_call("bad")])
    _patch_invoke_seams(monkeypatch, model, recursion_limit=50)

    result = asyncio.run(
        bta.ainvoke_tools_agent(system_message="", user_message=["go"], tools=[], response_format=_SCHEMA)
    )

    assert isinstance(result, AgentInvokeResult)
    assert isinstance(result.outcome, StructuredOutputUnresolvedFinal)
    assert result.outcome.attempts == 4
    assert model.calls == 4


def test_invoke_face_returns_recursion_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _ScriptedModel([_loop_call()])
    _patch_invoke_seams(monkeypatch, model, recursion_limit=4)

    result = asyncio.run(
        bta.ainvoke_tools_agent(system_message="", user_message=["go"], tools=[_loop_tool()], response_format=_SCHEMA)
    )

    assert isinstance(result.outcome, RecursionLimitFinal)
    assert result.outcome.limit == 4


def test_setting_default_cap_is_three() -> None:
    from tai42_agents.settings import AgentsLimitsSettings

    assert AgentsLimitsSettings().structured_output_reprompt_cap == 3


# --------------------------------------------------------------------------
# The deep-agent door (its streaming core is the same projection seam)
# --------------------------------------------------------------------------


def test_deep_agent_never_conforming_yields_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph.store.memory import InMemoryStore

    from tai42_agents.langchain_deep_agent.factory import build_langchain_deep_agent

    monkeypatch.setattr(
        "tai42_agents._internal.structured.agents_limits_settings",
        lambda: SimpleNamespace(structured_output_reprompt_cap=3),
    )
    model = _ScriptedModel([_answer_call("bad")])
    graph = asyncio.run(
        build_langchain_deep_agent(
            llm=model, store=InMemoryStore(), checkpointer=InMemorySaver(), tools=[], response_format=_SCHEMA
        )
    )
    events = asyncio.run(_project(graph, as_tool_strategy(_SCHEMA), _SCHEMA, _run_config()))

    assert isinstance(events[-1], StructuredOutputUnresolvedFinal)
    assert events[-1].attempts == 4
    assert model.calls == 4


# --------------------------------------------------------------------------
# The refine door (its final structured pass streams the projection; its
# evaluator/critic loop maps a recursion trip to the named outcome)
# --------------------------------------------------------------------------


def _drive_refine(agent: Any, **kwargs: Any) -> list[Any]:
    async def go() -> list[Any]:
        return [event async for event in agent.astream(**kwargs)]

    return asyncio.run(go())


def test_refine_final_pass_outcome_passes_through_without_the_missing_structured_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_contract.app import tai42_app

    from tai42_agents.refine_agent import agent as refine_mod

    agent = tai42_app.agents.get_agent("refine_agent")
    monkeypatch.setattr(refine_mod, "as_tool_strategy", lambda response_format: None)

    async def _fake_loop(**_kwargs: Any) -> tuple[Any, Any, Any]:
        return object(), {}, {}

    monkeypatch.setattr(refine_mod, "_run_refine_loop", _fake_loop)

    async def _fake_project(*_args: Any, **_kwargs: Any):
        yield StructuredOutputUnresolvedFinal(schema_name="Answer", attempts=4, error="bad")

    monkeypatch.setattr(refine_mod, "aproject_agent_events", _fake_project)

    # response_format set + no StructuredFinal would normally raise; the outcome suppresses it.
    events = _drive_refine(agent, response_format={"title": "Answer", "type": "object"})
    assert len(events) == 1
    assert isinstance(events[0], StructuredOutputUnresolvedFinal)


def test_refine_loop_recursion_trip_yields_named_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph.errors import GraphRecursionError
    from tai42_contract.app import tai42_app

    from tai42_agents.refine_agent import agent as refine_mod

    agent = tai42_app.agents.get_agent("refine_agent")
    monkeypatch.setattr(refine_mod, "as_tool_strategy", lambda response_format: None)

    async def _boom(**_kwargs: Any) -> tuple[Any, Any, Any]:
        raise GraphRecursionError("Recursion limit of 4 reached")

    monkeypatch.setattr(refine_mod, "_run_refine_loop", _boom)

    events = _drive_refine(agent)
    assert len(events) == 1
    assert isinstance(events[0], RecursionLimitFinal)
