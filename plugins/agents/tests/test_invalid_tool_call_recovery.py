"""Invalid tool calls (arguments that did not parse) are answered on every agent door.

A provider sends a turn's ``invalid_tool_calls`` back as tool calls on the next request,
and both OpenAI-compatible and Anthropic requests are rejected when a tool call has no
tool result. The shared recovery module answers each invalid call that has an id and a
name with an error ``ToolMessage`` carrying its parse error, right after the model turn
that made it:

* on the ``create_agent`` doors (tools agent, refine roles and final pass, deep agent main,
  declared, general-purpose and nested subagents) through the shared
  ``ToolErrorToMessageMiddleware`` model-turn hook, which also sends a turn whose only
  calls were broken back to the model instead of ending the loop on it;
* on the retrieval graph through its agent node;
* at turn start through ``_repair_dangling_tool_calls``.

The structured-output rail judges a broken structured call in-node, so that call is
answered once, by the rail's re-prompt feedback, and never reaches the hook. A turn whose
only calls are broken calls to other tools is an interim turn on a structured run too.

Async code is driven with ``asyncio.run`` (the suite does not use pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain.agents import create_agent
from langchain.agents.structured_output import ProviderStrategy, ToolStrategy
from langchain_anthropic.chat_models import _format_messages
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langchain_openai.chat_models.base import _convert_message_to_dict
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.constants import START
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel, PrivateAttr
from tai42_contract.agent.events import (
    RecursionLimitFinal,
    StructuredFinal,
    StructuredOutputUnresolvedFinal,
    ToolCallStep,
    ToolResultStep,
)
from tai42_contract.template import TemplatedText
from tai42_kit.llm.structured import plan_structured_output

from tai42_agents._internal import base_tool_agent as bta
from tai42_agents._internal import recovery as rec
from tai42_agents._internal import structured as structured_mod
from tai42_agents._internal.outcomes import RepromptCapError, drive_reprompt_scope
from tai42_agents._internal.stream_events import aproject_agent_events
from tai42_agents._internal.structured import structured_output_stack

from ._client_tool import client_tool
from ._graph_support import invoke_tools_agent
from .conftest import fake_init_langgraph_config

_RECOVERY_LOGGER = "tai42_agents._internal.recovery"

_SCHEMA = {
    "title": "Answer",
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
}


class _Model(BaseChatModel):
    """Emits copies of scripted AIMessages in order (the last repeats) and records each call's messages."""

    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def seen(self) -> list[list[BaseMessage]]:
        return self._seen

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _Model:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self._seen.append(list(messages))
        reply = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        # A fresh message per turn, as a real model returns: a repeated turn is a new message.
        return ChatResult(generations=[ChatGeneration(message=reply.model_copy(deep=True))])


def _invalid(call_id: str | None, name: str, error: str = "truncated") -> dict[str, Any]:
    return {"type": "invalid_tool_call", "id": call_id, "name": name, "args": '{"q": ', "error": error}


def _mixed(name: str = "lookup", ok_args: dict[str, Any] | None = None) -> AIMessage:
    """One parallel turn: a valid call "ok" plus a call "bad" whose arguments did not parse."""
    return AIMessage(
        content="",
        tool_calls=[{"id": "ok", "name": name, "args": ok_args or {}}],
        invalid_tool_calls=[_invalid("bad", name)],
    )


def _only_broken(name: str = "lookup") -> AIMessage:
    return AIMessage(content="", invalid_tool_calls=[_invalid("bad", name)])


def _lookup_tool() -> StructuredTool:
    async def _lookup() -> str:
        return "found"

    return StructuredTool.from_function(func=None, coroutine=_lookup, name="lookup", description="Look it up.")


def _unanswered_openai(messages: list[BaseMessage]) -> set[str]:
    wire = [_convert_message_to_dict(message) for message in messages]
    called = {call["id"] for m in wire if m["role"] == "assistant" for call in m.get("tool_calls", [])}
    answered = {m["tool_call_id"] for m in wire if m["role"] == "tool"}
    return called - answered


def _unanswered_anthropic(messages: list[BaseMessage]) -> set[str]:
    _, wire = _format_messages(messages, model="claude")
    blocks = [b for m in wire if isinstance(m["content"], list) for b in m["content"] if isinstance(b, dict)]
    called = {b["id"] for b in blocks if b.get("type") == "tool_use"}
    answered = {b["tool_use_id"] for b in blocks if b.get("type") == "tool_result"}
    return called - answered


