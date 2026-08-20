"""Tests for the ``refine_agent`` — registration, tool resolution, the
Evaluator↔Critic loop, the approval-token detection, and the streamed
final-evaluator-pass event taxonomy.

No live model or network: ``create_agent`` and the kit LLM/checkpoint/logging
seams are monkeypatched so the loop runs against scripted fake evaluator/critic
agents. Fake evaluators/critics return canned ``ainvoke`` states; the fake
evaluator's ``astream`` replays a fixed ``(mode, chunk)`` script that
``aproject_agent_events`` decodes into the contract event vocabulary.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr, ValidationError
from tai42_contract.agent import (
    Agent,
    MessageDelta,
    MessageFinal,
    ReasoningStep,
    RunUsage,
    StreamEvent,
    StructuredFinal,
    ToolCallStep,
    ToolResultStep,
)
from tai42_contract.app import tai42_app
from tai42_kit.llm.middleware.system_purge import SystemPurgeMiddleware
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError

import tai42_agents.refine_agent.agent as agent_mod
from tai42_agents._internal.reject import reject_unhonored
from tai42_agents.refine_agent.agent import RefineAgent, RefineAgentInput
from tai42_agents.refine_agent.prompt import (
    CRITIC_APPROVAL_MESSAGE,
    CRITIC_SYSTEM_MESSAGE,
    EVALUATOR_SYSTEM_MESSAGE,
)

AGENT_NAME = "refine_agent"


class FakeAgent:
    """A stand-in compiled LangGraph agent.

    ``ainvoke`` pops the next canned content off ``invoke_contents`` and wraps it
    in a one-message state (what ``build_user_output`` reads). ``astream`` replays
    a fixed list of ``(mode, chunk)`` items for the final-pass projection.
    """

    def __init__(
        self, invoke_contents: list[str], stream_items: list[Any] | None = None, history: list[Any] | None = None
    ) -> None:
        self._invoke_contents = list(invoke_contents)
        self._stream_items = list(stream_items or [])
        self._history = list(history or [])
        self.ainvoke_inputs: list[Any] = []
        self.astream_inputs: list[Any] = []
        self.get_state_calls = 0

    async def ainvoke(self, agent_input: Any, config: Any) -> dict[str, Any]:
        self.ainvoke_inputs.append(agent_input)
        content = self._invoke_contents.pop(0)
        return {"messages": [AIMessage(content=content)]}

    async def astream(self, agent_input: Any, config: Any, stream_mode: Any = None):
        self.astream_inputs.append(agent_input)
        for item in self._stream_items:
            yield item

    async def aget_state(self, config: Any) -> Any:
        # The structured final-pass reads the loop thread's negotiation history from
        # the checkpoint here; a fake snapshot exposes ``.values["messages"]``.
        self.get_state_calls += 1
        return SimpleNamespace(values={"messages": list(self._history)})


class CreateAgentRecorder:
    """A ``create_agent`` replacement: hands out queued fakes and records the
    tools, per-run ``system_prompt``, and ``response_format`` each was compiled
    with (the latter is ``None`` on the text-loop evaluator/critic and the
    schema strategy only on the structured final pass)."""

    def __init__(self, agents: list[FakeAgent]) -> None:
        self._queue = list(agents)
        self.tools_per_call: list[list[Any]] = []
        self.system_prompts: list[Any] = []
        self.middlewares_per_call: list[list[Any]] = []
        self.response_formats: list[Any] = []

    def __call__(
        self,
        llm: Any,
        *,
        tools: Any,
        checkpointer: Any,
        middleware: Any,
        debug: Any,
        response_format: Any = None,
        system_prompt: Any = None,
    ) -> FakeAgent:
        self.tools_per_call.append(list(tools))
        self.system_prompts.append(system_prompt)
        self.middlewares_per_call.append(list(middleware))
        self.response_formats.append(response_format)
        return self._queue.pop(0)


class _ProviderSettings:
    llm = "fake-llm"
    checkpoint = "fake-ckpt"
    checkpoint_conn_string = None


class _LlmSettings:
    def with_fallbacks(self, kwargs: dict[str, Any] | None) -> dict[str, Any]:
        return {}


class _CheckpointRegistry:
    async def get_checkpointer(self, provider: str, conn_string: Any) -> None:
        return None


class _LoggingSettings:
    def is_enabled_for(self, level: str) -> bool:
        return False


def _patch_loop(monkeypatch: pytest.MonkeyPatch, agents: list[FakeAgent]) -> CreateAgentRecorder:
    """Monkeypatch every non-loop seam so ``_run_refine_loop`` runs offline
    against the supplied fake evaluator/critic. Returns the create-agent recorder
    (evaluator is compiled first, critic second)."""
    recorder = CreateAgentRecorder(agents)
    monkeypatch.setattr(agent_mod, "create_agent", recorder)

    async def _get_llm_async(provider: str, **kwargs: Any) -> str:
        return "llm"

    monkeypatch.setattr(agent_mod, "get_llm_async", _get_llm_async)
    monkeypatch.setattr(agent_mod, "checkpoint_registry", lambda: _CheckpointRegistry())
    monkeypatch.setattr(agent_mod, "context_overflow_middlewares", lambda system_prompt=None: [])
    monkeypatch.setattr(agent_mod, "logging_settings", lambda: _LoggingSettings())
    monkeypatch.setattr(agent_mod, "llm_provider_settings", lambda: _ProviderSettings())
    monkeypatch.setattr(agent_mod, "llm_settings", lambda: _LlmSettings())

    def _init_config(config: dict[str, Any] | None) -> dict[str, Any]:
        return config or {"configurable": {"thread_id": "t"}}

    monkeypatch.setattr(agent_mod, "init_langgraph_config", _init_config)
    return recorder


def _final_pass_script() -> list[Any]:
    """A final-evaluator ``astream`` script exercising the whole event taxonomy:
    reasoning, a tool call + result, token deltas, and a structured response."""
    reasoning_and_call = AIMessage(
        content=[{"type": "thinking", "thinking": "weighing the options"}],
        tool_calls=[{"id": "c1", "name": "lookup", "args": {"q": 1}}],
    )
    usage_message = AIMessage(
        content="",
        usage_metadata={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
        response_metadata={"model_name": "fake-model"},
    )
    tool_result = ToolMessage(content="tool said hi", name="lookup", tool_call_id="c1")
    return [
        ("updates", {"model": {"messages": [reasoning_and_call]}}),
        ("updates", {"tools": {"messages": [tool_result]}}),
        ("updates", {"model": {"messages": [usage_message]}}),
        ("messages", (AIMessageChunk(content="Final "), {})),
        ("messages", (AIMessageChunk(content="answer"), {})),
        ("updates", {"model": {"messages": [], "structured_response": {"ok": True}}}),
    ]


def _collect(agent: Agent, **kwargs: Any) -> list[StreamEvent]:
    async def go() -> list[StreamEvent]:
        return [event async for event in agent.astream(**kwargs)]

    return asyncio.run(go())


def _a_tool() -> StructuredTool:
    def _f(x: int) -> int:
        return x

    return StructuredTool.from_function(func=_f, name="t1", description="a tool")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_decorator_registers_a_live_instance() -> None:
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    assert isinstance(agent, RefineAgent)
    assert isinstance(agent, Agent)
    assert agent.tool_name == AGENT_NAME


# ---------------------------------------------------------------------------
# Tool resolution
# ---------------------------------------------------------------------------


def test_tools_are_resolved_by_name_and_passed_to_both_agents(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    tool = _a_tool()
    app_tools.client_tools["t1"] = tool
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="ok"), {}))])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    recorder = _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    _collect(agent, evaluator_message="write it", critic_message="review it", tool_names=["t1"])

    assert recorder.tools_per_call == [[tool], [tool]]  # evaluator, then critic


def test_unknown_tool_name_raises(monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any) -> None:
    evaluator = FakeAgent(invoke_contents=["draft"])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match="unknown client tools"):
        _collect(agent, evaluator_message="write it", critic_message="review it", tool_names=["missing"])


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def test_approval_on_first_iteration_streams_the_final_pass(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=_final_pass_script())
    critic = FakeAgent(invoke_contents=[f"looks great {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(agent, evaluator_message="write it", critic_message="review it")

    # loop ran exactly one evaluator+critic round, then the final pass streamed.
    assert len(evaluator.ainvoke_inputs) == 1
    assert len(critic.ainvoke_inputs) == 1
    assert len(evaluator.astream_inputs) == 1
    assert any(isinstance(e, MessageFinal) for e in events)


def test_user_content_kwargs_mark_the_evaluators_first_user_turn(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """``user_content_kwargs`` makes the evaluator's first user turn a content block
    (the run's primary caller message); the internal system prompts are untouched."""
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=_final_pass_script())
    critic = FakeAgent(invoke_contents=[f"looks great {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    _collect(
        agent,
        evaluator_message="write it",
        critic_message="review it",
        user_content_kwargs={"cache_control": {"type": "ephemeral"}},
    )

    assert evaluator.ainvoke_inputs[0] == {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "write it", "cache_control": {"type": "ephemeral"}}]}
        ]
    }


