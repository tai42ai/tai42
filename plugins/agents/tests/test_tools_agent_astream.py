"""The ``tools_agent`` ``astream`` face plus the config tests that parametrize both
faces: langgraph-config threading, event taxonomy, and structured finals.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel
from tai42_contract.agent import (
    MessageDelta,
    MessageFinal,
    ReasoningStep,
    RunUsage,
    StreamEvent,
    StructuredFinal,
    ToolCallStep,
    ToolResultStep,
)
from tai42_contract.agent.base import PresetSpec, SubAgentSpec
from tai42_contract.template import TemplatedText
from tests._tools_agent_support import (
    _STRUCTURED_SCHEMA,
    _capture_config,
    _collect,
    _get_agent,
    _script_astream,
    make_tool,
)


def test_astream_honors_recursion_limit_into_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """``astream`` overlays ``recursion_limit`` onto the run config the delegated
    event stream receives — in parity with ``run`` — rather than rejecting it. A
    falsy ``0`` is a real forwarded value: a truthiness overlay would drop it, so
    pinning ``0`` keeps the overlay load-bearing."""
    captured: dict[str, Any] = {}
    _script_astream(monkeypatch, [MessageFinal(text="ok")], captured)
    agent = _get_agent()
    _collect(agent, user_message=TemplatedText(content="hi"), recursion_limit=0)
    assert captured["config"]["recursion_limit"] == 0


@pytest.mark.parametrize("face", ["run", "astream"])
def test_face_honors_langgraph_config_thread_id(monkeypatch: pytest.MonkeyPatch, face: str) -> None:
    """Both faces honor a ``configurable.thread_id`` carried directly in the
    caller's ``langgraph_config``, so a caller pinning a conversation's checkpointed
    memory reaches the graph's checkpointer on either face."""
    config = _capture_config(
        monkeypatch,
        face,
        user_message=TemplatedText(content="hi"),
        langgraph_config={"configurable": {"thread_id": "T-42"}},
    )
    assert config["configurable"]["thread_id"] == "T-42"


@pytest.mark.parametrize("face", ["run", "astream"])
def test_face_honors_langgraph_config_checkpoint_id(monkeypatch: pytest.MonkeyPatch, face: str) -> None:
    """Both faces honor a ``configurable.checkpoint_id`` carried directly in the
    caller's ``langgraph_config`` — the checkpoint a run forks from."""
    config = _capture_config(
        monkeypatch,
        face,
        user_message=TemplatedText(content="hi"),
        langgraph_config={"configurable": {"thread_id": "T-42", "checkpoint_id": "CK-7"}},
    )
    assert config["configurable"]["checkpoint_id"] == "CK-7"


@pytest.mark.parametrize("face", ["run", "astream"])
def test_face_explicit_memory_keys_win_over_langgraph_config(monkeypatch: pytest.MonkeyPatch, face: str) -> None:
    """The explicit ``thread_id`` / ``resume_checkpoint_id`` parameters overlay the
    caller's ``langgraph_config``, so they win over the same keys carried in its
    ``configurable`` — and the same one wins on both faces."""
    config = _capture_config(
        monkeypatch,
        face,
        user_message=TemplatedText(content="hi"),
        thread_id="explicit",
        resume_checkpoint_id="explicit-cp",
        langgraph_config={"configurable": {"thread_id": "from-config", "checkpoint_id": "from-config-cp"}},
    )
    assert config["configurable"]["thread_id"] == "explicit"
    assert config["configurable"]["checkpoint_id"] == "explicit-cp"


