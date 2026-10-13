"""An undeclared park is a registration fault the agent run ends on, never a tool error the model retries.

The refusal reaches the agents raw: the platform's dispatch seam raises ``UndeclaredPauseError`` for a tool
that returns a park signal without declaring ``tai42/pauses``. The preset adapter, a client-tool handle and the
``claude_code`` proxied call each let it end the run, with no error ``ToolMessage`` fed back to the model.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool, ToolException
from langgraph.checkpoint.memory import InMemorySaver
from tai42_contract.agent.base import PresetSpec
from tai42_contract.interactions import SuspendedInteraction
from tai42_contract.tools import UndeclaredPauseError

from tai42_agents._internal.lc_tools import preset_structured_tool, to_structured_tool
from tai42_agents.claude_code.protocol import ToolCallFrame
from tai42_agents.claude_code.tool_call import run_proxied_tool_call

from ._client_tool import FakeClientTool, client_tool
from ._graph_support import invoke_tools_agent
from .test_tool_error_recovery import ScriptedChatModel, _config, _seams, _tool_call


def _checkpointed_messages(saver: InMemorySaver, thread_id: str) -> list[Any]:
    """The thread's messages as the run left them (read raw, so no next-turn repair runs)."""
    saved = saver.get_tuple({"configurable": {"thread_id": thread_id}})
    return [] if saved is None else list(saved.checkpoint["channel_values"].get("messages", []))


def _refusing_runner(calls: list[str], name: str):
    """A base-tool runner standing in for the dispatch seam's refusal of an undeclared park."""

    def _run(**_kwargs: Any) -> Any:
        calls.append(name)
        raise UndeclaredPauseError(name)

    return _run


def _refusing_handle(calls: list[str], name: str) -> FakeClientTool:
    """A client-tool handle standing in for the platform handle's refusal of an undeclared park."""

    def _body() -> Any:
        calls.append(name)
        raise UndeclaredPauseError(name)

    return client_tool(name, _body, description="an undeclared parking tool")


def _preset(base: str) -> PresetSpec:
    return PresetSpec(name="my_preset", description="run a preset", base_tool=base, fixed_kwargs={})


def _preset_over(app_tools: Any, base: str) -> StructuredTool:
    app_tools.client_tools[base] = client_tool(base)
    return asyncio.run(preset_structured_tool(app_tools, _preset(base)))


@pytest.mark.parametrize("signal", ["suspended", "buffered"])
def test_both_builders_raise_an_undeclared_park_raw(app_tools: Any, signal: str) -> None:
    # The platform refuses an undeclared park signal (a SuspendedInteraction or a ResumeBuffered)
    # with ``UndeclaredPauseError``; both builders let it leave the call raw, never a ToolException.
    calls: list[str] = []
    name = f"undeclared_{signal}"
    app_tools.tool_runners[name] = _refusing_runner(calls, name)
    with pytest.raises(UndeclaredPauseError) as caught:
        asyncio.run(_preset_over(app_tools, name).ainvoke({}))
    assert not isinstance(caught.value, ToolException)
    with pytest.raises(UndeclaredPauseError) as caught:
        asyncio.run(to_structured_tool(_refusing_handle(calls, name)).ainvoke({}))
    assert not isinstance(caught.value, ToolException)
    assert calls == [name, name]


def test_both_builders_park_over_a_declared_tool(app_tools: Any) -> None:
    from tai42_contract.interactions import (
        read_suspended_interaction_marker,
        reset_resume_continuation_tool,
        set_resume_continuation_tool,
    )

    def declared(**_kw: Any) -> SuspendedInteraction:
        return SuspendedInteraction(interaction_id="i-declared", resume_owner="agent_resume")

    app_tools.tool_runners["declared"] = declared
    token = set_resume_continuation_tool("agent_resume")
    try:
        through_preset = asyncio.run(_preset_over(app_tools, "declared").ainvoke({}))
        through_handle = asyncio.run(to_structured_tool(client_tool("declared", declared)).ainvoke({}))
    finally:
        reset_resume_continuation_tool(token)
    assert read_suspended_interaction_marker(through_preset) is not None
    assert read_suspended_interaction_marker(through_handle) is not None


