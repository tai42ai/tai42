"""The ``tools_agent`` ``run`` face: tool resolution, message rendering,
response-format, recursion limit, resume, and unsupported-arg guards.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.tools import ToolException
from tai42_contract.agent.base import PresetSpec, SubAgentSpec
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tests._tools_agent_support import (
    _STRUCTURED_SCHEMA,
    _get_agent,
    make_tool,
)

from tai42_agents import tools_agent as tools_agent_module
from tai42_agents._internal.resolve_tools import resolve_tools
from tai42_agents._internal.usage import AgentInvokeResult, CallUsage


def test_run_resolves_tools_and_returns_final_text(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """``run`` resolves ``tool_names`` + ``presets`` through the app facet, renders
    the messages, and returns the invoked agent's user-facing output. The invoke
    seam is scripted; its captured arguments prove the wiring."""
    app_tools.client_tools["search"] = make_tool("search")
    app_tools.client_tools["example_tool"] = make_tool(
        "example_tool", {"example_config": {"type": "object"}, "q": {"type": "string"}}
    )
    routed: list[tuple[str, dict[str, Any]]] = []
    app_tools.tool_runners["example_tool"] = lambda **kwargs: routed.append(("example_tool", kwargs)) or {"ok": True}

    captured: dict[str, Any] = {}

    async def fake_invoke(**kwargs: Any) -> AgentInvokeResult:
        captured.update(kwargs)
        return AgentInvokeResult(output="the answer", usage=CallUsage(0, 0, None))

    monkeypatch.setattr(tools_agent_module, "ainvoke_tools_agent", fake_invoke)

    preset = PresetSpec(name="my_preset", base_tool="example_tool", fixed_kwargs={"example_config": {"nodes": []}})
    agent = _get_agent()
    result = asyncio.run(
        agent.run(
            tool_names=["search"],
            presets=[preset],
            system_message=TemplatedText(content="sys"),
            user_message=TemplatedText(content="hi"),
        )
    )

    assert result == "the answer"
    # live tools = resolved client tool + the preset, deduped and in order.
    resolved = captured["tools"]
    assert [tool.name for tool in resolved] == ["search", "my_preset"]
    # rendered messages threaded through as literal content.
    assert captured["system_message"] == "sys"
    assert captured["user_message"] == ["hi"]

    # the preset tool routes its call through the app's run_tool with fixed kwargs
    # merged under the runtime kwargs.
    preset_tool = next(tool for tool in resolved if tool.name == "my_preset")
    asyncio.run(preset_tool.arun({"q": "search-term"}))
    key, arguments = routed[-1]
    assert key == "example_tool"
    assert arguments == {"example_config": {"nodes": []}, "q": "search-term"}


def test_preset_tool_invocation_raises_on_unknown_base_tool(app_tools: Any) -> None:
    """A preset whose base tool is a registered client tool (so binding succeeds)
    but is absent from ``tool_runners`` surfaces the recording facet's
    unknown-base-tool failure when the bound tool is invoked: binding does not
    pre-check the runner map, so the failure lands at call time and comes out of the
    bound tool rather than being swallowed into a result.

    It comes out as the adapter's typed ``ToolException``: ``AppTools.run_tool`` is one
    opaque call declaring no exception type, so this adapter cannot tell a base tool
    that VANISHED from one that FAILED — the dispatch is the tool's body here, and
    everything it raises is the tool's failure. The original error is not lost: it is
    the exception's ``__cause__`` and is logged with its traceback."""
    app_tools.client_tools["example_tool"] = make_tool("example_tool", {"q": {"type": "string"}})
    preset = PresetSpec(name="my_preset", base_tool="example_tool")
    (preset_tool,) = asyncio.run(resolve_tools(tai42_app.tools, [], [], [preset]))
    with pytest.raises(ToolException, match=r"Error calling tool 'my_preset': unknown base tool") as caught:
        asyncio.run(preset_tool.arun({"q": "x"}))
    assert isinstance(caught.value.__cause__, RuntimeError)