@pytest.mark.parametrize("face", ["run", "astream"])
def test_face_preserves_the_rest_of_langgraph_config(monkeypatch: pytest.MonkeyPatch, face: str) -> None:
    """The overlay keeps every other key of the caller's config — the ``configurable``
    entries it does not own and the top-level ``RunnableConfig`` keys alike — and
    leaves the caller's own dict unmutated (a config handed to several runs is not
    scribbled on by one of them)."""
    base = {
        "configurable": {"tenant": "acme"},
        "recursion_limit": 12,
        "tags": ["t1"],
        "metadata": {"origin": "api"},
    }
    config = _capture_config(
        monkeypatch, face, user_message=TemplatedText(content="hi"), thread_id="T-1", langgraph_config=base
    )

    assert config["configurable"] == {"tenant": "acme", "thread_id": "T-1"}
    assert config["recursion_limit"] == 12
    assert config["tags"] == ["t1"]
    assert config["metadata"] == {"origin": "api"}
    assert base == {
        "configurable": {"tenant": "acme"},
        "recursion_limit": 12,
        "tags": ["t1"],
        "metadata": {"origin": "api"},
    }
    assert config["configurable"] is not base["configurable"]


@pytest.mark.parametrize("face", ["run", "astream"])
def test_face_recursion_limit_param_overlays_langgraph_config(monkeypatch: pytest.MonkeyPatch, face: str) -> None:
    """An explicit ``recursion_limit`` wins over one carried in the caller's config,
    identically on both faces."""
    config = _capture_config(
        monkeypatch,
        face,
        user_message=TemplatedText(content="hi"),
        recursion_limit=3,
        langgraph_config={"recursion_limit": 12},
    )
    assert config["recursion_limit"] == 3


def test_astream_renders_messages_by_template_id(monkeypatch: pytest.MonkeyPatch, resource_manager: Any) -> None:
    """``astream`` renders the system/user messages through the template manager —
    the same rendering ``run`` applies — so a caller supplying a stored template id
    (and its kwargs) reaches the streamed run with the rendered content."""
    resource_manager.templates["sys-tpl"] = "rendered-system"
    resource_manager.templates["user-tpl"] = "rendered-user"
    captured: dict[str, Any] = {}
    _script_astream(monkeypatch, [MessageFinal(text="ok")], captured)

    agent = _get_agent()
    _collect(
        agent,
        system_message=TemplatedText(id="sys-tpl", kwargs={"a": 1}),
        user_message=TemplatedText(id="user-tpl", kwargs={"b": 2}),
    )

    assert captured["system_message"] == "rendered-system"
    assert captured["user_message"] == ["rendered-user"]


def test_astream_unknown_template_id_raises_loudly(monkeypatch: pytest.MonkeyPatch, resource_manager: Any) -> None:
    """An unresolvable template id on the streaming face propagates rather than
    reaching the run as an empty message."""
    _script_astream(monkeypatch, [MessageFinal(text="ok")], {})
    agent = _get_agent()
    with pytest.raises(RuntimeError, match="unknown template id: nope"):
        _collect(agent, user_message=TemplatedText(id="nope"))


@pytest.mark.parametrize("face", ["run", "astream"])
def test_face_rejects_response_format_without_title(monkeypatch: pytest.MonkeyPatch, face: str) -> None:
    """A ``response_format`` dict with no top-level ``"title"`` (the structured-output
    name) is rejected loudly on both faces rather than handed to the graph."""
    with pytest.raises(ValueError, match="top-level 'title'"):
        _capture_config(monkeypatch, face, user_message=TemplatedText(content="hi"), response_format={"type": "object"})


@pytest.mark.parametrize("face", ["run", "astream"])
def test_face_rejects_oneof_response_format_with_untitled_variant(monkeypatch: pytest.MonkeyPatch, face: str) -> None:
    """A ``oneOf`` ``response_format`` binds one structured-output name per variant, so
    an untitled variant (which would take a random name) is rejected on both faces —
    even though the container carries a top-level title."""
    schema = {"title": "Top", "oneOf": [{"title": "A", "type": "object"}, {"type": "object"}]}
    with pytest.raises(ValueError, match="oneOf variants must each"):
        _capture_config(monkeypatch, face, user_message=TemplatedText(content="hi"), response_format=schema)