def _answers(messages: list[BaseMessage], call_id: str) -> list[ToolMessage]:
    return [m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == call_id]


def _assert_every_request_answers_every_call(model: _Model) -> None:
    for request in model.seen:
        assert _unanswered_openai(request) == set()
        assert _unanswered_anthropic(request) == set()


def _graph(model: _Model, *, middleware: Sequence[Any] | None = None, response_format: Any = None) -> Any:
    return create_agent(
        model,
        tools=[_lookup_tool()],
        checkpointer=InMemorySaver(),
        middleware=list(middleware if middleware is not None else [rec._tool_error_middleware]),
        response_format=response_format,
    )


def _config(thread_id: str = "t", recursion_limit: int = 50) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": recursion_limit}


def _run(graph: Any, config: dict[str, Any] | None = None) -> dict[str, Any]:
    return asyncio.run(graph.ainvoke({"messages": [HumanMessage("go")]}, config or _config()))


# --------------------------------------------------------------------------
# The shared model-turn hook (every create_agent door carries it)
# --------------------------------------------------------------------------


def test_broken_call_in_a_parallel_turn_is_answered_before_the_next_request() -> None:
    model = _Model([_mixed(), AIMessage(content="done")])

    _run(_graph(model))

    second = model.seen[1]
    assert _unanswered_openai(second) == set()
    assert _unanswered_anthropic(second) == set()
    [repaired] = _answers(second, "bad")
    assert repaired.status == "error"
    assert "truncated" in str(repaired.content)
    [executed] = _answers(second, "ok")
    assert executed.content == "found"


def test_turn_whose_only_call_is_broken_goes_back_to_the_model() -> None:
    model = _Model([_only_broken(), AIMessage(content="done")])

    result = _run(_graph(model))

    assert len(model.seen) == 2
    [repaired] = _answers(model.seen[1], "bad")
    assert "truncated" in str(repaired.content)
    assert result["messages"][-1].content == "done"


def test_each_answered_invalid_call_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    model = _Model([_mixed(), AIMessage(content="done")])

    with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
        _run(_graph(model))

    assert any(
        "lookup" in r.getMessage() and "bad" in r.getMessage() and "truncated" in r.getMessage() for r in caplog.records
    )


def test_invalid_call_without_an_id_is_logged_and_cannot_be_answered(caplog: pytest.LogCaptureFixture) -> None:
    turn = AIMessage(
        content="",
        tool_calls=[{"id": "ok", "name": "lookup", "args": {}}],
        invalid_tool_calls=[_invalid(None, "lookup", error="no id here")],
    )
    model = _Model([turn, AIMessage(content="done")])

    with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
        _run(_graph(model))

    assert [m.tool_call_id for m in model.seen[1] if isinstance(m, ToolMessage)] == ["ok"]
    expected = "the model returned a call to tool 'lookup' with no id whose arguments did not parse: no id here"
    assert any(r.getMessage() == expected for r in caplog.records)


@pytest.mark.parametrize(
    ("call_id", "name", "expected"),
    [
        ("", "lookup", "the model returned a call to tool 'lookup' with no id whose arguments did not parse: empty"),
        ("bad", "", "the model returned a call bad with no tool name whose arguments did not parse: empty"),
    ],
)
def test_invalid_call_with_an_empty_id_or_name_is_logged_and_cannot_be_answered(
    caplog: pytest.LogCaptureFixture, call_id: str, name: str, expected: str
) -> None:
    turn = AIMessage(
        content="",
        tool_calls=[{"id": "ok", "name": "lookup", "args": {}}],
        invalid_tool_calls=[_invalid(call_id, name, error="empty")],
    )
    model = _Model([turn, AIMessage(content="done")])

    with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
        _run(_graph(model))

    assert [m.tool_call_id for m in model.seen[1] if isinstance(m, ToolMessage)] == ["ok"]
    assert any(r.getMessage() == expected for r in caplog.records)