def test_approval_on_a_later_iteration(monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any) -> None:
    evaluator = FakeAgent(
        invoke_contents=["draft-1", "draft-2", "draft-3"],
        stream_items=[("messages", (AIMessageChunk(content="done"), {}))],
    )
    critic = FakeAgent(invoke_contents=["needs work", "still off", f"ok {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(agent, evaluator_message="write it", critic_message="review it", max_iterations=5)

    assert len(critic.ainvoke_inputs) == 3  # approved on the third round
    assert len(evaluator.ainvoke_inputs) == 3
    assert any(isinstance(e, MessageFinal) and e.text == "done" for e in events)


def test_max_iterations_without_approval_raises(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    evaluator = FakeAgent(invoke_contents=["d1", "d2"])
    critic = FakeAgent(invoke_contents=["nope", "still nope"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match=r"Max iterations \(2\) reached without critic approval"):
        _collect(agent, evaluator_message="write it", critic_message="review it", max_iterations=2)

    assert len(evaluator.ainvoke_inputs) == 2  # both budgeted rounds attempted
    assert len(critic.ainvoke_inputs) == 2


def test_empty_critic_feedback_raises(monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any) -> None:
    evaluator = FakeAgent(invoke_contents=["draft"])
    critic = FakeAgent(invoke_contents=[""])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match="No critic feedback found"):
        _collect(agent, evaluator_message="write it", critic_message="review it")


# ---------------------------------------------------------------------------
# Approval-token detection (single case-sensitive policy)
# ---------------------------------------------------------------------------


def test_exact_token_substring_is_recognized_as_approval(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """The exact token embedded in surrounding text counts as approval, even at
    the last budgeted iteration — the check must actually detect it."""
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="done"), {}))])
    critic = FakeAgent(invoke_contents=[f"All good here: {CRITIC_APPROVAL_MESSAGE} — ship it"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(agent, evaluator_message="write it", critic_message="review it", max_iterations=1)

    assert any(isinstance(e, MessageFinal) and e.text == "done" for e in events)


def test_wrong_case_token_is_not_approval(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """Detection is case-sensitive against the exact sentinel: an upper-cased
    look-alike is NOT approval, so the run fails loudly at max_iterations."""
    evaluator = FakeAgent(invoke_contents=["draft"])
    critic = FakeAgent(invoke_contents=[CRITIC_APPROVAL_MESSAGE.upper()])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match="Max iterations"):
        _collect(agent, evaluator_message="write it", critic_message="review it", max_iterations=1)


# ---------------------------------------------------------------------------
# Final-pass event taxonomy
# ---------------------------------------------------------------------------


def test_final_pass_emits_the_full_event_taxonomy(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=_final_pass_script())
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(agent, evaluator_message="write it", critic_message="review it")

    assert [type(e) for e in events] == [
        ReasoningStep,
        ToolCallStep,
        ToolResultStep,
        RunUsage,
        MessageDelta,
        MessageDelta,
        MessageFinal,
        StructuredFinal,
    ]
    reasoning = next(e for e in events if isinstance(e, ReasoningStep))
    assert reasoning.text == "weighing the options"
    call = next(e for e in events if isinstance(e, ToolCallStep))
    assert call.tool == "lookup"
    assert call.call_id == "c1"
    result = next(e for e in events if isinstance(e, ToolResultStep))
    assert result.call_id == "c1"
    assert result.result == "tool said hi"
    usage = next(e for e in events if isinstance(e, RunUsage))
    assert usage.total_tokens == 12
    assert usage.model == "fake-model"
    final = next(e for e in events if isinstance(e, MessageFinal))
    assert final.text == "Final answer"
    structured = next(e for e in events if isinstance(e, StructuredFinal))
    assert structured.data == {"ok": True}


# ---------------------------------------------------------------------------
# run() drains the stream to the final value
# ---------------------------------------------------------------------------


def test_run_drains_stream_to_final_text(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    evaluator = FakeAgent(
        invoke_contents=["draft"],
        stream_items=[
            ("messages", (AIMessageChunk(content="Final "), {})),
            ("messages", (AIMessageChunk(content="answer"), {})),
        ],
    )
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    result = asyncio.run(agent.run(evaluator_message="write it", critic_message="review it"))
    assert result == "Final answer"


_REFINE_SCHEMA = {"title": "Answer", "type": "object", "properties": {"answer": {"type": "string"}}}


def _structured_stream_items(payload: dict[str, Any]) -> list[Any]:
    """A structured final-pass ``astream`` script: one updates chunk carrying the
    forced structured response the projection turns into a ``StructuredFinal``."""
    return [("updates", {"model": {"messages": [], "structured_response": payload}})]


def test_run_with_response_format_forces_final_answer_on_a_fresh_thread(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """With a ``response_format`` set the loop iterations stay text and only the final
    approved answer is forced: a SECOND, structured evaluator (a distinct graph built
    with the schema) runs the final pass, fed the loop thread's negotiation history
    EXPLICITLY as input (read from the checkpoint) rather than via a cross-topology
    checkpoint resume, and ``run`` returns the validated structured object."""
    history = [AIMessage(content="draft under negotiation")]
    evaluator = FakeAgent(invoke_contents=["draft"], history=history)
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    structured = FakeAgent(invoke_contents=[], stream_items=_structured_stream_items({"answer": "final"}))
    recorder = _patch_loop(monkeypatch, [evaluator, critic, structured])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    result = asyncio.run(
        agent.run(evaluator_message="write it", critic_message="review it", response_format=_REFINE_SCHEMA)
    )

    assert result == {"answer": "final"}
    # Only the THIRD create_agent call (the structured final pass) carried the
    # schema — pinned to the tool-calling strategy, never provider-dependent
    # auto-routing; the text-loop evaluator and critic were built without one.
    assert recorder.response_formats[:2] == [None, None]
    structured_format = recorder.response_formats[2]
    assert isinstance(structured_format, ToolStrategy)
    # The schema is pinned as a TypedDict whose parse round-trips a value back to
    # the raw-schema dict shape (int64 bounds injected on the way).
    from pydantic import TypeAdapter

    assert TypeAdapter(structured_format.schema).validate_python({"answer": "x"}) == {"answer": "x"}
    # The loop's evaluator (not the structured graph) is read via aget_state twice —
    # once by the turn-start repair before the loop, once to read history for the
    # structured pass — and the structured pass is a DISTINCT graph (no cross-topology
    # resume of the text-loop thread).
    assert evaluator.get_state_calls == 2
    assert structured is not evaluator
    # The structured pass was fed the negotiation history + the approval prompt as
    # EXPLICIT input, not resumed from the loop thread's checkpoint.
    fed = structured.astream_inputs[0]["messages"]
    assert fed[0] is history[0]
    assert fed[-1] == {"role": "user", "content": "Critic Approved."}


def test_role_prompts_are_per_run_config_with_purge_middleware_and_system_free_inputs(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """Every ``create_agent`` call carries its role's system message as the graph's
    per-run ``system_prompt`` — never as an input message — plus a leading
    ``SystemPurgeMiddleware``, and the loop's agent inputs are system-free, so no
    system message ever enters checkpointed thread state."""
    history = [AIMessage(content="draft under negotiation")]
    evaluator = FakeAgent(invoke_contents=["draft"], history=history)
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    structured = FakeAgent(invoke_contents=[], stream_items=_structured_stream_items({"answer": "final"}))
    recorder = _patch_loop(monkeypatch, [evaluator, critic, structured])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    asyncio.run(agent.run(evaluator_message="write it", critic_message="review it", response_format=_REFINE_SCHEMA))

    # The evaluator, critic, and structured final pass each compiled with their
    # role's per-run system prompt.
    assert recorder.system_prompts == [EVALUATOR_SYSTEM_MESSAGE, CRITIC_SYSTEM_MESSAGE, EVALUATOR_SYSTEM_MESSAGE]
    # Each graph's middleware stack leads with the system purge, so a stored
    # system message never reaches the model alongside the per-run prompt.
    assert len(recorder.middlewares_per_call) == 3
    for middleware in recorder.middlewares_per_call:
        assert isinstance(middleware[0], SystemPurgeMiddleware)
    # The loop feeds its agents only user turns and prior conversation history —
    # never a system message that would become checkpointed state.
    loop_inputs = [*evaluator.ainvoke_inputs, *critic.ainvoke_inputs, *structured.astream_inputs]
    assert loop_inputs
    for agent_input in loop_inputs:
        for message in agent_input["messages"]:
            if isinstance(message, dict):
                assert message["role"] != "system"
            else:
                assert not isinstance(message, SystemMessage)


def test_run_with_response_format_but_no_structured_raises_loudly(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """A requested ``response_format`` the final pass produced none for raises loudly
    rather than returning the approved text."""
    evaluator = FakeAgent(invoke_contents=["draft"], history=[AIMessage(content="ctx")])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    # The structured pass streams only text — no structured_response is written.
    structured = FakeAgent(invoke_contents=[], stream_items=[("messages", (AIMessageChunk(content="text"), {}))])
    _patch_loop(monkeypatch, [evaluator, critic, structured])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match="no structured output"):
        asyncio.run(agent.run(evaluator_message="write it", critic_message="review it", response_format=_REFINE_SCHEMA))


def test_astream_with_response_format_emits_one_structured_final(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """The stream face surfaces exactly one terminal ``StructuredFinal`` from the
    structured final pass."""
    evaluator = FakeAgent(invoke_contents=["draft"], history=[AIMessage(content="ctx")])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    structured = FakeAgent(invoke_contents=[], stream_items=_structured_stream_items({"answer": "final"}))
    _patch_loop(monkeypatch, [evaluator, critic, structured])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(agent, evaluator_message="write it", critic_message="review it", response_format=_REFINE_SCHEMA)
    finals = [e for e in events if isinstance(e, StructuredFinal)]
    assert len(finals) == 1
    assert finals[0].data == {"answer": "final"}


def test_astream_with_response_format_but_no_structured_raises_loudly(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """A requested ``response_format`` the final pass produced no ``StructuredFinal``
    for raises loudly after the stream drains — in parity with ``run`` — rather than
    silently omitting the frame."""
    evaluator = FakeAgent(invoke_contents=["draft"], history=[AIMessage(content="ctx")])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    # The structured pass streams only text — no structured_response is written.
    structured = FakeAgent(invoke_contents=[], stream_items=[("messages", (AIMessageChunk(content="text"), {}))])
    _patch_loop(monkeypatch, [evaluator, critic, structured])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match="no structured output"):
        _collect(agent, evaluator_message="write it", critic_message="review it", response_format=_REFINE_SCHEMA)


def test_astream_nonconforming_structured_raises(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """A structured final pass violating a schema constraint keyword raises loudly
    from the projection's validation step — pinning that ``response_format`` is
    threaded into the final pass's projection."""
    schema = {
        "title": "Answer",
        "type": "object",
        "properties": {"answer": {"type": "string", "minLength": 1}},
        "required": ["answer"],
    }
    evaluator = FakeAgent(invoke_contents=["draft"], history=[AIMessage(content="ctx")])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    structured = FakeAgent(invoke_contents=[], stream_items=_structured_stream_items({"answer": ""}))
    _patch_loop(monkeypatch, [evaluator, critic, structured])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(JsonSchemaValidationError):
        _collect(agent, evaluator_message="write it", critic_message="review it", response_format=schema)


def test_run_response_format_without_title_raises_loudly(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """A ``response_format`` dict lacking a top-level ``"title"`` is rejected loudly at
    the run seam before the loop runs."""
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(ValueError, match="top-level 'title'"):
        asyncio.run(
            agent.run(evaluator_message="write it", critic_message="review it", response_format={"type": "object"})
        )


def test_astream_response_format_without_title_raises_loudly(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """The streaming face — the one the public run door drives — rejects an untitled
    ``response_format`` up front, exactly as the invoke face does."""
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(ValueError, match="top-level 'title'"):
        _collect(agent, evaluator_message="write it", critic_message="review it", response_format={"type": "object"})


def test_astream_rejects_oneof_response_format_with_untitled_variant(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """A ``oneOf`` ``response_format`` whose variants lack titles (each binds its own
    structured-output name) is rejected up front, even though the container is
    titled."""
    schema = {"title": "Top", "oneOf": [{"title": "A", "type": "object"}, {"type": "object"}]}
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(ValueError, match="oneOf variants must each"):
        _collect(agent, evaluator_message="write it", critic_message="review it", response_format=schema)


# ---------------------------------------------------------------------------
# Contract-parameter parity: honor tool_names on both faces; raise on the rest
# ---------------------------------------------------------------------------


def test_tool_names_honored_on_run_face(monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any) -> None:
    """The ``run`` face resolves ``tool_names`` and compiles both agents with the
    live tool — parity with the ``astream`` face, not a silent drop."""
    tool = _a_tool()
    app_tools.client_tools["t1"] = tool
    evaluator = FakeAgent(
        invoke_contents=["draft"],
        stream_items=[("messages", (AIMessageChunk(content="ok"), {}))],
    )
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    recorder = _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    asyncio.run(agent.run(evaluator_message="write it", critic_message="review it", tool_names=["t1"]))

    assert recorder.tools_per_call == [[tool], [tool]]  # evaluator, then critic


# Each unhonored contract parameter with a MEANINGFUL value — a truthy collection or
# a not-``None`` scalar — plus two falsy-but-meaningful scalars (``strategy=""``,
# ``resume=False``) that must still raise (they are set whenever not ``None``, so they
# never slip through a truthiness gate). ``X=None`` / ``X=()`` are the ABC's own
# "not requested" sentinels and are pinned to PASS by the tests below.
_UNHONORED_CASES = [
    ("tools", [object()]),
    ("presets", [object()]),
    ("subagents", [object()]),
    ("strategy", "react"),
    ("strategy", ""),
    ("system_message", "sys"),
    ("user_message", "usr"),
    ("interrupt_on", {"tool": True}),
    ("skills", ["s"]),
    ("inline_skills", [{"name": "s", "content": "c"}]),
    ("recursion_limit", 0),
    ("thread_id", "t"),
    ("resume", False),
    ("resume_checkpoint_id", "cp"),
    ("llm_provider", "openai"),
    ("store_provider", "redis"),
    ("llm_kwargs", {"model": "x"}),
    ("system_content_kwargs", {"cache_control": {"type": "ephemeral"}}),
]


@pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
def test_astream_rejects_unhonored_contract_param(
    param: str, value: Any, app_tools: Any, resource_manager: Any
) -> None:
    """Every contract ``Agent.run`` parameter with no seat in the role-named loop
    raises loudly on the stream face, naming the offending parameter and the exact
    ``astream`` face it was called on. Falsy-but-meaningful scalars (``strategy=""``,
    ``resume=False``) still raise."""
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match=rf"refine_agent\.astream does not support .*\b{param}\b"):
        _collect(agent, evaluator_message="write it", critic_message="review it", **{param: value})


@pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
def test_run_rejects_unhonored_contract_param(param: str, value: Any, app_tools: Any, resource_manager: Any) -> None:
    """The ``run`` face rejects the same unsupported parameters, naming the exact
    ``run`` face — the run-face guard is load-bearing (were it dropped, the delegated
    ``astream`` guard would surface the ``astream`` token and fail this assertion)."""
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match=rf"refine_agent\.run does not support .*\b{param}\b"):
        # The base ``Agent.run`` signature types some of these non-optional, so the mismatch is expected.
        asyncio.run(agent.run(evaluator_message="write it", critic_message="review it", **{param: value}))  # type: ignore[arg-type]


def test_unhonored_cases_cover_the_full_reasons_map() -> None:
    """Every key in the guard's reasons map has a parametrized reject case, so a key
    added to the map without a matching test fails here immediately."""
    assert {param for param, _ in _UNHONORED_CASES} == set(agent_mod._UNHONORED_REASONS)


# The unhonored params whose ABC ``Agent.run`` default is an empty collection (``()`` /
# ``""``), read from the contract signature — the independent source of truth for which
# unhonored params are collection-typed. Intersected with this agent's reasons map it is
# exactly the set ``_UNHONORED_COLLECTION_PARAMS`` must classify as collections. The
# empty-collection test below parametrizes from HERE, not from the frozenset, so a member
# dropped from the frozenset (reclassifying it as a scalar) turns a case red rather than
# silently vanishing.
_EMPTY_COLLECTION_ABC_DEFAULTS = frozenset(
    name
    for name, parameter in inspect.signature(Agent.run).parameters.items()
    if isinstance(parameter.default, (tuple, list, str)) and not parameter.default
)
_COLLECTION_REJECT_PARAMS = sorted(agent_mod._UNHONORED_REASONS.keys() & _EMPTY_COLLECTION_ABC_DEFAULTS)


def test_collection_params_match_the_abc_collection_defaults() -> None:
    """``_UNHONORED_COLLECTION_PARAMS`` is exactly this agent's unhonored params whose ABC
    default is an empty collection: no scalar wrongly listed (which would let a meaningful
    falsy value slip through), none dropped (which would over-reject the not-requested
    empty default)."""
    assert set(agent_mod._UNHONORED_COLLECTION_PARAMS) == set(_COLLECTION_REJECT_PARAMS)


@pytest.mark.parametrize("empty", [[], ""])
@pytest.mark.parametrize("param", _COLLECTION_REJECT_PARAMS)
def test_reject_unhonored_permits_empty_collection_param(param: str, empty: object) -> None:
    """An empty collection is the ABC's "not requested" default for a collection parameter,
    so the guard does not raise for it — in either falsy empty form (``[]`` / ``""``). Were
    the parameter dropped from ``_UNHONORED_COLLECTION_PARAMS`` it would be classified as a
    scalar (set whenever it is not ``None``) and this empty value would raise."""
    reject_unhonored(
        "refine_agent.run",
        {param: empty},
        agent_mod._UNHONORED_REASONS,
        collection_params=agent_mod._UNHONORED_COLLECTION_PARAMS,
    )


@pytest.mark.parametrize(
    "param", ["response_format", "strategy", "thread_id", "resume", "llm_provider", "recursion_limit"]
)
def test_unhonored_scalar_none_passes_the_guard(param: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ABC's ``None`` "not requested" sentinel passes the guard rather than being
    over-rejected: an explicit ``response_format=None`` / ``resume=None`` / … reaches
    the loop and completes, in parity with the LangGraph-backed agents."""
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="ok"), {}))])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(agent, evaluator_message="write it", critic_message="review it", **{param: None})
    assert any(isinstance(e, MessageFinal) for e in events)


def test_unhonored_collection_empty_passes_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty collection is the ABC's "not requested" sentinel and passes the guard:
    ``tools=()`` / ``presets=[]`` reach the loop rather than raising."""
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="ok"), {}))])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(agent, evaluator_message="write it", critic_message="review it", tools=(), presets=[])
    assert any(isinstance(e, MessageFinal) for e in events)


def test_extension_kwarg_is_not_rejected(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """A plain extension kwarg beyond the contract names is the documented
    extension point and passes through untouched rather than raising."""
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="ok"), {}))])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(agent, evaluator_message="write it", critic_message="review it", role_specific_extra="ok")

    assert any(isinstance(e, MessageFinal) for e in events)


