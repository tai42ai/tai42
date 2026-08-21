"""Tests for the tools-agent factory + its invoke / raw-stream faces.

Every provider seam ``_build_agent_and_input`` reaches (LLM, checkpointer,
middleware, ``create_agent``) is monkeypatched to a scripted double, so the
factory's wiring — default-provider fallback, config init, message build, and
the ``ainvoke`` / ``astream`` faces — is exercised with no LLM, checkpointer, or
network.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import SystemMessage
from langchain_core.tools import StructuredTool
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError

from tai42_agents._internal import base_tool_agent as bta
from tai42_agents._internal.usage import CallUsage


def _tool(name: str) -> StructuredTool:
    async def _run(**_: Any) -> str:
        return "ok"

    return StructuredTool.from_function(func=None, coroutine=_run, name=name, description="d")


def _patch_seams(
    monkeypatch: pytest.MonkeyPatch, *, state: Any = None, chunks: list[Any] | None = None
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        bta,
        "llm_provider_settings",
        lambda: SimpleNamespace(llm="def_llm", checkpoint="def_checkpoint", checkpoint_conn_string="cp-conn"),
    )
    monkeypatch.setattr(bta, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))

    async def fake_get_llm(*, provider: str, **kwargs: Any) -> Any:
        captured["llm_provider"] = provider
        captured["llm_kwargs"] = kwargs
        return "llm-obj"

    monkeypatch.setattr(bta, "get_llm_async", fake_get_llm)

    async def fake_get_checkpointer(*, provider: str, conn_string: str) -> Any:
        captured["checkpoint"] = (provider, conn_string)
        return "checkpointer-obj"

    monkeypatch.setattr(bta, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=fake_get_checkpointer))
    monkeypatch.setattr(bta, "context_overflow_middlewares", lambda system_prompt=None: ["mw"])
    monkeypatch.setattr(bta, "logging_settings", lambda: SimpleNamespace(is_enabled_for=lambda level: level == "DEBUG"))
    monkeypatch.setattr(bta, "init_langgraph_config", lambda config: {"configurable": {"thread_id": "t"}})

    fake_agent = MagicMock()
    fake_agent.ainvoke = AsyncMock(return_value=state if state is not None else {"messages": []})
    # The faces repair a thread's dangling tool_calls before running: a fresh thread
    # has no checkpointed messages, so the double reports empty state (no repair).
    fake_agent.aget_state = AsyncMock(return_value=SimpleNamespace(values={}))
    fake_agent.aupdate_state = AsyncMock()

    async def fake_astream(messages: Any, config: Any, stream_mode: str) -> AsyncIterator[Any]:
        captured["stream"] = (messages, config, stream_mode)
        for chunk in chunks or []:
            yield chunk

    fake_agent.astream = fake_astream

    def fake_create_agent(
        llm: Any,
        *,
        tools: Any,
        checkpointer: Any,
        middleware: Any,
        debug: bool,
        response_format: Any = None,
        system_prompt: Any = None,
    ) -> Any:
        captured["create"] = {
            "llm": llm,
            "tools": tools,
            "checkpointer": checkpointer,
            "middleware": middleware,
            "debug": debug,
            "response_format": response_format,
            "system_prompt": system_prompt,
        }
        return fake_agent

    monkeypatch.setattr(bta, "create_agent", fake_create_agent)
    return captured


async def _collect(agen: AsyncIterator[Any]) -> list[Any]:
    return [chunk async for chunk in agen]


class TestBuildAgentAndInput:
    def test_resolves_default_providers_and_wires_create_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _patch_seams(monkeypatch)
        tool = _tool("search")

        agent, messages, config = asyncio.run(
            bta._build_agent_and_input("sys-prompt", ["hi"], [tool], llm_kwargs={"temperature": 0})
        )

        assert agent is not None
        assert config == {"configurable": {"thread_id": "t"}}
        # Default providers fell back to the settings values.
        assert captured["llm_provider"] == "def_llm"
        assert captured["checkpoint"] == ("def_checkpoint", "cp-conn")
        assert captured["llm_kwargs"] == {"temperature": 0}
        # create_agent wired with the resolved model, tools, checkpointer, middleware.
        assert captured["create"]["llm"] == "llm-obj"
        assert captured["create"]["tools"] == [tool]
        assert captured["create"]["checkpointer"] == "checkpointer-obj"
        # The park hook leads (it is the loop's first before_model hook, so it recognizes an
        # async-ask park before any compacting hook could evict its marked ToolMessage);
        # the system-purge middleware follows (state never carries a system message), the
        # context-overflow middleware is threaded through, the rolling-cache-mark middleware
        # keeps one breakpoint at the call, and the tool-error middleware is appended so a
        # tool-logic failure never aborts the loop.
        assert isinstance(captured["create"]["middleware"][0], bta.AsyncParkMiddleware)
        assert isinstance(captured["create"]["middleware"][1], bta.SystemPurgeMiddleware)
        assert captured["create"]["middleware"][2] == "mw"
        assert any(isinstance(mw, bta.RollingCacheMarkMiddleware) for mw in captured["create"]["middleware"])
        assert captured["create"]["middleware"][-1] is bta._tool_error_middleware
        assert captured["create"]["debug"] is True
        # No response_format requested -> the text-behavior default is threaded through.
        assert captured["create"]["response_format"] is None
        # The system prompt is per-run graph configuration, not an input message:
        # it reaches create_agent as system_prompt and the built input carries only
        # the user message, so checkpointed state stays system-free.
        assert isinstance(captured["create"]["system_prompt"], SystemMessage)
        assert captured["create"]["system_prompt"].content == "sys-prompt"
        assert messages == {"messages": [{"role": "user", "content": "hi"}]}

    def test_system_content_kwargs_become_a_system_prompt_content_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _patch_seams(monkeypatch)
        asyncio.run(
            bta._build_agent_and_input(
                "sys-prompt", ["hi"], [], system_content_kwargs={"cache_control": {"type": "ephemeral"}}
            )
        )
        # The cache_control block form survives into the per-run system prompt.
        system_prompt = captured["create"]["system_prompt"]
        assert isinstance(system_prompt, SystemMessage)
        assert system_prompt.content == [{"type": "text", "text": "sys-prompt", "cache_control": {"type": "ephemeral"}}]

    def test_user_content_kwargs_mark_the_last_input_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_seams(monkeypatch)
        _, messages, _ = asyncio.run(
            bta._build_agent_and_input(
                "sys", ["first", "last"], [], user_content_kwargs={"cache_control": {"type": "ephemeral"}}
            )
        )
        # Only the final user turn carries the content-block form; earlier turns stay plain.
        assert messages == {
            "messages": [
                {"role": "user", "content": "first"},
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "last", "cache_control": {"type": "ephemeral"}}],
                },
            ]
        }

    def test_system_and_user_content_kwargs_apply_together(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _patch_seams(monkeypatch)
        _, messages, _ = asyncio.run(
            bta._build_agent_and_input(
                "sys-prompt",
                ["hi"],
                [],
                system_content_kwargs={"cache_control": {"type": "ephemeral"}},
                user_content_kwargs={"cache_control": {"type": "ephemeral"}},
            )
        )
        # The system prompt block and the last user message block both carry the keys.
        system_prompt = captured["create"]["system_prompt"]
        assert isinstance(system_prompt, SystemMessage)
        assert system_prompt.content == [{"type": "text", "text": "sys-prompt", "cache_control": {"type": "ephemeral"}}]
        assert messages["messages"][-1]["content"] == [
            {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
        ]

    def test_response_format_is_threaded_into_create_agent_as_tool_strategy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _patch_seams(monkeypatch)
        schema = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}
        asyncio.run(bta._build_agent_and_input("sys", ["hi"], [], response_format=schema))
        # The raw schema dict is pinned to the tool-calling strategy (never left to
        # provider-dependent auto-routing) as a TypedDict whose parse round-trips a
        # value back to the dict shape while enforcing the injected int64 bound.
        from pydantic import TypeAdapter, ValidationError

        threaded = captured["create"]["response_format"]
        assert isinstance(threaded, ToolStrategy)
        adapter = TypeAdapter(threaded.schema)
        assert adapter.validate_python({"value": 7}) == {"value": 7}
        with pytest.raises(ValidationError):
            adapter.validate_python({"value": 9223372036854775807 + 1})

    def test_explicit_providers_override_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _patch_seams(monkeypatch)
        asyncio.run(
            bta._build_agent_and_input("sys", ["hi"], [], llm_provider="my_llm", checkpoint_provider="my_checkpoint")
        )
        assert captured["llm_provider"] == "my_llm"
        assert captured["checkpoint"][0] == "my_checkpoint"


class TestAinvoke:
    def test_returns_user_output_and_aggregated_usage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = {"messages": ["state"]}
        _patch_seams(monkeypatch, state=state)
        usage = CallUsage(3, 2, "scripted")
        monkeypatch.setattr(bta, "build_user_output", lambda s: f"out:{s['messages'][0]}")
        monkeypatch.setattr(bta, "aggregate_usage", lambda s: usage)

        result = asyncio.run(bta.ainvoke_tools_agent("sys", ["hi"], [_tool("t")]))

        assert result.output == "out:state"
        assert result.usage is usage
        # No response_format requested -> no structured payload on the result.
        assert result.structured is None

    def test_returns_structured_response_when_requested(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = {"messages": ["state"], "structured_response": {"value": 7}}
        _patch_seams(monkeypatch, state=state)
        monkeypatch.setattr(bta, "build_user_output", lambda s: "text")
        monkeypatch.setattr(bta, "aggregate_usage", lambda s: CallUsage(0, 0, None))

        schema = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}
        result = asyncio.run(bta.ainvoke_tools_agent("sys", ["hi"], [_tool("t")], response_format=schema))

        # The structured output the run wrote to state["structured_response"] is
        # surfaced on the invoke result for the direct-invoke structured path.
        assert result.structured == {"value": 7}

    def test_requested_but_missing_structured_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = {"messages": ["state"]}
        _patch_seams(monkeypatch, state=state)
        monkeypatch.setattr(bta, "build_user_output", lambda s: "text")
        monkeypatch.setattr(bta, "aggregate_usage", lambda s: CallUsage(0, 0, None))

        schema = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}
        with pytest.raises(RuntimeError, match="no structured_response"):
            asyncio.run(bta.ainvoke_tools_agent("sys", ["hi"], [_tool("t")], response_format=schema))

    def test_nonconforming_structured_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A structured output violating a schema constraint keyword raises loudly
        instead of being surfaced on the result."""
        state = {"messages": ["state"], "structured_response": {"value": -1}}
        _patch_seams(monkeypatch, state=state)
        monkeypatch.setattr(bta, "build_user_output", lambda s: "text")
        monkeypatch.setattr(bta, "aggregate_usage", lambda s: CallUsage(0, 0, None))

        schema = {
            "title": "Answer",
            "type": "object",
            "properties": {"value": {"type": "integer", "minimum": 0}},
            "required": ["value"],
        }
        with pytest.raises(JsonSchemaValidationError):
            asyncio.run(bta.ainvoke_tools_agent("sys", ["hi"], [_tool("t")], response_format=schema))