def test_answered_invalid_call_streams_as_a_paired_call_and_error_result() -> None:
    model = _Model([_mixed(), AIMessage(content="done")])

    async def go() -> list[Any]:
        return [
            event async for event in aproject_agent_events(_graph(model), {"messages": [HumanMessage("go")]}, _config())
        ]

    events = asyncio.run(go())

    calls = {event.call_id for event in events if isinstance(event, ToolCallStep)}
    results = {event.call_id: event for event in events if isinstance(event, ToolResultStep)}
    assert calls == {"ok", "bad"}
    assert set(results) == {"ok", "bad"}
    assert results["bad"].is_error is True
    assert results["ok"].is_error is False


def test_a_model_that_keeps_breaking_its_only_call_ends_on_the_recursion_limit() -> None:
    model = _Model([_only_broken()])

    async def go() -> list[Any]:
        return [
            event
            async for event in aproject_agent_events(
                _graph(model), {"messages": [HumanMessage("go")]}, _config(recursion_limit=8)
            )
        ]

    events = asyncio.run(go())

    assert isinstance(events[-1], RecursionLimitFinal)
    _assert_every_request_answers_every_call(model)


# --------------------------------------------------------------------------
# The turn-start repair
# --------------------------------------------------------------------------


def test_turn_start_repair_answers_the_last_turns_invalid_calls() -> None:
    model = _Model([AIMessage(content="done")])
    graph = _graph(model)
    config = _config("t-repair")

    async def go() -> list[BaseMessage]:
        await graph.aupdate_state(config, {"messages": [HumanMessage("first"), _mixed()]}, as_node=START)
        await rec._repair_dangling_tool_calls(graph, config)
        return list((await graph.aget_state(config)).values["messages"])

    messages = asyncio.run(go())

    assert _unanswered_openai(messages) == set()
    [repaired] = _answers(messages, "bad")
    assert repaired.status == "error"
    assert "truncated" in str(repaired.content)
    [interrupted] = _answers(messages, "ok")
    assert interrupted.content == rec._INTERRUPTED_TOOL_RESULT


# --------------------------------------------------------------------------
# Composition with the structured-output rail
# --------------------------------------------------------------------------


def _structured_graph(
    model: _Model, monkeypatch: pytest.MonkeyPatch, response_format: Any = _SCHEMA
) -> tuple[Any, Any]:
    monkeypatch.setattr(
        structured_mod, "agents_limits_settings", lambda: SimpleNamespace(structured_output_reprompt_cap=3)
    )
    strategy, rail = structured_output_stack(model, "openai", response_format)
    assert rail is not None
    # The door order: the shared tool-error middleware, then the rail innermost.
    return _graph(model, middleware=[rec._tool_error_middleware, rail], response_format=strategy), strategy


def _answer_call(value: int) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": "c", "name": "Answer", "args": {"value": value}}])


def _project_structured(graph: Any, strategy: Any) -> list[Any]:
    async def go() -> list[Any]:
        return [
            event
            async for event in aproject_agent_events(
                graph,
                {"messages": [HumanMessage("go")]},
                _config(),
                response_format=_SCHEMA,
                structured_strategy=strategy,
            )
        ]

    return asyncio.run(go())


def test_broken_structured_call_is_judged_by_the_rail_and_answered_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    broken = AIMessage(
        content="",
        tool_calls=[{"id": "ok", "name": "lookup", "args": {}}],
        invalid_tool_calls=[_invalid("bad", "Answer")],
    )
    model = _Model([broken, _answer_call(7)])
    graph, strategy = _structured_graph(model, monkeypatch)

    with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
        events = _project_structured(graph, strategy)

    assert [event.data for event in events if isinstance(event, StructuredFinal)] == [{"value": 7}]
    # The rail re-prompted in-node: the feedback answers each call of the judged turn once.
    second = model.seen[1]
    assert len(_answers(second, "bad")) == 1
    assert len(_answers(second, "ok")) == 1
    assert "'Answer'" in str(_answers(second, "bad")[0].content)
    # The judged turn never left the model node, so the hook never answered it.
    assert not any("bad" in r.getMessage() for r in caplog.records)
    stored = asyncio.run(graph.aget_state(_config())).values["messages"]
    assert _answers(stored, "bad") == []