# ---------------------------------------------------------------------------
# Input model rejects unknown keys (extra="forbid")
# ---------------------------------------------------------------------------


def test_run_raises_when_required_evaluator_message_slot_is_unset(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """The evaluator message renders with ``allow_empty=False``: an unset slot
    (empty content AND empty id) surfaces the resource manager's fail-loud message
    rather than silently running the loop on an empty prompt. The evaluator/critic
    seams are faked so that WITHOUT render.py's ``allow_empty`` passthrough the loop
    would instead run to approval — a dropped passthrough turns this red rather than
    letting the empty prompt slip through."""
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="ok"), {}))])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(ValueError, match="must provide either"):
        asyncio.run(agent.run(critic_message="review it"))


def test_tool_input_rejects_unknown_key() -> None:
    """A typo'd key is rejected loudly at validation rather than silently ignored."""
    with pytest.raises(ValidationError, match="max_iteration"):
        RefineAgentInput.model_validate({"evaluator_message": "hi", "max_iteration": 5})


def test_empty_content_kwargs_normalize_to_none() -> None:
    """An empty ``user_content_kwargs`` dict from the JSON door reads as absent — the
    builders treat {} as no mark, so the field normalizes to None rather than a
    set-but-empty value the unhonored-reject face would misread."""
    validated = RefineAgentInput.model_validate({"evaluator_message": "hi", "user_content_kwargs": {}})
    assert validated.user_content_kwargs is None
    # A non-empty mark is a real value and rides through unchanged.
    marked = RefineAgentInput.model_validate(
        {"evaluator_message": "hi", "user_content_kwargs": {"cache_control": {"type": "ephemeral"}}}
    )
    assert marked.user_content_kwargs == {"cache_control": {"type": "ephemeral"}}