class TestUserContentKwargsReachTheInvokeCall:
    def test_wrapper_forwards_user_content_kwargs_onto_the_last_invoke_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fake only the compile — the real ``_build_agent_and_input`` build and message
        # forwarding run — and capture the messages the wrapper hands to agent.ainvoke.
        # This crosses the ainvoke_tools_agent -> _build_agent_and_input ->
        # build_agent_input hop end to end, proving the mark reaches the model call and
        # not just the builder the other tests stop at.
        captured: dict[str, Any] = {}

        fake_agent = MagicMock()

        async def fake_ainvoke(messages: Any, config: Any) -> Any:
            captured["messages"] = messages
            return {"messages": []}

        fake_agent.ainvoke = fake_ainvoke
        # A fresh thread reports empty state, so the turn-start repair is a no-op.
        fake_agent.aget_state = AsyncMock(return_value=SimpleNamespace(values={}))

        async def fake_compile(*args: Any, **kwargs: Any) -> Any:
            return fake_agent

        monkeypatch.setattr(bta, "_compile_tools_agent", fake_compile)
        monkeypatch.setattr(bta, "build_user_output", lambda s: "")
        monkeypatch.setattr(bta, "aggregate_usage", lambda s: CallUsage(0, 0, None))

        asyncio.run(
            bta.ainvoke_tools_agent(
                "sys",
                ["first", "last"],
                [],
                config={"configurable": {"thread_id": "t-hop"}},
                user_content_kwargs={"cache_control": {"type": "ephemeral"}},
            )
        )

        # The final user turn reaching the invoke carries the content-block mark; the
        # earlier turn stays a plain string.
        assert captured["messages"] == {
            "messages": [
                {"role": "user", "content": "first"},
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "last", "cache_control": {"type": "ephemeral"}}],
                },
            ]
        }


class TestAstream:
    def test_yields_raw_chunks_and_threads_stream_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _patch_seams(monkeypatch, chunks=["chunk-a", "chunk-b"])

        chunks = asyncio.run(_collect(bta.astream_tools_agent("sys", ["hi"], [_tool("t")], stream_mode="updates")))

        assert chunks == ["chunk-a", "chunk-b"]
        _, config, stream_mode = captured["stream"]
        assert stream_mode == "updates"
        assert config == {"configurable": {"thread_id": "t"}}