def test_broken_call_to_another_tool_in_an_interim_turn_is_answered_once_by_the_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _Model([_mixed(), _answer_call(7)])
    graph, strategy = _structured_graph(model, monkeypatch)

    events = _project_structured(graph, strategy)

    assert [event.data for event in events if isinstance(event, StructuredFinal)] == [{"value": 7}]
    second = model.seen[1]
    assert _unanswered_openai(second) == set()
    [repaired] = _answers(second, "bad")
    assert repaired.status == "error"
    assert "truncated" in str(repaired.content)
    assert [m.content for m in _answers(second, "ok")] == ["found"]


def test_broken_structured_call_beside_another_call_is_judged_under_an_explicit_tool_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = AIMessage(
        content="",
        tool_calls=[{"id": "ok", "name": "lookup", "args": {}}],
        invalid_tool_calls=[_invalid("bad", "Answer")],
    )
    model = _Model([broken, _answer_call(7)])
    graph, strategy = _structured_graph(model, monkeypatch, ToolStrategy(_SCHEMA))

    events = _project_structured(graph, strategy)

    assert [event.data for event in events if isinstance(event, StructuredFinal)] == [{"value": 7}]
    assert len(model.seen) == 2
    _assert_every_request_answers_every_call(model)
    assert "'Answer'" in str(_answers(model.seen[1], "bad")[0].content)


def test_explicit_tool_strategy_that_keeps_breaking_its_structured_call_ends_on_the_typed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = AIMessage(
        content="",
        tool_calls=[{"id": "ok", "name": "lookup", "args": {}}],
        invalid_tool_calls=[_invalid("bad", "Answer")],
    )
    model = _Model([broken])
    graph, strategy = _structured_graph(model, monkeypatch, ToolStrategy(_SCHEMA))

    events = _project_structured(graph, strategy)

    terminal = events[-1]
    assert isinstance(terminal, StructuredOutputUnresolvedFinal)
    assert terminal.schema_name == "Answer"
    assert terminal.attempts == 4  # cap + 1
    _assert_every_request_answers_every_call(model)


# --------------------------------------------------------------------------
# The single-shot door
# --------------------------------------------------------------------------


def _single_shot(model: _Model, monkeypatch: pytest.MonkeyPatch, cap: int = 3) -> Any:
    monkeypatch.setattr(
        structured_mod, "agents_limits_settings", lambda: SimpleNamespace(structured_output_reprompt_cap=cap)
    )
    plan = plan_structured_output(model, "openai", _SCHEMA)
    assert plan.mode == "tool"
    return asyncio.run(structured_mod.ainvoke_structured(model, plan, [HumanMessage("go")]))


def test_single_shot_broken_structured_call_is_reprompted_with_its_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _Model([_only_broken("Answer"), _answer_call(7)])

    assert _single_shot(model, monkeypatch) == {"value": 7}

    second = model.seen[1]
    assert _unanswered_openai(second) == set()
    assert _unanswered_anthropic(second) == set()
    [feedback] = _answers(second, "bad")
    assert "truncated" in str(feedback.content)
    assert "'Answer'" in str(feedback.content)
    assert "no structured answer" not in str(feedback.content)


def test_single_shot_that_keeps_breaking_its_structured_call_reports_the_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _Model([_only_broken("Answer")])

    with pytest.raises(RepromptCapError) as caught:
        _single_shot(model, monkeypatch)

    assert caught.value.schema_name == "Answer"
    assert caught.value.attempts == 4
    assert "truncated" in caught.value.error
    assert "no structured answer" not in caught.value.error
    _assert_every_request_answers_every_call(model)


# --------------------------------------------------------------------------
# The doors
# --------------------------------------------------------------------------


def test_tools_agent_door_answers_a_broken_call(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _Model([_mixed(), AIMessage(content="done")])
    monkeypatch.setattr(
        bta, "llm_provider_settings", lambda: SimpleNamespace(llm="openai", checkpoint="c", checkpoint_conn_string=None)
    )
    monkeypatch.setattr(bta, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))

    async def fake_get_llm(*, provider: str, **kwargs: Any) -> Any:
        return model

    async def fake_get_checkpointer(*, provider: str, conn_string: Any) -> Any:
        return InMemorySaver()

    monkeypatch.setattr(bta, "get_llm_async", fake_get_llm)
    monkeypatch.setattr(bta, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=fake_get_checkpointer))
    monkeypatch.setattr(bta, "context_overflow_middlewares", AsyncMock(return_value=[]))
    monkeypatch.setattr(bta, "init_langgraph_config", fake_init_langgraph_config)

    async def _lookup() -> str:
        return "found"

    tool = client_tool("lookup", _lookup, description="Look it up.")
    result = asyncio.run(invoke_tools_agent("sys", ["go"], [tool], config={"configurable": {"thread_id": "t"}}))

    assert result.output == "done"
    _assert_every_request_answers_every_call(model)
    assert len(_answers(model.seen[1], "bad")) == 1