def test_run_unset_system_message_renders_empty(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """An unset system slot (``system_message`` left ``None``) renders to the empty
    string rather than raising, and a system slot supplied as a stored ``id`` renders
    through the template manager's map.

    Two runs pin both halves. First, a fully unset system slot: ``run`` renders and
    returns with an empty system message. Second, a system supplied as a
    :class:`~tai42_contract.template.TemplatedText` naming a stored ``id`` renders to
    the stored text."""
    resource_manager.templates["sys_tpl"] = "SYSTEM"
    captured: dict[str, Any] = {}

    async def fake_invoke(**kwargs: Any) -> AgentInvokeResult:
        captured.update(kwargs)
        return AgentInvokeResult(output="the answer", usage=CallUsage(0, 0, None))

    monkeypatch.setattr(tools_agent_module, "ainvoke_tools_agent", fake_invoke)

    agent = _get_agent()
    result = asyncio.run(agent.run(user_message=TemplatedText(content="hi")))

    assert result == "the answer"
    assert captured["system_message"] == ""
    assert captured["user_message"] == ["hi"]

    result = asyncio.run(
        agent.run(system_message=TemplatedText(id="sys_tpl"), user_message=TemplatedText(content="hi"))
    )

    assert result == "the answer"
    assert captured["system_message"] == "SYSTEM"


def test_run_raises_when_required_user_message_renders_empty(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """``run`` requires exactly one of ``user_message`` or ``resume``; a user message
    authored as explicitly empty inline content (``{"content": ""}``) satisfies the
    ``is None`` guard yet renders to nothing, so the run face refuses it loudly rather
    than dispatching on a blank prompt. The invoke seam is faked to a completing double
    so a dropped ``allow_empty`` refusal would return "the answer" and turn this red."""

    async def fake_invoke(**kwargs: Any) -> AgentInvokeResult:
        return AgentInvokeResult(output="the answer", usage=CallUsage(0, 0, None))

    monkeypatch.setattr(tools_agent_module, "ainvoke_tools_agent", fake_invoke)

    agent = _get_agent()
    with pytest.raises(ValueError, match="user_message: required message was not provided"):
        asyncio.run(agent.run(user_message=TemplatedText(content="")))


def test_run_with_response_format_returns_the_structured_object(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """``run`` with a ``response_format`` forces the run's structured output and
    returns the structured object the invoke seam produced — not the text — with the
    schema threaded down to the invoke seam."""
    payload = {"value": 7}
    captured: dict[str, Any] = {}

    async def fake_invoke(**kwargs: Any) -> AgentInvokeResult:
        captured.update(kwargs)
        return AgentInvokeResult(output="ignored text", usage=CallUsage(0, 0, None), structured=payload)

    monkeypatch.setattr(tools_agent_module, "ainvoke_tools_agent", fake_invoke)

    agent = _get_agent()
    result = asyncio.run(agent.run(user_message=TemplatedText(content="hi"), response_format=_STRUCTURED_SCHEMA))
    assert result == payload
    assert captured["response_format"] == _STRUCTURED_SCHEMA


def test_run_with_response_format_but_no_structured_raises_loudly(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """A requested ``response_format`` that the run produced none for raises loudly
    rather than silently falling back to text. The raise happens inside
    ``ainvoke_tools_agent`` (its structured extraction), and ``run`` propagates it
    unchanged."""

    async def fake_invoke(**kwargs: Any) -> AgentInvokeResult:
        raise RuntimeError(
            "response_format was requested but the agent produced no structured_response (missing/None in final state)."
        )

    monkeypatch.setattr(tools_agent_module, "ainvoke_tools_agent", fake_invoke)

    agent = _get_agent()
    with pytest.raises(RuntimeError, match="no structured_response"):
        asyncio.run(agent.run(user_message=TemplatedText(content="hi"), response_format=_STRUCTURED_SCHEMA))


def test_run_response_format_without_title_raises_loudly(app_tools: Any, resource_manager: Any) -> None:
    """A ``response_format`` JSON-Schema dict lacking a top-level ``"title"`` is
    rejected loudly at the run seam (the title names the structured output)."""
    agent = _get_agent()
    with pytest.raises(ValueError, match="top-level 'title'"):
        asyncio.run(agent.run(user_message=TemplatedText(content="hi"), response_format={"type": "object"}))


def test_run_with_subagents_raises_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` never silently drops baked ``subagents``: sub-agent delegation is
    langchain_deep_agent's domain, so a non-empty value fails loud rather than being ignored."""
    invoked = False

    async def fake_invoke(**kwargs: Any) -> AgentInvokeResult:
        nonlocal invoked
        invoked = True
        return AgentInvokeResult(output="x", usage=CallUsage(0, 0, None))

    monkeypatch.setattr(tools_agent_module, "ainvoke_tools_agent", fake_invoke)
    agent = _get_agent()
    with pytest.raises(RuntimeError, match="subagents"):
        asyncio.run(agent.run(user_message=TemplatedText(content="hi"), subagents=[SubAgentSpec(name="helper")]))
    assert invoked is False


def test_run_with_strategy_raises_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` never silently drops a baked ``strategy``: it applies none, so a
    non-null value fails loud rather than being ignored."""
    invoked = False

    async def fake_invoke(**kwargs: Any) -> AgentInvokeResult:
        nonlocal invoked
        invoked = True
        return AgentInvokeResult(output="x", usage=CallUsage(0, 0, None))

    monkeypatch.setattr(tools_agent_module, "ainvoke_tools_agent", fake_invoke)
    agent = _get_agent()
    with pytest.raises(RuntimeError, match="strategy"):
        asyncio.run(agent.run(user_message=TemplatedText(content="hi"), strategy="react"))
    assert invoked is False


def test_run_renders_message_by_template_id(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """A system message supplied as a stored ``id`` renders through the template
    manager's stored map."""
    resource_manager.templates["greeting"] = "rendered-system"
    captured: dict[str, Any] = {}

    async def fake_invoke(**kwargs: Any) -> AgentInvokeResult:
        captured.update(kwargs)
        return AgentInvokeResult(output="x", usage=CallUsage(0, 0, None))

    monkeypatch.setattr(tools_agent_module, "ainvoke_tools_agent", fake_invoke)

    agent = _get_agent()
    asyncio.run(agent.run(system_message=TemplatedText(id="greeting"), user_message=TemplatedText(content="hi")))
    assert captured["system_message"] == "rendered-system"


def test_run_honors_live_tools_and_thread_config(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """``run`` honors the same seams ``astream`` does: live ``tools`` resolve into
    the invoked tool set, and ``thread_id`` / ``resume_checkpoint_id`` map into the
    run config's ``configurable`` — none of them is silently dropped."""
    app_tools.client_tools["search"] = make_tool("search")
    captured: dict[str, Any] = {}

    async def fake_invoke(**kwargs: Any) -> AgentInvokeResult:
        captured.update(kwargs)
        return AgentInvokeResult(output="ok", usage=CallUsage(0, 0, None))

    monkeypatch.setattr(tools_agent_module, "ainvoke_tools_agent", fake_invoke)

    live_tool = make_tool("live")
    agent = _get_agent()
    asyncio.run(
        agent.run(
            tools=[live_tool],
            tool_names=["search"],
            user_message=TemplatedText(content="hi"),
            thread_id="th-1",
            resume_checkpoint_id="ck-9",
        )
    )
    # live tool first, then the resolved client tool — the same order ``astream`` uses.
    assert [tool.name for tool in captured["tools"]] == ["live", "search"]
    assert captured["config"] == {"configurable": {"thread_id": "th-1", "checkpoint_id": "ck-9"}}


def test_run_keyless_does_not_pin_thread_id(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """A keyless ``run`` (no ``thread_id``) leaves the config's ``configurable``
    empty rather than pinning ``thread_id`` to ``None`` — so the run config helper
    mints a fresh isolated thread instead of colliding every keyless run."""
    captured: dict[str, Any] = {}

    async def fake_invoke(**kwargs: Any) -> AgentInvokeResult:
        captured.update(kwargs)
        return AgentInvokeResult(output="ok", usage=CallUsage(0, 0, None))

    monkeypatch.setattr(tools_agent_module, "ainvoke_tools_agent", fake_invoke)
    agent = _get_agent()
    asyncio.run(agent.run(user_message=TemplatedText(content="hi")))
    assert captured["config"] == {"configurable": {}}


def test_run_honors_a_caller_driven_resume(monkeypatch: pytest.MonkeyPatch, app_tools: Any) -> None:
    """A ``run`` given ``resume`` (no ``user_message``) drives the caller's
    ``Command(resume=...)`` map through to the runtime and returns its output — the
    resumable path an async ``ask_user`` park opens."""
    captured: dict[str, Any] = {}

    async def fake_invoke(**kwargs: Any) -> AgentInvokeResult:
        captured.update(kwargs)
        return AgentInvokeResult(output="resumed answer", usage=CallUsage(0, 0, None))

    monkeypatch.setattr(tools_agent_module, "ainvoke_tools_agent", fake_invoke)
    agent = _get_agent()
    resume_map = {"int1": {"i1": "the answer"}}
    result = asyncio.run(agent.run(resume=resume_map, thread_id="t-resume"))
    assert result == "resumed answer"
    assert captured["resume"] == resume_map


def test_run_honors_recursion_limit_into_config(monkeypatch: pytest.MonkeyPatch, resource_manager: Any) -> None:
    """``recursion_limit`` is a standard ``RunnableConfig`` key the compiled graph
    reads, so ``run`` overlays it onto the run config rather than rejecting it — the
    captured config the invoke seam receives carries the value (a falsy ``0`` too)."""
    captured: dict[str, Any] = {}

    async def fake_invoke(**kwargs: Any) -> AgentInvokeResult:
        captured.update(kwargs)
        return AgentInvokeResult(output="ok", usage=CallUsage(0, 0, None))

    monkeypatch.setattr(tools_agent_module, "ainvoke_tools_agent", fake_invoke)
    agent = _get_agent()
    asyncio.run(agent.run(user_message=TemplatedText(content="hi"), recursion_limit=0))
    assert captured["config"]["recursion_limit"] == 0


def test_run_threads_content_kwargs_to_the_invoke_seam(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """``run`` forwards ``system_content_kwargs`` / ``user_content_kwargs`` to the
    invoke seam, where the kit marks the system prompt and last user message."""
    captured: dict[str, Any] = {}

    async def fake_invoke(**kwargs: Any) -> AgentInvokeResult:
        captured.update(kwargs)
        return AgentInvokeResult(output="ok", usage=CallUsage(0, 0, None))

    monkeypatch.setattr(tools_agent_module, "ainvoke_tools_agent", fake_invoke)
    agent = _get_agent()
    cache = {"cache_control": {"type": "ephemeral"}}
    asyncio.run(
        agent.run(user_message=TemplatedText(content="hi"), system_content_kwargs=cache, user_content_kwargs=cache)
    )

    assert captured["system_content_kwargs"] == cache
    assert captured["user_content_kwargs"] == cache