def test_a_tools_agent_run_calling_the_preset_ends_failed_with_no_tool_message(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    model = ScriptedChatModel([_tool_call("my_preset", "call_1"), AIMessage(content="unreached")])
    _seams(monkeypatch, model, saver)
    calls: list[str] = []
    app_tools.tool_runners["undeclared"] = _refusing_runner(calls, "undeclared")
    app_tools.client_tools["undeclared"] = client_tool("undeclared")

    with pytest.raises(UndeclaredPauseError):
        asyncio.run(
            invoke_tools_agent(
                "sys", ["do it"], [], config=_config("t-undeclared-preset"), presets=[_preset("undeclared")]
            )
        )

    messages = _checkpointed_messages(saver, "t-undeclared-preset")
    assert [m for m in messages if isinstance(m, ToolMessage)] == []
    assert calls == ["undeclared"]


def test_a_tools_agent_run_calling_an_undeclared_handle_ends_failed_with_no_tool_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver = InMemorySaver()
    model = ScriptedChatModel([_tool_call("undeclared", "call_1"), AIMessage(content="unreached")])
    _seams(monkeypatch, model, saver)
    calls: list[str] = []

    with pytest.raises(UndeclaredPauseError):
        asyncio.run(
            invoke_tools_agent(
                "sys", ["do it"], [_refusing_handle(calls, "undeclared")], config=_config("t-undeclared-handle")
            )
        )

    messages = _checkpointed_messages(saver, "t-undeclared-handle")
    assert [m for m in messages if isinstance(m, ToolMessage)] == []
    assert calls == ["undeclared"]


def test_a_tools_agent_run_calling_a_handle_over_a_nested_refusal_ends_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client-tool handle over a re-dispatching tool (a ``chain``-shaped body calling the undeclared tool) raises
    the nested refusal raw; the run fails with it, the model reads no tool error, the inner body fired once."""
    saver = InMemorySaver()
    model = ScriptedChatModel([_tool_call("relay", "call_1"), AIMessage(content="unreached")])
    _seams(monkeypatch, model, saver)
    fired: list[str] = []

    async def _relay(**_kwargs: Any) -> Any:
        fired.append("undeclared")
        raise UndeclaredPauseError("undeclared")

    relay = client_tool("relay", _relay, description="re-dispatches")

    with pytest.raises(UndeclaredPauseError):
        asyncio.run(invoke_tools_agent("sys", ["do it"], [relay], config=_config("t-undeclared-nested")))

    messages = _checkpointed_messages(saver, "t-undeclared-nested")
    assert [m for m in messages if isinstance(m, ToolMessage)] == []
    assert fired == ["undeclared"]


class _RecordingHandle:
    """A stand-in runner handle recording every frame written back to it."""

    def __init__(self) -> None:
        self.written: list[bytes] = []

    async def write_stdin(self, data: bytes) -> None:
        self.written.append(data)


def test_a_proxied_call_of_an_undeclared_park_raises_and_writes_no_result(app_tools: Any) -> None:
    calls: list[str] = []
    app_tools.tool_runners["undeclared"] = _refusing_runner(calls, "undeclared")
    handle = _RecordingHandle()
    frame = ToolCallFrame(call_id="c1", tool_name="undeclared", arguments={})
    with pytest.raises(UndeclaredPauseError):
        asyncio.run(run_proxied_tool_call(frame, handle=handle, allowlist={"undeclared"}, thread_id="t"))
    assert handle.written == []
    assert calls == ["undeclared"]


def test_a_proxied_call_of_a_declared_park_parks_as_today(app_tools: Any) -> None:
    from tai42_contract.interactions import reset_resume_continuation_tool, set_resume_continuation_tool

    app_tools.tool_runners["declared"] = lambda **_kw: SuspendedInteraction(
        interaction_id="i-proxied", resume_owner="agent_resume"
    )
    handle = _RecordingHandle()
    frame = ToolCallFrame(call_id="c1", tool_name="declared", arguments={})
    token = set_resume_continuation_tool("agent_resume")
    try:
        parked = asyncio.run(run_proxied_tool_call(frame, handle=handle, allowlist={"declared"}, thread_id="t"))
    finally:
        reset_resume_continuation_tool(token)
    assert isinstance(parked, SuspendedInteraction)