def test_refine_role_agent_answers_a_broken_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # The evaluator, the critic and the structured final pass are all built by this one
    # role builder, so one test covers the three refine stacks.
    from tai42_agents.refine_agent import agent as refine_mod

    monkeypatch.setattr(refine_mod, "context_overflow_middlewares", AsyncMock(return_value=[]))
    model = _Model([_only_broken(), AIMessage(content="done")])
    role = asyncio.run(
        refine_mod._build_role_agent(
            model, [_lookup_tool()], "evaluate", InMemorySaver(), provider="openai", is_enabled_for_debug=False
        )
    )

    _run(role)

    assert len(model.seen) == 2
    _assert_every_request_answers_every_call(model)
    assert len(_answers(model.seen[1], "bad")) == 1


def _delegate(subagent_type: str, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": "task", "args": {"description": "go", "subagent_type": subagent_type}}],
    )


def _deep_graph(model: _Model, subagents: list[Any] | None = None) -> Any:
    from tai42_agents.langchain_deep_agent.factory import build_langchain_deep_agent

    return asyncio.run(
        build_langchain_deep_agent(
            llm=model,
            store=InMemoryStore(),
            checkpointer=InMemorySaver(),
            provider="openai",
            tools=[_lookup_tool()],
            subagents=subagents,
        )
    )


def _spec(name: str, subagents: list[Any] | None = None) -> Any:
    from tai42_agents.langchain_deep_agent.spec import ResolvedSubAgentSpec

    return ResolvedSubAgentSpec(
        name=name, description=f"{name} work", system_prompt=TemplatedText(content=name), subagents=subagents or []
    )


def test_deep_agent_main_answers_a_broken_call() -> None:
    model = _Model([_mixed(), AIMessage(content="done")])

    _run(_deep_graph(model))

    _assert_every_request_answers_every_call(model)
    assert len(_answers(model.seen[1], "bad")) == 1


@pytest.mark.parametrize("subagent_type", ["general-purpose", "worker"])
def test_deep_agent_subagent_answers_a_broken_call(subagent_type: str) -> None:
    # One shared scripted model serves every level in order: the main agent delegates,
    # the subagent makes the broken parallel turn, then each level answers.
    model = _Model([_delegate(subagent_type, "t1"), _mixed(), AIMessage(content="sub done"), AIMessage(content="done")])

    _run(_deep_graph(model, [_spec("worker")]))

    assert len(model.seen) == 4
    _assert_every_request_answers_every_call(model)
    assert len(_answers(model.seen[2], "bad")) == 1


def test_deep_agent_nested_subagent_answers_a_broken_call() -> None:
    model = _Model(
        [
            _delegate("worker", "t1"),
            _delegate("helper", "t2"),
            _mixed(),
            AIMessage(content="nested done"),
            AIMessage(content="sub done"),
            AIMessage(content="done"),
        ]
    )

    _run(_deep_graph(model, [_spec("worker", [_spec("helper")])]))

    assert len(model.seen) == 6
    _assert_every_request_answers_every_call(model)
    assert len(_answers(model.seen[3], "bad")) == 1


class _StubStore(InMemoryStore):
    """Every retrieval search finds the given tool keys."""

    def __init__(self, keys: list[str]) -> None:
        super().__init__()
        self._keys = keys

    async def asearch(self, namespace_prefix: Any, /, **kwargs: Any) -> Any:
        return [SimpleNamespace(key=key) for key in self._keys]


def _status(result: str) -> AIMessage:
    return AIMessage(content=json.dumps({"status": "success", "message": "m", "result": result}))