# ---------------------------------------------------------------------------
# Rolling cache mark: the evaluator graph strips accumulated marks at the model call
# ---------------------------------------------------------------------------


class _RecordingChatModel(BaseChatModel):
    """Returns a fresh ``AIMessage`` carrying a fixed content on every call (a new id
    each time, so the checkpoint reducer appends rather than dedups) and records the
    exact message list each model call received; ``bind_tools`` is a no-op."""

    _content: str = PrivateAttr()
    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    def __init__(self, content: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._content = content

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self._seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self._content))])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self


def _mark_count(messages: list[BaseMessage]) -> int:
    """Number of messages carrying a ``cache_control`` block (a cache breakpoint)."""
    return sum(
        isinstance(m.content, list) and any(isinstance(b, dict) and "cache_control" in b for b in m.content)
        for m in messages
    )


def _patch_loop_real(
    monkeypatch: pytest.MonkeyPatch,
    evaluator_model: BaseChatModel,
    critic_model: BaseChatModel,
    saver: InMemorySaver,
) -> list[Any]:
    """Patch every non-loop seam so ``_run_refine_loop`` compiles REAL ``create_agent``
    graphs (real middleware wiring) over the two provider-keyed models and one shared
    checkpointer. Returns a list the spied ``create_agent`` appends each compiled agent
    to (evaluator first, critic second, per run)."""
    created: list[Any] = []
    real_create_agent = agent_mod.create_agent

    def spy_create_agent(*args: Any, **kwargs: Any) -> Any:
        agent = real_create_agent(*args, **kwargs)
        created.append(agent)
        return agent

    monkeypatch.setattr(agent_mod, "create_agent", spy_create_agent)

    async def _get_llm_async(provider: str, **kwargs: Any) -> BaseChatModel:
        return evaluator_model if provider == "eval" else critic_model

    monkeypatch.setattr(agent_mod, "get_llm_async", _get_llm_async)

    async def _get_checkpointer(provider: str, conn_string: Any) -> InMemorySaver:
        return saver

    monkeypatch.setattr(agent_mod, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=_get_checkpointer))
    monkeypatch.setattr(agent_mod, "context_overflow_middlewares", lambda system_prompt=None: [])
    monkeypatch.setattr(agent_mod, "logging_settings", lambda: _LoggingSettings())
    monkeypatch.setattr(agent_mod, "llm_provider_settings", lambda: _ProviderSettings())
    monkeypatch.setattr(agent_mod, "llm_settings", lambda: _LlmSettings())
    # Keep the caller's thread_id so the two runs land on the same checkpointed thread.
    monkeypatch.setattr(
        agent_mod, "init_langgraph_config", lambda config: config or {"configurable": {"thread_id": "t"}}
    )
    return created


