"""``refine_agent`` contract surface: reject-unhonored guards, the required
evaluator-message slot, and the input model.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk
from pydantic import ValidationError
from tai42_contract.agent import (
    MessageFinal,
)
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tests._refine_agent_support import (
    AGENT_NAME,
    FakeAgent,
    _a_tool,
    _collect,
    _patch_loop,
)

from tai42_agents.refine_agent.agent import RefineAgentInput
from tai42_agents.refine_agent.prompt import (
    CRITIC_APPROVAL_MESSAGE,
)


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
    asyncio.run(
        agent.run(
            evaluator_message=TemplatedText(content="write it"),
            critic_message=TemplatedText(content="review it"),
            tool_names=["t1"],
        )
    )

    # Both compiled with the live tool — by SURFACE, not object identity (each pass gets a
    # delivery-scoped copy), so this is parity with the astream face, not a silent drop.
    expected = [(tool.name, tool.description, tool.args)]
    assert [[(t.name, t.description, t.args) for t in call] for call in recorder.tools_per_call] == [
        expected,
        expected,
    ]


# The contract keys the role-named loop cannot honor, each with a set value.
_UNHONORED_CASES = [("user_message", "usr"), ("user_message", ""), ("thread_id", "t")]

# Keywords the loop does not declare, refused by presence.
_UNDECLARED_CASES = [
    ("tools", [object()]),
    ("presets", [object()]),
    ("system_message", "sys"),
    ("strategy", ""),
    ("resume", False),
    ("recursion_limit", 0),
    ("llm_provider", "openai"),
    ("system_content_kwargs", {"cache_control": {"type": "ephemeral"}}),
]


def _messages() -> dict[str, Any]:
    return {
        "evaluator_message": TemplatedText(content="write it"),
        "critic_message": TemplatedText(content="review it"),
    }


@pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
def test_both_faces_refuse_a_contract_key_they_cannot_honor(
    param: str, value: Any, app_tools: Any, resource_manager: Any
) -> None:
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    match = rf"refine_agent\.astream does not support .*\b{param}\b"
    with pytest.raises(RuntimeError, match=match):
        _collect(agent, **_messages(), **{param: value})
    with pytest.raises(RuntimeError, match=match):
        asyncio.run(agent.run(**_messages(), **{param: value}))


@pytest.mark.parametrize(("param", "value"), _UNDECLARED_CASES)
def test_both_faces_refuse_an_undeclared_keyword(param: str, value: Any, app_tools: Any, resource_manager: Any) -> None:
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    match = rf"refine_agent\.astream got unknown run keyword argument\(s\): {param}"
    with pytest.raises(TypeError, match=match):
        _collect(agent, **_messages(), **{param: value})
    with pytest.raises(TypeError, match=match):
        asyncio.run(agent.run(**_messages(), **{param: value}))


def test_the_not_requested_sentinels_pass_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="ok"), {}))])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(agent, **_messages(), thread_id=None, user_message=None, response_format=None)
    assert any(isinstance(e, MessageFinal) for e in events)


def test_run_raises_when_required_evaluator_message_slot_is_unset(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """The evaluator message renders with ``allow_empty=False``: an unset slot
    (``None``) raises loudly rather than silently running the loop on an empty prompt.
    The evaluator/critic seams are faked so that WITHOUT render.py's ``allow_empty``
    passthrough the loop would instead run to approval — a dropped passthrough turns
    this red rather than letting the empty prompt slip through."""
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="ok"), {}))])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(ValueError, match="required message was not provided"):
        asyncio.run(agent.run(critic_message=TemplatedText(content="review it")))


def test_tool_input_rejects_unknown_key() -> None:
    """A typo'd key is rejected loudly at validation rather than silently ignored."""
    with pytest.raises(ValidationError, match="max_iteration"):
        RefineAgentInput.model_validate({"evaluator_message": {"content": "hi"}, "max_iteration": 5})


def test_empty_content_kwargs_normalize_to_none() -> None:
    """An empty ``user_content_kwargs`` dict from the JSON door reads as absent — the
    builders treat {} as no mark, so the field normalizes to None rather than a
    set-but-empty value the unhonored-reject face would misread."""
    validated = RefineAgentInput.model_validate({"evaluator_message": {"content": "hi"}, "user_content_kwargs": {}})
    assert validated.user_content_kwargs is None
    # A non-empty mark is a real value and rides through unchanged.
    marked = RefineAgentInput.model_validate(
        {"evaluator_message": {"content": "hi"}, "user_content_kwargs": {"cache_control": {"type": "ephemeral"}}}
    )
    assert marked.user_content_kwargs == {"cache_control": {"type": "ephemeral"}}