def _retrieval_graph(model: _Model) -> Any:
    from tai42_kit.utils.data.string_util import text_to_md5

    from tai42_agents.retrieval_tools_agent.graph import RetrievalToolsGraph

    tool = _lookup_tool()
    return asyncio.run(
        RetrievalToolsGraph(
            provider="anthropic", tools=[tool], llm=model, store=_StubStore([text_to_md5(tool.name)])
        ).abuild()
    )


def test_retrieval_graph_answers_a_broken_call_in_a_parallel_turn() -> None:
    turn = AIMessage(
        content="",
        tool_calls=[{"id": "ok", "name": "retrieve_tools", "args": {"query": "q"}}],
        invalid_tool_calls=[_invalid("bad", "lookup")],
    )
    model = _Model([turn, _status("done")])

    _run(_retrieval_graph(model))

    _assert_every_request_answers_every_call(model)
    assert len(_answers(model.seen[1], "bad")) == 1


def test_retrieval_graph_sends_a_turn_whose_only_call_is_broken_back_to_the_model() -> None:
    model = _Model([_only_broken(), _status("done")])

    result = _run(_retrieval_graph(model))

    assert len(model.seen) == 2
    _assert_every_request_answers_every_call(model)
    assert len(_answers(model.seen[1], "bad")) == 1
    assert json.loads(result["messages"][-1].content)["result"] == "done"


# --------------------------------------------------------------------------
# A valid call left unanswered after the turn's broken calls were answered
# --------------------------------------------------------------------------


def _aborting_lookup_tool() -> StructuredTool:
    async def _lookup() -> str:
        raise RuntimeError("infra down")

    return StructuredTool.from_function(func=None, coroutine=_lookup, name="lookup", description="Look it up.")


def test_turn_start_repair_answers_a_valid_call_whose_tool_aborted_after_the_broken_call_was_answered() -> None:
    # The hook answers "bad" as the model turn leaves the node; the tool for "ok" then
    # aborts loudly, so the checkpoint ends on the answer to "bad" with "ok" unanswered.
    model = _Model([_mixed(), AIMessage(content="done")])
    graph: Any = create_agent(
        model, tools=[_aborting_lookup_tool()], checkpointer=InMemorySaver(), middleware=[rec._tool_error_middleware]
    )
    config = _config("t-abort")

    async def go() -> None:
        with pytest.raises(RuntimeError, match="infra down"):
            await graph.ainvoke({"messages": [HumanMessage("go")]}, config)
        await rec._repair_dangling_tool_calls(graph, config)
        await graph.ainvoke({"messages": [HumanMessage("again")]}, config)

    asyncio.run(go())

    last_request = model.seen[-1]
    assert _unanswered_openai(last_request) == set()
    assert _unanswered_anthropic(last_request) == set()
    [interrupted] = _answers(last_request, "ok")
    assert interrupted.content == rec._INTERRUPTED_TOOL_RESULT
    assert len(_answers(last_request, "bad")) == 1


def test_turn_start_repair_answers_only_the_calls_left_unanswered_after_the_last_turn() -> None:
    # The state the retrieval graph's agent node and the shared hook both leave behind when
    # the tools node aborts: the turn, then the answer to its broken call.
    graph = _graph(_Model([AIMessage(content="done")]))
    config = _config("t-partial")
    answered_bad = ToolMessage(content="Error: truncated", tool_call_id="bad", name="lookup", status="error")

    async def go() -> list[BaseMessage]:
        await graph.aupdate_state(config, {"messages": [HumanMessage("first"), _mixed(), answered_bad]}, as_node=START)
        await rec._repair_dangling_tool_calls(graph, config)
        return list((await graph.aget_state(config)).values["messages"])

    messages = asyncio.run(go())

    assert _unanswered_openai(messages) == set()
    assert len(_answers(messages, "bad")) == 1
    [interrupted] = _answers(messages, "ok")
    assert interrupted.content == rec._INTERRUPTED_TOOL_RESULT


# --------------------------------------------------------------------------
# A structured run's interim turn whose only call is broken
# --------------------------------------------------------------------------


