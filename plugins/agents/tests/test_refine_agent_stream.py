"""``refine_agent`` event taxonomy, run drain, response-format on both faces, and
per-run role prompts.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage
from tai42_contract.agent import (
    MessageDelta,
    MessageFinal,
    ReasoningStep,
    RunUsage,
    StructuredFinal,
    ToolCallStep,
    ToolResultStep,
)
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tai42_kit.llm.middleware.system_purge import SystemPurgeMiddleware
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError
from tests._refine_agent_support import (
    AGENT_NAME,
    FakeAgent,
    _collect,
    _final_pass_script,
    _patch_loop,
    _structured_stream_items,
)

from tai42_agents.refine_agent.prompt import (
    CRITIC_APPROVAL_MESSAGE,
    CRITIC_SYSTEM_MESSAGE,
    EVALUATOR_SYSTEM_MESSAGE,
)


def test_final_pass_emits_the_full_event_taxonomy(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=_final_pass_script())
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(
        agent, evaluator_message=TemplatedText(content="write it"), critic_message=TemplatedText(content="review it")
    )

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
    result = asyncio.run(
        agent.run(
            evaluator_message=TemplatedText(content="write it"), critic_message=TemplatedText(content="review it")
        )
    )
    assert result == "Final answer"


_REFINE_SCHEMA = {"title": "Answer", "type": "object", "properties": {"answer": {"type": "string"}}}


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
        agent.run(
            evaluator_message=TemplatedText(content="write it"),
            critic_message=TemplatedText(content="review it"),
            response_format=_REFINE_SCHEMA,
        )
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
    asyncio.run(
        agent.run(
            evaluator_message=TemplatedText(content="write it"),
            critic_message=TemplatedText(content="review it"),
            response_format=_REFINE_SCHEMA,
        )
    )

    # The evaluator, critic, and structured final pass each compiled with their
    # role's per-run system prompt, built as a SystemMessage handed to create_agent.
    for system_prompt in recorder.system_prompts:
        assert isinstance(system_prompt, SystemMessage)
    assert [system_prompt.content for system_prompt in recorder.system_prompts] == [
        EVALUATOR_SYSTEM_MESSAGE,
        CRITIC_SYSTEM_MESSAGE,
        EVALUATOR_SYSTEM_MESSAGE,
    ]
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
        asyncio.run(
            agent.run(
                evaluator_message=TemplatedText(content="write it"),
                critic_message=TemplatedText(content="review it"),
                response_format=_REFINE_SCHEMA,
            )
        )


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
    events = _collect(
        agent,
        evaluator_message=TemplatedText(content="write it"),
        critic_message=TemplatedText(content="review it"),
        response_format=_REFINE_SCHEMA,
    )
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
        _collect(
            agent,
            evaluator_message=TemplatedText(content="write it"),
            critic_message=TemplatedText(content="review it"),
            response_format=_REFINE_SCHEMA,
        )


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
        _collect(
            agent,
            evaluator_message=TemplatedText(content="write it"),
            critic_message=TemplatedText(content="review it"),
            response_format=schema,
        )


def test_run_response_format_without_title_raises_loudly(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """A ``response_format`` dict lacking a top-level ``"title"`` is rejected loudly at
    the run seam before the loop runs."""
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(ValueError, match="top-level 'title'"):
        asyncio.run(
            agent.run(
                evaluator_message=TemplatedText(content="write it"),
                critic_message=TemplatedText(content="review it"),
                response_format={"type": "object"},
            )
        )


def test_astream_response_format_without_title_raises_loudly(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """The streaming face — the one the public run door drives — rejects an untitled
    ``response_format`` up front, exactly as the invoke face does."""
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(ValueError, match="top-level 'title'"):
        _collect(
            agent,
            evaluator_message=TemplatedText(content="write it"),
            critic_message=TemplatedText(content="review it"),
            response_format={"type": "object"},
        )


def test_astream_rejects_oneof_response_format_with_untitled_variant(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """A ``oneOf`` ``response_format`` whose variants lack titles (each binds its own
    structured-output name) is rejected up front, even though the container is
    titled."""
    schema = {"title": "Top", "oneOf": [{"title": "A", "type": "object"}, {"type": "object"}]}
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(ValueError, match="oneOf variants must each"):
        _collect(
            agent,
            evaluator_message=TemplatedText(content="write it"),
            critic_message=TemplatedText(content="review it"),
            response_format=schema,
        )