def test_evaluator_graph_rolls_accumulated_cache_marks_on_a_reused_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two refine runs on the same evaluator thread each mark their first user turn;
    the marks persist into the reused thread's history. Without rolling, the second
    run's evaluator model call would replay two user-side breakpoints and grow past the
    provider cap. The evaluator graph's ``RollingCacheMarkMiddleware`` strips every
    older mark at the model call, so the second run's draft call sends exactly one —
    the newest — while the checkpointed thread keeps both."""
    evaluator_model = _RecordingChatModel("draft")
    critic_model = _RecordingChatModel(f"ok {CRITIC_APPROVAL_MESSAGE}")
    saver = InMemorySaver()
    created = _patch_loop_real(monkeypatch, evaluator_model, critic_model, saver)

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    mark = {"cache_control": {"type": "ephemeral"}}
    eval_cfg = {"configurable": {"thread_id": "eval-t"}}
    critic_cfg = {"configurable": {"thread_id": "critic-t"}}

    def _run(message: str) -> None:
        _collect(
            agent,
            evaluator_message=message,
            critic_message="review it",
            user_content_kwargs=mark,
            evaluator_llm_provider="eval",
            critic_llm_provider="critic",
            evaluator_langgraph_config=eval_cfg,
            critic_langgraph_config=critic_cfg,
        )

    _run("first task")
    _run("second task")

    # Per run, create_agent is called for the evaluator then the critic; the second
    # run's evaluator draft is the third model call the evaluator model received
    # (run-1 loop draft, run-1 final pass, run-2 loop draft).
    second_run_draft = evaluator_model._seen[2]
    # Two user-side marks live in the replayed history; the older is stripped, so the
    # outgoing request carries exactly one breakpoint.
    assert _mark_count(second_run_draft) == 1
    newest_user = [m for m in second_run_draft if isinstance(m, HumanMessage)][-1]
    assert newest_user.content == [{"type": "text", "text": "second task", "cache_control": {"type": "ephemeral"}}]
    # The rewrite is request-scoped: the checkpointed thread still holds both marks, so
    # the next turn re-rolls from the same history rather than losing the record.
    snapshot = asyncio.run(created[-2].aget_state(eval_cfg))
    assert _mark_count(snapshot.values.get("messages", [])) == 2