def test_structured_run_sends_a_turn_whose_only_call_is_broken_back_with_its_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cap 0: any judged turn would end the run on the cap outcome.
    monkeypatch.setattr(
        structured_mod, "agents_limits_settings", lambda: SimpleNamespace(structured_output_reprompt_cap=0)
    )
    model = _Model([_only_broken(), _answer_call(7)])
    strategy, rail = structured_output_stack(model, "openai", _SCHEMA)
    graph = _graph(model, middleware=[rec._tool_error_middleware, rail], response_format=strategy)

    events = _project_structured(graph, strategy)

    assert [event.data for event in events if isinstance(event, StructuredFinal)] == [{"value": 7}]
    [repaired] = _answers(model.seen[1], "bad")
    assert repaired.status == "error"
    assert "truncated" in str(repaired.content)
    assert _unanswered_openai(model.seen[1]) == set()


def test_native_structured_run_sends_a_turn_whose_only_call_is_broken_back_with_its_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        structured_mod, "agents_limits_settings", lambda: SimpleNamespace(structured_output_reprompt_cap=0)
    )
    model = _Model([_only_broken(), AIMessage(content='{"value": 7}')], profile={"structured_output": True})
    strategy, rail = structured_output_stack(model, "openai", _SCHEMA)
    assert not isinstance(strategy, ToolStrategy)
    graph = _graph(model, middleware=[rec._tool_error_middleware, rail], response_format=strategy)

    events = _project_structured(graph, strategy)

    assert [event.data for event in events if isinstance(event, StructuredFinal)] == [{"value": 7}]
    [repaired] = _answers(model.seen[1], "bad")
    assert repaired.status == "error"
    assert "truncated" in str(repaired.content)
    assert _unanswered_openai(model.seen[1]) == set()


class _Answer(BaseModel):
    value: int


_PROVIDER_FORMATS = {"dict": ProviderStrategy(_SCHEMA), "pydantic": ProviderStrategy(_Answer)}


def _non_conforming_answer_beside_a_broken_call() -> AIMessage:
    """A native answer whose JSON decodes but fails the schema, beside a broken call to another tool."""
    return AIMessage(content='{"value": "seven"}', invalid_tool_calls=[_invalid("bad", "lookup")])


def _project_provider(graph: Any, strategy: Any, schema: Any) -> list[Any]:
    async def go() -> list[Any]:
        return [
            event
            async for event in aproject_agent_events(
                graph,
                {"messages": [HumanMessage("go")]},
                _config(recursion_limit=25),
                response_format=schema,
                structured_strategy=strategy,
            )
        ]

    return asyncio.run(go())


@pytest.mark.parametrize("schema", list(_PROVIDER_FORMATS.values()), ids=list(_PROVIDER_FORMATS))
def test_provider_strategy_judges_a_decoded_answer_beside_only_broken_calls(
    monkeypatch: pytest.MonkeyPatch, schema: Any
) -> None:
    # Cap 0: the judged turn ends the run on the cap outcome at once.
    monkeypatch.setattr(
        structured_mod, "agents_limits_settings", lambda: SimpleNamespace(structured_output_reprompt_cap=0)
    )
    model = _Model(
        [_non_conforming_answer_beside_a_broken_call(), AIMessage(content='{"value": 7}')],
        profile={"structured_output": True},
    )
    strategy, rail = structured_output_stack(model, "openai", schema)
    graph = _graph(model, middleware=[rec._tool_error_middleware, rail], response_format=strategy)

    events = _project_provider(graph, strategy, schema)

    terminal = events[-1]
    assert isinstance(terminal, StructuredOutputUnresolvedFinal)
    assert terminal.attempts == 1
    assert len(model.seen) == 1


@pytest.mark.parametrize("schema", list(_PROVIDER_FORMATS.values()), ids=list(_PROVIDER_FORMATS))
def test_provider_strategy_repeating_a_decoded_answer_beside_only_broken_calls_ends_on_the_cap(
    monkeypatch: pytest.MonkeyPatch, schema: Any
) -> None:
    monkeypatch.setattr(
        structured_mod, "agents_limits_settings", lambda: SimpleNamespace(structured_output_reprompt_cap=3)
    )
    model = _Model([_non_conforming_answer_beside_a_broken_call()], profile={"structured_output": True})
    strategy, rail = structured_output_stack(model, "openai", schema)
    graph = _graph(model, middleware=[rec._tool_error_middleware, rail], response_format=strategy)

    events = _project_provider(graph, strategy, schema)

    terminal = events[-1]
    assert isinstance(terminal, StructuredOutputUnresolvedFinal)
    assert not any(isinstance(event, RecursionLimitFinal) for event in events)
    assert terminal.attempts == 4
    assert len(model.seen) == 4
    _assert_every_request_answers_every_call(model)