def test_astream_emits_the_full_event_taxonomy(monkeypatch: pytest.MonkeyPatch) -> None:
    """``astream`` surfaces the projected taxonomy — reasoning, a tool call/result
    pair, message deltas, the final message, and usage — and threads live tools
    and the resume config through to the projection."""
    script: list[StreamEvent] = [
        ReasoningStep(text="planning"),
        ToolCallStep(tool="search", args={"q": "x"}, call_id="c1"),
        ToolResultStep(tool="search", call_id="c1", result="hit"),
        MessageDelta(text="Hel"),
        MessageDelta(text="lo"),
        MessageFinal(text="Hello"),
        RunUsage(input_tokens=3, output_tokens=2, total_tokens=5, model="scripted"),
    ]
    captured: dict[str, Any] = {}
    _script_astream(monkeypatch, script, captured)

    live_tool = make_tool("search")
    agent = _get_agent()
    events = _collect(
        agent,
        tools=[live_tool],
        system_message=TemplatedText(content="sys"),
        user_message=TemplatedText(content="hi"),
        thread_id="th-1",
        resume_checkpoint_id="ck-9",
    )

    assert [type(event) for event in events] == [
        ReasoningStep,
        ToolCallStep,
        ToolResultStep,
        MessageDelta,
        MessageDelta,
        MessageFinal,
        RunUsage,
    ]
    call, result = events[1], events[2]
    assert isinstance(call, ToolCallStep)
    assert isinstance(result, ToolResultStep)
    assert call.call_id == result.call_id == "c1"
    assert events[5] == MessageFinal(text="Hello")

    # Live tools passed through — by SURFACE, not by object identity: resolution hands the
    # graph a delivery-scoped copy of every tool (its body runs with the park-completion
    # binding cleared), so the model-facing name/description/schema is what carries through.
    assert [(tool.name, tool.description, tool.args) for tool in captured["tools"]] == [
        (live_tool.name, live_tool.description, live_tool.args)
    ]
    # and the resume checkpoint threaded into config.
    assert captured["user_message"] == ["hi"]
    assert captured["config"] == {"configurable": {"thread_id": "th-1", "checkpoint_id": "ck-9"}}


def test_astream_resolves_baked_tool_names_and_presets(monkeypatch: pytest.MonkeyPatch, app_tools: Any) -> None:
    """``astream`` resolves ``tool_names`` + ``presets`` through the app facet — the
    SAME resolution ``run`` applies — so a baked spec's tools drive the streamed run
    instead of being silently dropped."""
    app_tools.client_tools["search"] = make_tool("search")
    app_tools.client_tools["example_tool"] = make_tool(
        "example_tool", {"example_config": {"type": "object"}, "q": {"type": "string"}}
    )
    captured: dict[str, Any] = {}
    _script_astream(monkeypatch, [MessageFinal(text="ok")], captured)
    agent = _get_agent()
    preset = PresetSpec(name="my_preset", base_tool="example_tool", fixed_kwargs={"example_config": {"nodes": []}})
    _collect(agent, tool_names=["search"], presets=[preset], user_message=TemplatedText(content="hi"))
    assert [tool.name for tool in captured["tools"]] == ["search", "my_preset"]


def test_astream_with_subagents_raises_loudly() -> None:
    """``astream`` never silently drops baked ``subagents`` — parity with ``run``."""
    agent = _get_agent()
    with pytest.raises(RuntimeError, match="subagents"):
        _collect(agent, user_message=TemplatedText(content="hi"), subagents=[SubAgentSpec(name="helper")])


def test_astream_with_strategy_raises_loudly() -> None:
    """``astream`` never silently drops a baked ``strategy`` — parity with ``run``."""
    agent = _get_agent()
    with pytest.raises(RuntimeError, match="strategy"):
        _collect(agent, user_message=TemplatedText(content="hi"), strategy="react")


def test_astream_with_response_format_emits_structured_final(monkeypatch: pytest.MonkeyPatch) -> None:
    """``astream`` with a ``response_format`` threads the schema to the delegated
    event stream (which forces the structured output) and surfaces the terminal
    :class:`StructuredFinal` the projection emits."""
    captured: dict[str, Any] = {}
    _script_astream(monkeypatch, [MessageFinal(text="text"), StructuredFinal(data={"value": 7})], captured)
    agent = _get_agent()
    events = _collect(agent, user_message=TemplatedText(content="hi"), response_format=_STRUCTURED_SCHEMA)

    finals = [event for event in events if isinstance(event, StructuredFinal)]
    assert len(finals) == 1
    assert finals[0].data == {"value": 7}
    assert captured["response_format"] == _STRUCTURED_SCHEMA


