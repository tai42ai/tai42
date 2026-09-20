"""``refine_agent`` tool resolution and the iterate/approve loop, including per-turn
cache marking on a reused thread.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from tai42_contract.agent import (
    Agent,
    MessageFinal,
)
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tests._delivery_scope import assert_delivery_scoped, probe_tool
from tests._refine_agent_support import (
    AGENT_NAME,
    FakeAgent,
    _a_tool,
    _collect,
    _final_pass_script,
    _mark_count,
    _patch_loop,
    _patch_loop_real,
    _RecordingChatModel,
)

from tai42_agents.refine_agent.agent import RefineAgent
from tai42_agents.refine_agent.prompt import (
    CRITIC_APPROVAL_MESSAGE,
)


def test_decorator_registers_a_live_instance() -> None:
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    assert isinstance(agent, RefineAgent)
    assert isinstance(agent, Agent)
    assert agent.tool_name == AGENT_NAME


def test_tools_are_resolved_by_name_and_passed_to_both_agents(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    tool = _a_tool()
    app_tools.client_tools["t1"] = tool
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="ok"), {}))])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    recorder = _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    _collect(
        agent,
        evaluator_message=TemplatedText(content="write it"),
        critic_message=TemplatedText(content="review it"),
        tool_names=["t1"],
    )

    # Both agents got the tool — by SURFACE, not object identity: resolution hands each pass a
    # delivery-scoped copy (its body runs with the park-completion binding cleared). Name alone
    # would not catch a copy that lost its schema, so the whole model-facing triple is pinned.
    expected = [(tool.name, tool.description, tool.args)]
    assert [[(t.name, t.description, t.args) for t in call] for call in recorder.tools_per_call] == [
        expected,
        expected,
    ]


def test_dispatched_tools_are_delivery_scoped(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    # A tool this loop dispatches is a STEP of the refine turn, never a second answerer of it:
    # a parking driver reached through it must not read the completion binding that addresses
    # the agent's own deferred answer off the contextvar.
    probe, seen = probe_tool("t1")
    app_tools.client_tools["t1"] = probe
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="ok"), {}))])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    recorder = _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    _collect(
        agent,
        evaluator_message=TemplatedText(content="write it"),
        critic_message=TemplatedText(content="review it"),
        tool_names=["t1"],
    )

    assert_delivery_scoped(recorder.tools_per_call[0][0], seen)


def test_unknown_tool_name_raises(monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any) -> None:
    evaluator = FakeAgent(invoke_contents=["draft"])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match="unknown client tools"):
        _collect(
            agent,
            evaluator_message=TemplatedText(content="write it"),
            critic_message=TemplatedText(content="review it"),
            tool_names=["missing"],
        )


def test_approval_on_first_iteration_streams_the_final_pass(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=_final_pass_script())
    critic = FakeAgent(invoke_contents=[f"looks great {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(
        agent, evaluator_message=TemplatedText(content="write it"), critic_message=TemplatedText(content="review it")
    )

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
        evaluator_message=TemplatedText(content="write it"),
        critic_message=TemplatedText(content="review it"),
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
    events = _collect(
        agent,
        evaluator_message=TemplatedText(content="write it"),
        critic_message=TemplatedText(content="review it"),
        max_iterations=5,
    )

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
        _collect(
            agent,
            evaluator_message=TemplatedText(content="write it"),
            critic_message=TemplatedText(content="review it"),
            max_iterations=2,
        )

    assert len(evaluator.ainvoke_inputs) == 2  # both budgeted rounds attempted
    assert len(critic.ainvoke_inputs) == 2


def test_empty_critic_feedback_raises(monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any) -> None:
    evaluator = FakeAgent(invoke_contents=["draft"])
    critic = FakeAgent(invoke_contents=[""])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match="No critic feedback found"):
        _collect(
            agent,
            evaluator_message=TemplatedText(content="write it"),
            critic_message=TemplatedText(content="review it"),
        )


def test_exact_token_substring_is_recognized_as_approval(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """The exact token embedded in surrounding text counts as approval, even at
    the last budgeted iteration — the check must actually detect it."""
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="done"), {}))])
    critic = FakeAgent(invoke_contents=[f"All good here: {CRITIC_APPROVAL_MESSAGE} — ship it"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(
        agent,
        evaluator_message=TemplatedText(content="write it"),
        critic_message=TemplatedText(content="review it"),
        max_iterations=1,
    )

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
        _collect(
            agent,
            evaluator_message=TemplatedText(content="write it"),
            critic_message=TemplatedText(content="review it"),
            max_iterations=1,
        )


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
            evaluator_message=TemplatedText(content=message),
            critic_message=TemplatedText(content="review it"),
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
