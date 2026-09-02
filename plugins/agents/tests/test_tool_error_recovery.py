"""Tests for the tools-agent tool-error recovery: the ``wrap_tool_call`` middleware
that turns a tool-logic failure into a model-visible error ``ToolMessage``, and the
turn-start repair that answers a thread's dangling tool_calls before a new turn.

Both are exercised over a REAL ``create_agent`` graph driven by a scripted fake chat
model and an in-memory checkpointer, so the middleware wiring and the state-repair
land through the same reducers a live run uses — no LLM, checkpointer, or network.
Async code is driven with ``asyncio.run`` (the repo does not use ``pytest-asyncio``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool, ToolException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.constants import START
from pydantic import BaseModel, PrivateAttr, ValidationError
from tai42_contract.agent.base import PresetSpec

from tai42_agents._internal import base_tool_agent as bta
from tai42_agents._internal import recovery as rec
from tai42_agents._internal.resolve_tools import resolve_tools
from tai42_agents._internal.stream_events import astream_tools_agent_events

_RECOVERY_LOGGER = "tai42_agents._internal.recovery"


class _StrictModel(BaseModel):
    n: int


def _validation_error() -> ValidationError:
    try:
        _StrictModel(n="not-an-int")  # type: ignore[arg-type]
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


class ScriptedChatModel(BaseChatModel):
    """Returns a fixed list of messages in order; ``bind_tools`` is a no-op so the
    agent can bind its tools and still be scripted."""

    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        message = self._responses[self._index]
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self


def _raising_tool(exc: Exception, name: str = "failing") -> StructuredTool:
    async def _run(**_: Any) -> str:
        raise exc

    return StructuredTool.from_function(func=None, coroutine=_run, name=name, description="a tool that raises")


def _tool_call(name: str, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": {}}])


def _seams(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver) -> None:
    """Patch every provider seam ``_build_agent_and_input`` reaches so a REAL
    ``create_agent`` graph is compiled over ``model`` + ``saver`` — the middleware
    wiring, config init, and message build stay real."""
    monkeypatch.setattr(
        bta,
        "llm_provider_settings",
        lambda: SimpleNamespace(llm="p", checkpoint="c", checkpoint_conn_string=None),
    )
    monkeypatch.setattr(bta, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))

    async def fake_get_llm(*, provider: str, **kwargs: Any) -> Any:
        return model

    monkeypatch.setattr(bta, "get_llm_async", fake_get_llm)

    async def fake_get_checkpointer(*, provider: str, conn_string: Any) -> Any:
        return saver

    monkeypatch.setattr(bta, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=fake_get_checkpointer))
    # No context-overflow strategies in the test: keep the middleware list to the
    # tool-error middleware the factory appends.
    monkeypatch.setattr(bta, "context_overflow_middlewares", lambda system_prompt=None: [])
    # The real config init wires the recording monitoring stub's non-callable
    # callback sentinels, which the live graph would try to invoke; keep the
    # caller's thread_id and nothing else.
    monkeypatch.setattr(
        bta,
        "init_langgraph_config",
        lambda config: {"configurable": {"thread_id": config["configurable"]["thread_id"]}},
    )


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


async def _state_messages(saver: InMemorySaver, model: BaseChatModel, thread_id: str) -> list[BaseMessage]:
    """Read a thread's checkpointed messages back through a freshly built agent over
    the same checkpointer."""
    agent, _, config = await bta._build_agent_and_input("sys", ["x"], [], config=_config(thread_id))
    snapshot = await agent.aget_state(config)
    return snapshot.values.get("messages", []) if snapshot.values else []


# --------------------------------------------------------------------------
# A. wrap_tool_call middleware
# --------------------------------------------------------------------------


class TestToolErrorMiddleware:
    def test_tool_exception_becomes_error_message_and_run_continues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The tool raises a ToolException; the middleware surfaces it as an error
        # ToolMessage, so the model is called again and the run completes.
        model = ScriptedChatModel([_tool_call("failing", "call_1"), AIMessage(content="recovered")])
        _seams(monkeypatch, model, InMemorySaver())
        tool = _raising_tool(ToolException("tool said no"))

        result = asyncio.run(bta.ainvoke_tools_agent("sys", ["do it"], [tool], config=_config("t-toolexc")))

        # The loop continued past the failure and the model produced a final answer.
        assert result.output == "recovered"

    def test_tool_exception_error_message_is_visible_in_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        saver = InMemorySaver()
        model = ScriptedChatModel([_tool_call("failing", "call_1"), AIMessage(content="recovered")])
        _seams(monkeypatch, model, saver)
        tool = _raising_tool(ToolException("tool said no"))

        result = asyncio.run(bta.ainvoke_tools_agent("sys", ["do it"], [tool], config=_config("t-toolexc")))
        assert result.output == "recovered"

        messages = asyncio.run(_state_messages(saver, model, "t-toolexc"))
        errors = [m for m in messages if isinstance(m, ToolMessage) and m.status == "error"]
        assert len(errors) == 1
        assert errors[0].tool_call_id == "call_1"
        assert "tool said no" in errors[0].content

    def test_tool_exception_is_logged_before_conversion(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The recovery path is never silent: a warning names the tool, the tool_call
        # id, and the error before the failure becomes a model-visible ToolMessage.
        model = ScriptedChatModel([_tool_call("failing", "call_1"), AIMessage(content="recovered")])
        _seams(monkeypatch, model, InMemorySaver())
        tool = _raising_tool(ToolException("tool said no"))

        with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
            asyncio.run(bta.ainvoke_tools_agent("sys", ["do it"], [tool], config=_config("t-toolexc")))

        assert any(
            "failing" in r.getMessage() and "call_1" in r.getMessage() and "tool said no" in r.getMessage()
            for r in caplog.records
        )

    def test_validation_error_leg_converts_and_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        # The second arm of the typed catch: a pydantic ValidationError out of the
        # tool invocation is converted to an error ToolMessage and logged. Exercised
        # directly on the middleware — langchain's ToolNode pre-converts a body-raised
        # ValidationError to a ToolInvocationError before this hook, so only a handler
        # that raises one reaches this leg.
        request = ToolCallRequest(
            tool_call={"id": "call_v", "name": "failing", "args": {}},
            tool=None,
            state={"messages": []},
            runtime=None,  # type: ignore[arg-type]
        )
        exc = _validation_error()

        async def handler(_req: ToolCallRequest) -> ToolMessage:
            raise exc

        with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
            result = asyncio.run(rec._tool_error_middleware.awrap_tool_call(request, handler))

        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert result.tool_call_id == "call_v"
        assert result.name == "failing"
        assert str(exc) in result.content
        assert any("failing" in r.getMessage() and "call_v" in r.getMessage() for r in caplog.records)

    def test_preset_base_tool_failure_becomes_error_message_and_run_continues(
        self, monkeypatch: pytest.MonkeyPatch, app_tools: Any
    ) -> None:
        # A PRESET is a tool like any other from the model's side, so its base tool failing
        # must reach the model as an error ToolMessage and let the turn finish — the same
        # contract a directly-bound tool has: an untyped exception out of the adapter would
        # abort the run and leave the thread holding a checkpointed dangling tool_call.
        saver = InMemorySaver()
        model = ScriptedChatModel([_tool_call("my_preset", "call_1"), AIMessage(content="recovered")])
        _seams(monkeypatch, model, saver)
        app_tools.client_tools["base"] = _raising_tool(RuntimeError("unused"), name="base")

        def boom(**_kwargs: Any) -> Any:
            raise ValueError("base tool down")

        app_tools.tool_runners["base"] = boom
        preset = PresetSpec(name="my_preset", description="run a preset", base_tool="base", fixed_kwargs={})
        (preset_tool,) = asyncio.run(resolve_tools(app_tools, [], [], [preset]))

        result = asyncio.run(bta.ainvoke_tools_agent("sys", ["do it"], [preset_tool], config=_config("t-preset-fail")))

        # The loop continued past the base tool's failure and the model produced an answer.
        assert result.output == "recovered"
        messages = asyncio.run(_state_messages(saver, model, "t-preset-fail"))
        errors = [m for m in messages if isinstance(m, ToolMessage) and m.status == "error"]
        assert len(errors) == 1
        assert errors[0].tool_call_id == "call_1"
        # Named for the tool the MODEL called (the preset), carrying the base tool's text.
        assert "Error calling tool 'my_preset': base tool down" in errors[0].content

    def test_runtime_error_propagates_and_aborts_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A non-tool-logic failure is NOT masked: it propagates and aborts the run.
        model = ScriptedChatModel([_tool_call("failing", "call_1"), AIMessage(content="unreached")])
        _seams(monkeypatch, model, InMemorySaver())
        tool = _raising_tool(RuntimeError("infra down"))

        with pytest.raises(RuntimeError, match="infra down"):
            asyncio.run(bta.ainvoke_tools_agent("sys", ["do it"], [tool], config=_config("t-runtime")))


# --------------------------------------------------------------------------
# B. turn-start repair of dangling tool_calls
# --------------------------------------------------------------------------


class TestDanglingToolCallRepair:
    async def _seed_dangling(self, saver: InMemorySaver, model: BaseChatModel, thread_id: str) -> None:
        """Checkpoint a thread whose last message is an ``AIMessage`` with an
        unanswered tool_call — the poisoned shape an aborted turn leaves behind."""
        agent, _, config = await bta._build_agent_and_input("sys", ["x"], [], config=_config(thread_id))
        await agent.aupdate_state(
            config,
            {"messages": [HumanMessage(content="first question"), _tool_call("failing", "call_x")]},
            as_node=START,
        )

    def test_repair_answers_dangling_call_before_new_turn(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        saver = InMemorySaver()
        model = ScriptedChatModel([AIMessage(content="ok")])
        _seams(monkeypatch, model, saver)
        asyncio.run(self._seed_dangling(saver, model, "t-repair"))

        with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
            result = asyncio.run(bta.ainvoke_tools_agent("sys", ["second question"], [], config=_config("t-repair")))

        # The run completed on the previously poisoned thread.
        assert result.output == "ok"

        messages = asyncio.run(_state_messages(saver, model, "t-repair"))
        repairs = [m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "call_x"]
        assert len(repairs) == 1
        assert repairs[0].status == "error"
        assert repairs[0].content == rec._INTERRUPTED_TOOL_RESULT

        # The synthetic answer lands BEFORE the new user input.
        repair_index = messages.index(repairs[0])
        second = next(m for m in messages if isinstance(m, HumanMessage) and m.content == "second question")
        assert repair_index < messages.index(second)

        # The repair is announced, naming the thread and the repaired tool_call_id.
        assert any(
            "repairing dangling tool_calls" in r.message and "t-repair" in r.getMessage() and "call_x" in r.getMessage()
            for r in caplog.records
        )

    def test_fresh_thread_is_untouched(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        saver = InMemorySaver()
        model = ScriptedChatModel([AIMessage(content="hi")])
        _seams(monkeypatch, model, saver)

        with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
            result = asyncio.run(bta.ainvoke_tools_agent("sys", ["hello"], [], config=_config("t-fresh")))

        assert result.output == "hi"
        # No repair ran on a thread with no prior state.
        assert not any("repairing dangling tool_calls" in r.getMessage() for r in caplog.records)
        messages = asyncio.run(_state_messages(saver, model, "t-fresh"))
        assert not any(isinstance(m, ToolMessage) for m in messages)

    def test_healthy_thread_is_untouched(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A thread whose last checkpointed message is a completed AIMessage (no
        # pending tool_calls) is not a poisoned thread, so no repair runs.
        saver = InMemorySaver()
        model = ScriptedChatModel([AIMessage(content="second answer")])
        _seams(monkeypatch, model, saver)

        async def seed() -> None:
            agent, _, config = await bta._build_agent_and_input("sys", ["x"], [], config=_config("t-healthy"))
            await agent.aupdate_state(
                config,
                {"messages": [HumanMessage(content="first"), AIMessage(content="first answer")]},
                as_node=START,
            )

        asyncio.run(seed())

        with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
            result = asyncio.run(bta.ainvoke_tools_agent("sys", ["second"], [], config=_config("t-healthy")))

        assert result.output == "second answer"
        assert not any("repairing dangling tool_calls" in r.getMessage() for r in caplog.records)
        messages = asyncio.run(_state_messages(saver, model, "t-healthy"))
        assert not any(isinstance(m, ToolMessage) for m in messages)


class TestRepairHelper:
    async def _collect(self, agen: AsyncIterator[Any]) -> list[Any]:
        return [chunk async for chunk in agen]

    def test_repair_via_events_face_heals_dangling_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The production streaming face builds through the same choke point, so a
        # poisoned thread is repaired before it streams.
        saver = InMemorySaver()
        model = ScriptedChatModel([AIMessage(content="ok")])
        _seams(monkeypatch, model, saver)

        async def seed() -> None:
            agent, _, config = await bta._build_agent_and_input("sys", ["x"], [], config=_config("t-events"))
            await agent.aupdate_state(
                config,
                {"messages": [HumanMessage(content="first"), _tool_call("failing", "call_e")]},
                as_node=START,
            )

        asyncio.run(seed())

        stream = astream_tools_agent_events("sys", ["second"], [], config=_config("t-events"))
        events = asyncio.run(self._collect(stream))
        assert events

        messages = asyncio.run(_state_messages(saver, model, "t-events"))
        repairs = [m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "call_e"]
        assert len(repairs) == 1
        assert repairs[0].status == "error"

    def test_paused_interrupt_thread_is_not_repaired(self, caplog: pytest.LogCaptureFixture) -> None:
        # A human-in-the-loop pause legitimately owns its unanswered tool_calls until
        # it resumes, so the repair must leave a really-paused thread untouched —
        # pinned against real langgraph pause semantics, not a stub.
        from langchain.agents import create_agent
        from langchain_core.runnables import RunnableConfig
        from langgraph.types import Command, interrupt

        async def _ask(**_: Any) -> str:
            return interrupt("need input")

        ask = StructuredTool.from_function(func=None, coroutine=_ask, name="ask", description="d")
        model = ScriptedChatModel([_tool_call("ask", "call_i"), AIMessage(content="final")])
        agent = create_agent(model, tools=[ask], checkpointer=InMemorySaver())
        config: RunnableConfig = {"configurable": {"thread_id": "t-paused"}}

        # (a) run to the pause: the last message is the dangling AIMessage and the
        # thread carries a pending interrupt.
        paused = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="hi")]}, config))
        assert "__interrupt__" in paused
        assert asyncio.run(agent.aget_state(config)).interrupts

        # (b) the repair leaves the paused thread untouched — no synthetic message, no log.
        with caplog.at_level(logging.WARNING, logger=_RECOVERY_LOGGER):
            asyncio.run(rec._repair_dangling_tool_calls(agent, {"configurable": {"thread_id": "t-paused"}}))
        after = asyncio.run(agent.aget_state(config)).values["messages"]
        assert not any(isinstance(m, ToolMessage) and m.content == rec._INTERRUPTED_TOOL_RESULT for m in after)
        assert not any("repairing dangling tool_calls" in r.getMessage() for r in caplog.records)

        # (c) resuming the interrupt still completes with the real tool answer.
        resumed = asyncio.run(agent.ainvoke(Command(resume="the answer"), config))
        assert resumed["messages"][-1].content == "final"
        assert any(isinstance(m, ToolMessage) and m.content == "the answer" for m in resumed["messages"])
