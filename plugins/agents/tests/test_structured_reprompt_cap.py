"""The structured-output re-prompt cap and the two typed, non-fatal run outcomes.

Every case drives a REAL compiled ``create_agent`` graph with a scripted chat model
and the platform ``StructuredOutputRailMiddleware`` appended by
:func:`~tai42_agents._internal.structured.structured_output_stack`, so the per-run cap
counter and the outcome each face surfaces are exercised through the true
compile/invoke paths — no mock of the rail itself. The scripted model carries no
profile, so the plan takes the tool tier (the bounded ``TypedDict`` parse is the
in-node failure the rail re-prompts on).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, Union

import pytest
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, PrivateAttr
from tai42_contract.agent.events import RecursionLimitFinal, StructuredFinal, StructuredOutputUnresolvedFinal

from tai42_agents._internal import base_tool_agent as bta
from tai42_agents._internal.stream_events import aproject_agent_events
from tai42_agents._internal.structured import structured_output_stack
from tai42_agents._internal.usage import AgentInvokeResult

from ._client_tool import FakeClientTool, client_tool
from ._graph_support import invoke_tools_agent
from .conftest import fake_init_langgraph_config

_SCHEMA = {
    "title": "Answer",
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
}


class _ScriptedModel(BaseChatModel):
    """Emits scripted AIMessages, counts each model call, and records each call's messages.

    ``bind_tools`` is a no-op so the graph binds its structured/loop tool yet the
    responses stay fixed. Once the script is exhausted the LAST message repeats, so a
    never-conforming run keeps producing the same off-schema structured tool call.
    """

    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)
    calls: int = 0

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def seen(self) -> list[list[BaseMessage]]:
        return self._seen

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self._seen.append(list(messages))
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


def _loop_handle() -> FakeClientTool:
    async def loop() -> str:
        return "again"

    return client_tool("loop", loop, description="loops")


def _compile(model: BaseChatModel, tools: list[StructuredTool], schema: Any) -> tuple[Any, Any]:
    strategy, rail = structured_output_stack(model, "openai", schema)
    assert rail is not None
    graph = create_agent(model, tools=tools, checkpointer=InMemorySaver(), response_format=strategy, middleware=[rail])
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
        bta,
        "llm_provider_settings",
        lambda: SimpleNamespace(llm="anthropic", checkpoint="c", checkpoint_conn_string="s"),
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
        lambda config, _context: fake_init_langgraph_config(
            {"configurable": {"thread_id": "t"}, "recursion_limit": recursion_limit}
        ),
    )


def test_invoke_face_returns_reprompt_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _ScriptedModel([_answer_call("bad")])
    _patch_invoke_seams(monkeypatch, model, recursion_limit=50)

    result = asyncio.run(invoke_tools_agent(system_message="", user_message=["go"], tools=[], response_format=_SCHEMA))

    assert isinstance(result, AgentInvokeResult)
    assert isinstance(result.outcome, StructuredOutputUnresolvedFinal)
    assert result.outcome.attempts == 4
    assert model.calls == 4


def test_invoke_face_returns_recursion_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _ScriptedModel([_loop_call()])
    _patch_invoke_seams(monkeypatch, model, recursion_limit=4)

    result = asyncio.run(
        invoke_tools_agent(system_message="", user_message=["go"], tools=[_loop_handle()], response_format=_SCHEMA)
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
            llm=model,
            store=InMemoryStore(),
            checkpointer=InMemorySaver(),
            provider="openai",
            tools=[],
            response_format=_SCHEMA,
        )
    )
    strategy, _rail = structured_output_stack(model, "openai", _SCHEMA)
    events = asyncio.run(_project(graph, strategy, _SCHEMA, _run_config()))

    assert isinstance(events[-1], StructuredOutputUnresolvedFinal)
    assert events[-1].attempts == 4
    assert model.calls == 4


# --------------------------------------------------------------------------
# A final turn with no structured answer: no structured tool call and no other
# tool call (empty content, or the answer written as message text)
# --------------------------------------------------------------------------

_NO_ANSWER_TURNS = {
    "empty": AIMessage(content=""),
    "plain_json": AIMessage(content='{"value": 7}'),
    "fenced_json": AIMessage(content='```json\n{"value": 7}\n```'),
}


def _set_cap(monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    monkeypatch.setattr(
        "tai42_agents._internal.structured.agents_limits_settings",
        lambda: SimpleNamespace(structured_output_reprompt_cap=cap),
    )


@pytest.mark.parametrize("turn", list(_NO_ANSWER_TURNS.values()), ids=list(_NO_ANSWER_TURNS))
def test_final_turn_without_the_structured_answer_is_reprompted(
    monkeypatch: pytest.MonkeyPatch, turn: AIMessage
) -> None:
    _set_cap(monkeypatch, 3)
    model = _ScriptedModel([turn, _answer_call(7)])
    graph, strategy = _compile(model, [], _SCHEMA)

    events = asyncio.run(_project(graph, strategy, _SCHEMA, _run_config()))

    assert [event.data for event in events if isinstance(event, StructuredFinal)] == [{"value": 7}]
    assert model.calls == 2
    # The second call carries the offending turn and the re-prompt in the rail's feedback shape,
    # naming the structured tool the answer must be returned through.
    feedback = model.seen[1][-1]
    assert isinstance(feedback, HumanMessage)
    assert isinstance(feedback.content, str)
    assert feedback.content.startswith("Error: ")
    assert feedback.content.endswith("Please fix your mistakes.")
    assert "'Answer'" in feedback.content


@pytest.mark.parametrize("turn", list(_NO_ANSWER_TURNS.values()), ids=list(_NO_ANSWER_TURNS))
def test_final_turn_never_carrying_the_structured_answer_ends_on_the_cap_outcome(
    monkeypatch: pytest.MonkeyPatch, turn: AIMessage
) -> None:
    _set_cap(monkeypatch, 3)
    model = _ScriptedModel([turn])
    graph, strategy = _compile(model, [], _SCHEMA)

    events = asyncio.run(_project(graph, strategy, _SCHEMA, _run_config()))

    terminal = events[-1]
    assert isinstance(terminal, StructuredOutputUnresolvedFinal)
    assert terminal.schema_name == "Answer"
    assert terminal.attempts == 4
    assert model.calls == 4
    assert "'Answer'" in terminal.error


def test_interim_tool_call_turn_passes_the_rail_unjudged(monkeypatch: pytest.MonkeyPatch) -> None:
    # A cap of zero: any judged turn would trip it, so the run succeeding proves the
    # interim turn calling another tool was passed through without a re-prompt.
    _set_cap(monkeypatch, 0)
    model = _ScriptedModel([_loop_call(), _answer_call(7)])
    graph, strategy = _compile(model, [_loop_tool()], _SCHEMA)

    events = asyncio.run(_project(graph, strategy, _SCHEMA, _run_config()))

    assert [event.data for event in events if isinstance(event, StructuredFinal)] == [{"value": 7}]
    assert model.calls == 2
    assert not any(isinstance(message, HumanMessage) for message in model.seen[1][1:])


def test_broken_structured_tool_call_is_answered_and_reprompted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arguments that are not valid JSON parse to an invalid tool call, so the turn carries no
    # tool call at all. An OpenAI-compatible request sends invalid tool calls back as tool
    # calls, so the re-prompt answers each one with a ToolMessage.
    _set_cap(monkeypatch, 3)
    broken = AIMessage(
        content="",
        invalid_tool_calls=[
            {"type": "invalid_tool_call", "id": "bad", "name": "Answer", "args": '{"value": ', "error": "malformed"}
        ],
    )
    model = _ScriptedModel([broken, _answer_call(7)])
    graph, strategy = _compile(model, [], _SCHEMA)

    events = asyncio.run(_project(graph, strategy, _SCHEMA, _run_config()))

    assert [event.data for event in events if isinstance(event, StructuredFinal)] == [{"value": 7}]
    answers = [message for message in model.seen[1] if isinstance(message, ToolMessage)]
    assert [answer.tool_call_id for answer in answers] == ["bad"]
    assert "'Answer'" in str(answers[0].content)


def test_parallel_turn_with_a_broken_structured_call_is_judged_and_every_call_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One parallel turn: a valid call to another tool plus a structured call whose arguments
    # did not parse. The turn carries a tool call, yet its structured answer is broken, so it
    # is judged under the cap, and the re-prompt answers BOTH calls (a provider replays the
    # invalid call as a tool call, and an unanswered one is rejected).
    _set_cap(monkeypatch, 3)
    mixed = AIMessage(
        content="",
        tool_calls=[{"id": "ok", "name": "loop", "args": {}}],
        invalid_tool_calls=[
            {"type": "invalid_tool_call", "id": "bad", "name": "Answer", "args": '{"value": ', "error": "truncated"}
        ],
    )
    model = _ScriptedModel([mixed, _answer_call(7)])
    graph, strategy = _compile(model, [_loop_tool()], _SCHEMA)

    events = asyncio.run(_project(graph, strategy, _SCHEMA, _run_config()))

    assert [event.data for event in events if isinstance(event, StructuredFinal)] == [{"value": 7}]
    assert model.calls == 2
    answers = [message for message in model.seen[1] if isinstance(message, ToolMessage)]
    assert sorted(answer.tool_call_id for answer in answers) == ["bad", "ok"]
    assert all("'Answer'" in str(answer.content) for answer in answers)


def test_parallel_turn_with_a_broken_structured_call_ends_on_the_cap_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_cap(monkeypatch, 3)
    mixed = AIMessage(
        content="",
        tool_calls=[{"id": "ok", "name": "loop", "args": {}}],
        invalid_tool_calls=[
            {"type": "invalid_tool_call", "id": "bad", "name": "Answer", "args": '{"value": ', "error": "truncated"}
        ],
    )
    model = _ScriptedModel([mixed])
    graph, strategy = _compile(model, [_loop_tool()], _SCHEMA)

    events = asyncio.run(_project(graph, strategy, _SCHEMA, _run_config()))

    terminal = events[-1]
    assert isinstance(terminal, StructuredOutputUnresolvedFinal)
    assert terminal.schema_name == "Answer"
    assert terminal.attempts == 4
    assert model.calls == 4


_FAN_OUT_SCHEMA = {
    "title": "Shape",
    "anyOf": [
        {"type": "object", "properties": {"r": {"type": "number"}}, "required": ["r"]},
        {"type": "object", "properties": {"w": {"type": "number"}}, "required": ["w"]},
    ],
}

_FAN_OUT_FAILURES = {
    "text_answer": AIMessage(content="It is a circle of radius 2."),
    "broken_variant_call": AIMessage(
        content="",
        invalid_tool_calls=[
            {"type": "invalid_tool_call", "id": "bad", "name": "Shape_any0", "args": '{"r": ', "error": "truncated"}
        ],
    ),
}


@pytest.mark.parametrize("turn", list(_FAN_OUT_FAILURES.values()), ids=list(_FAN_OUT_FAILURES))
def test_fan_out_cap_outcome_names_the_requested_schema(monkeypatch: pytest.MonkeyPatch, turn: AIMessage) -> None:
    _set_cap(monkeypatch, 2)
    model = _ScriptedModel([turn])
    graph, strategy = _compile(model, [], _FAN_OUT_SCHEMA)

    events = asyncio.run(_project(graph, strategy, _FAN_OUT_SCHEMA, _run_config()))

    terminal = events[-1]
    assert isinstance(terminal, StructuredOutputUnresolvedFinal)
    assert terminal.schema_name == "Shape"
    assert terminal.attempts == 3


class _VariantA(BaseModel):
    a: int


class _VariantB(BaseModel):
    b: int


_UNION_FAILURES = {
    "text_answer": AIMessage(content="just text"),
    "broken_variant_call": AIMessage(
        content="",
        invalid_tool_calls=[
            {"type": "invalid_tool_call", "id": "bad", "name": "_VariantA", "args": '{"a": ', "error": "truncated"}
        ],
    ),
}


@pytest.mark.parametrize("turn", list(_UNION_FAILURES.values()), ids=list(_UNION_FAILURES))
@pytest.mark.parametrize("explicit", [False, True], ids=["union", "explicit_tool_strategy"])
def test_union_cap_outcome_names_every_variant_on_every_path(
    monkeypatch: pytest.MonkeyPatch, turn: AIMessage, explicit: bool
) -> None:
    _set_cap(monkeypatch, 2)
    union: Any = Union[_VariantA, _VariantB]  # noqa: UP007 (a typing.Union is the authored response_format shape)
    schema: Any = ToolStrategy(union) if explicit else union
    model = _ScriptedModel([turn])
    graph, strategy = _compile(model, [], schema)

    events = asyncio.run(_project(graph, strategy, schema, _run_config()))

    terminal = events[-1]
    assert isinstance(terminal, StructuredOutputUnresolvedFinal)
    assert terminal.schema_name == "_VariantA, _VariantB"
    assert terminal.attempts == 3


def test_invoke_face_final_turn_without_the_structured_answer_returns_the_cap_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _ScriptedModel([AIMessage(content="")])
    _patch_invoke_seams(monkeypatch, model, recursion_limit=50)

    result = asyncio.run(invoke_tools_agent(system_message="", user_message=["go"], tools=[], response_format=_SCHEMA))

    assert isinstance(result.outcome, StructuredOutputUnresolvedFinal)
    assert result.outcome.attempts == 4
    assert model.calls == 4


def test_deep_agent_final_turn_without_the_structured_answer_yields_the_cap_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langgraph.store.memory import InMemoryStore

    from tai42_agents.langchain_deep_agent.factory import build_langchain_deep_agent

    _set_cap(monkeypatch, 3)
    model = _ScriptedModel([AIMessage(content='{"value": 7}')])
    graph = asyncio.run(
        build_langchain_deep_agent(
            llm=model,
            store=InMemoryStore(),
            checkpointer=InMemorySaver(),
            provider="openai",
            tools=[],
            response_format=_SCHEMA,
        )
    )
    strategy, _rail = structured_output_stack(model, "openai", _SCHEMA)
    events = asyncio.run(_project(graph, strategy, _SCHEMA, _run_config()))

    assert isinstance(events[-1], StructuredOutputUnresolvedFinal)
    assert events[-1].attempts == 4
    assert model.calls == 4


def _deep_agent_with_structured_subagent(monkeypatch: pytest.MonkeyPatch, sub_model: _ScriptedModel) -> tuple[Any, Any]:
    """A deep agent whose main model delegates once to a subagent that forces ``_SCHEMA``."""
    from langgraph.store.memory import InMemoryStore
    from tai42_contract.template import TemplatedText

    from tai42_agents.langchain_deep_agent import factory as deep_factory
    from tai42_agents.langchain_deep_agent.spec import ResolvedSubAgentSpec

    async def _sub_llm(provider: str, **_kwargs: Any) -> BaseChatModel:
        return sub_model

    monkeypatch.setattr(deep_factory, "get_llm_async", _sub_llm)
    delegate = AIMessage(
        content="",
        tool_calls=[{"id": "t", "name": "task", "args": {"description": "count", "subagent_type": "counter"}}],
    )
    main_model = _ScriptedModel([delegate, AIMessage(content="done")])
    spec = ResolvedSubAgentSpec(
        name="counter",
        description="counts",
        system_prompt=TemplatedText(content="count"),
        llm_provider="openai",
        response_format=_SCHEMA,
    )
    graph = asyncio.run(
        deep_factory.build_langchain_deep_agent(
            llm=main_model,
            store=InMemoryStore(),
            checkpointer=InMemorySaver(),
            provider="openai",
            tools=[],
            subagents=[spec],
        )
    )
    return graph, main_model


def test_deep_agent_subagent_final_turn_without_the_structured_answer_is_reprompted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_cap(monkeypatch, 3)
    sub_model = _ScriptedModel([AIMessage(content='{"value": 7}'), _answer_call(7)])
    graph, main_model = _deep_agent_with_structured_subagent(monkeypatch, sub_model)

    asyncio.run(_project(graph, None, None, _run_config()))

    assert sub_model.calls == 2
    feedback = sub_model.seen[1][-1]
    assert isinstance(feedback, HumanMessage)
    assert "no structured answer was returned" in str(feedback.content)
    delegated = [m for m in main_model.seen[1] if isinstance(m, ToolMessage) and m.tool_call_id == "t"]
    assert [m.content for m in delegated] == ['{"value": 7}']


def test_deep_agent_subagent_never_carrying_the_structured_answer_yields_the_cap_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_cap(monkeypatch, 2)
    sub_model = _ScriptedModel([AIMessage(content="")])
    graph, _main_model = _deep_agent_with_structured_subagent(monkeypatch, sub_model)

    events = asyncio.run(_project(graph, None, None, _run_config()))

    terminal = events[-1]
    assert isinstance(terminal, StructuredOutputUnresolvedFinal)
    assert terminal.schema_name == "Answer"
    assert terminal.attempts == 3
    assert "no structured answer was returned" in terminal.error
    assert sub_model.calls == 3


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

    async def _fake_loop(**_kwargs: Any) -> tuple[Any, Any, Any, Any, list[str]]:
        return object(), {}, {}, None, []

    monkeypatch.setattr(refine_mod, "_run_refine_loop", _fake_loop)

    async def _fake_project(*_args: Any, **_kwargs: Any):
        yield StructuredOutputUnresolvedFinal(schema_name="Answer", attempts=4, error="bad")

    monkeypatch.setattr(refine_mod, "aproject_agent_events", _fake_project)

    # response_format set + no StructuredFinal would normally raise; the outcome suppresses it.
    events = _drive_refine(agent, response_format={"title": "Answer", "type": "object"})
    assert len(events) == 1
    assert isinstance(events[0], StructuredOutputUnresolvedFinal)


def _patch_refine_final_pass(monkeypatch: pytest.MonkeyPatch, model: _ScriptedModel) -> None:
    """Stand in for the refine loop: hand back the real structured final pass over ``model``."""
    from tai42_agents.refine_agent import agent as refine_mod

    async def _final_pass(**kwargs: Any) -> tuple[Any, Any, Any, Any, list[str]]:
        strategy, structured_rail = structured_output_stack(model, "openai", kwargs["response_format"])
        final_agent = await refine_mod._build_role_agent(
            model,
            [],
            refine_mod.EVALUATOR_SYSTEM_MESSAGE,
            InMemorySaver(),
            provider="openai",
            is_enabled_for_debug=False,
            response_format=strategy,
            structured_rail=structured_rail,
        )
        final_input = {"messages": [{"role": "user", "content": "Critic Approved."}]}
        return final_agent, final_input, _run_config(), strategy, []

    monkeypatch.setattr(refine_mod, "_run_refine_loop", _final_pass)


def test_refine_final_pass_without_the_structured_answer_is_reprompted(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_contract.app import tai42_app

    _set_cap(monkeypatch, 3)
    model = _ScriptedModel([AIMessage(content='{"value": 7}'), _answer_call(7)])
    _patch_refine_final_pass(monkeypatch, model)

    events = _drive_refine(tai42_app.agents.get_agent("refine_agent"), response_format=_SCHEMA)

    assert [event.data for event in events if isinstance(event, StructuredFinal)] == [{"value": 7}]
    assert model.calls == 2
    feedback = model.seen[1][-1]
    assert isinstance(feedback, HumanMessage)
    assert "no structured answer was returned" in str(feedback.content)


def test_refine_final_pass_never_carrying_the_structured_answer_ends_on_the_cap_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_contract.app import tai42_app

    _set_cap(monkeypatch, 2)
    model = _ScriptedModel([AIMessage(content="")])
    _patch_refine_final_pass(monkeypatch, model)

    events = _drive_refine(tai42_app.agents.get_agent("refine_agent"), response_format=_SCHEMA)

    terminal = events[-1]
    assert isinstance(terminal, StructuredOutputUnresolvedFinal)
    assert terminal.schema_name == "Answer"
    assert terminal.attempts == 3
    assert "no structured answer was returned" in terminal.error
    assert model.calls == 3


def test_refine_loop_recursion_trip_yields_named_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph.errors import GraphRecursionError
    from tai42_contract.app import tai42_app

    from tai42_agents.refine_agent import agent as refine_mod

    agent = tai42_app.agents.get_agent("refine_agent")

    async def _boom(**_kwargs: Any) -> tuple[Any, Any, Any, Any]:
        raise GraphRecursionError("Recursion limit of 4 reached")

    monkeypatch.setattr(refine_mod, "_run_refine_loop", _boom)

    events = _drive_refine(agent)
    assert len(events) == 1
    assert isinstance(events[0], RecursionLimitFinal)