# --------------------------------------------------------------------------
# A structured answer stored by an earlier run on the same thread
# --------------------------------------------------------------------------


def _two_runs(graph: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    config = _config("t-stale")

    async def go() -> tuple[dict[str, Any], dict[str, Any]]:
        first = await graph.ainvoke({"messages": [HumanMessage("go")]}, config)
        second = await graph.ainvoke({"messages": [HumanMessage("again")]}, config)
        return first, second

    return asyncio.run(go())


def test_earlier_runs_structured_answer_does_not_end_a_turn_whose_only_call_is_broken() -> None:
    model = _Model([_answer_call(7), _only_broken(), _answer_call(9)])

    first, second = _two_runs(_graph(model, response_format=ToolStrategy(_SCHEMA)))

    assert first["structured_response"] == {"value": 7}
    assert second["structured_response"] == {"value": 9}
    assert len(model.seen) == 3
    assert len(_answers(model.seen[2], "bad")) == 1


def test_earlier_runs_structured_answer_does_not_end_a_railed_turn_whose_only_call_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _Model([_answer_call(7), _only_broken(), _answer_call(9)])
    graph, _ = _structured_graph(model, monkeypatch)

    config = _config("t-stale-rail")

    async def drive() -> tuple[dict[str, Any], dict[str, Any]]:
        with drive_reprompt_scope():
            first = await graph.ainvoke({"messages": [HumanMessage("go")]}, config)
        with drive_reprompt_scope():
            second = await graph.ainvoke({"messages": [HumanMessage("again")]}, config)
        return first, second

    first, second = asyncio.run(drive())

    assert first["structured_response"] == {"value": 7}
    assert second["structured_response"] == {"value": 9}
    assert len(model.seen) == 3


# --------------------------------------------------------------------------
# A broken call with an id but no name
# --------------------------------------------------------------------------


def _orphan_results_anthropic(messages: list[BaseMessage]) -> set[str]:
    _, wire = _format_messages(messages, model="claude")
    blocks = [b for m in wire if isinstance(m["content"], list) for b in m["content"] if isinstance(b, dict)]
    called = {b["id"] for b in blocks if b.get("type") == "tool_use"}
    answered = {b["tool_use_id"] for b in blocks if b.get("type") == "tool_result"}
    return answered - called


def _with_nameless(**kwargs: Any) -> AIMessage:
    nameless = {"type": "invalid_tool_call", "id": "anon", "name": None, "args": "{", "error": "no name here"}
    return AIMessage(content="", invalid_tool_calls=[*kwargs.pop("invalid", []), nameless], **kwargs)


def test_broken_call_without_a_name_is_logged_and_not_answered(caplog: pytest.LogCaptureFixture) -> None:
    model = _Model([_with_nameless(tool_calls=[{"id": "ok", "name": "lookup", "args": {}}]), AIMessage("done")])

    async def go() -> list[Any]:
        return [
            event async for event in aproject_agent_events(_graph(model), {"messages": [HumanMessage("go")]}, _config())
        ]

    with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
        events = asyncio.run(go())

    assert _answers(model.seen[1], "anon") == []
    assert _orphan_results_anthropic(model.seen[1]) == set()
    assert "anon" not in {event.call_id for event in events if isinstance(event, (ToolCallStep, ToolResultStep))}
    assert any("anon" in r.getMessage() and "no name here" in r.getMessage() for r in caplog.records)


def test_rail_feedback_does_not_answer_a_broken_call_without_a_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    model = _Model([_with_nameless(invalid=[_invalid("bad", "Answer")]), _answer_call(7)])
    graph, strategy = _structured_graph(model, monkeypatch)

    with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
        events = _project_structured(graph, strategy)

    assert [event.data for event in events if isinstance(event, StructuredFinal)] == [{"value": 7}]
    assert _answers(model.seen[1], "anon") == []
    assert len(_answers(model.seen[1], "bad")) == 1
    assert _orphan_results_anthropic(model.seen[1]) == set()
    assert any("anon" in r.getMessage() and "no name here" in r.getMessage() for r in caplog.records)