def test_astream_with_response_format_but_no_structured_raises_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A requested ``response_format`` the stream produced no ``StructuredFinal``
    for raises loudly after the stream drains — in parity with ``run`` — rather
    than silently omitting the frame."""
    captured: dict[str, Any] = {}
    _script_astream(monkeypatch, [MessageFinal(text="text only")], captured)
    agent = _get_agent()
    with pytest.raises(RuntimeError, match="no structured output"):
        _collect(agent, user_message=TemplatedText(content="hi"), response_format=_STRUCTURED_SCHEMA)


def test_astream_without_resume_omits_checkpoint_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _script_astream(monkeypatch, [MessageFinal(text="ok")], captured)
    agent = _get_agent()
    _collect(agent, user_message=TemplatedText(content="hi"))
    assert captured["config"] == {"configurable": {}}


def test_astream_keyless_run_does_not_pin_thread_id_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyless ``astream`` (no ``thread_id``) leaves ``thread_id`` out of the
    configurable entirely, rather than pinning it to ``None`` — so the run config
    helper mints a fresh isolated thread instead of colliding every keyless run
    on one shared checkpoint thread."""
    captured: dict[str, Any] = {}
    _script_astream(monkeypatch, [MessageFinal(text="ok")], captured)
    agent = _get_agent()
    _collect(agent, user_message=TemplatedText(content="hi"))
    assert "thread_id" not in captured["config"]["configurable"]


def test_astream_emits_structured_final(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the projected run carries a structured response, ``astream`` surfaces a
    terminal :class:`StructuredFinal`."""

    class Answer(BaseModel):
        value: int

    payload = Answer(value=7)
    script: list[StreamEvent] = [
        MessageDelta(text="done"),
        MessageFinal(text="done"),
        StructuredFinal(data=payload),
    ]
    _script_astream(monkeypatch, script, {})
    agent = _get_agent()
    events = _collect(agent, user_message=TemplatedText(content="hi"))

    finals = [event for event in events if isinstance(event, StructuredFinal)]
    assert len(finals) == 1
    assert finals[0].data == payload
    assert finals[0].final is True


def test_drain_structured_stream_returns_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Draining a stream that ends in a :class:`StructuredFinal` with a requested
    ``response_format`` returns the structured payload (the contract terminal rule)."""

    class Answer(BaseModel):
        value: int

    payload = Answer(value=7)
    _script_astream(monkeypatch, [MessageFinal(text="text"), StructuredFinal(data=payload)], {})
    agent = _get_agent()

    async def go() -> Any:
        return await agent._drain(agent.astream(user_message=TemplatedText(content="hi")), response_format=Answer)

    assert asyncio.run(go()) == payload


def test_astream_never_emits_an_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """This agent has no interrupt source: a normal run's stream carries no
    ``InterruptFinal`` (its absence is by design, not a partial run)."""
    from tai42_contract.agent import InterruptFinal

    _script_astream(monkeypatch, [MessageDelta(text="hi"), MessageFinal(text="hi")], {})
    agent = _get_agent()
    events = _collect(agent, user_message=TemplatedText(content="hi"))
    assert not any(isinstance(event, InterruptFinal) for event in events)


def test_astream_threads_content_kwargs_to_the_event_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """``astream`` forwards both content-block kwargs to the event seam, in parity
    with :meth:`run`."""
    captured: dict[str, Any] = {}
    _script_astream(monkeypatch, [MessageFinal(text="hi")], captured)
    agent = _get_agent()
    cache = {"cache_control": {"type": "ephemeral"}}
    _collect(agent, user_message=TemplatedText(content="hi"), system_content_kwargs=cache, user_content_kwargs=cache)

    assert captured["system_content_kwargs"] == cache
    assert captured["user_content_kwargs"] == cache
