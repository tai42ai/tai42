"""Native-mode rail parity (the reviewed gap) + the native projection.

Under the native plan the factory parses the JSON payload but does NOT validate it against
the authored schema — so a WELL-FORMED but non-conforming payload (an int64-oversized
integer, an enum violation) escaped as a raw validation error downstream, with no re-prompt.
The rail now validates every ``structured_response`` in-node against the ORIGINAL authored
schema, exactly like tool mode's bounded parse, and re-prompts under the same per-run cap:

* a violating-then-conforming payload succeeds in exactly two model calls, the second request
  carrying a re-prompt turn, and no ``JsonSchemaValidationError`` escapes the face;
* a never-conforming payload ends on the typed ``StructuredOutputUnresolvedFinal`` (attempts
  == cap + 1), never a raw raise;
* malformed JSON is re-prompted the same way.

The projection emits the single validated ``StructuredFinal`` (validated against the original
type-array schema) and NO ``MessageDelta``/``MessageFinal`` under the native plan.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr
from tai42_contract.agent.events import (
    MessageDelta,
    MessageFinal,
    StructuredFinal,
    StructuredOutputUnresolvedFinal,
)
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError

from tai42_agents._internal import base_tool_agent as bta
from tai42_agents._internal.stream_events import aproject_agent_events
from tai42_agents._internal.structured import structured_output_stack

from ._graph_support import invoke_tools_agent
from .conftest import fake_run_trace

_INT_SCHEMA = {
    "title": "TurnIntake",
    "type": "object",
    "properties": {"value": {"type": ["integer", "null"]}},
    "required": ["value"],
}
_ENUM_SCHEMA = {
    "title": "Pick",
    "type": "object",
    "properties": {"color": {"type": "string", "enum": ["red", "green"]}},
    "required": ["color"],
}

_INT64_OVER = 9223372036854775808  # 2**63, one past the platform int64 ceiling


class _NativeModel(BaseChatModel):
    """Declares native structured output and answers scripted JSON text, recording each
    message list it was called with (so a re-prompt turn can be asserted)."""

    _texts: list[str] = PrivateAttr(default_factory=list)
    calls: int = 0

    def __init__(self, texts: Sequence[str], **kwargs: Any) -> None:
        super().__init__(profile={"structured_output": True}, **kwargs)
        self._texts = list(texts)
        self._seen: list[list[BaseMessage]] = []

    @property
    def seen(self) -> list[list[BaseMessage]]:
        return self._seen

    @property
    def _llm_type(self) -> str:
        return "native"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self._seen.append(list(messages))
        index = min(self.calls, len(self._texts) - 1)
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self._texts[index]))])


def _compile(model: BaseChatModel, schema: Any) -> tuple[Any, Any]:
    strategy, rail = structured_output_stack(model, "openai", schema)
    assert rail is not None
    graph = create_agent(model, tools=[], checkpointer=InMemorySaver(), response_format=strategy, middleware=[rail])
    return graph, strategy


def _project(model: BaseChatModel, schema: Any, cap: int, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    monkeypatch.setattr(
        "tai42_agents._internal.structured.agents_limits_settings",
        lambda: SimpleNamespace(structured_output_reprompt_cap=cap),
    )
    graph, strategy = _compile(model, schema)

    async def go() -> list[Any]:
        return [
            event
            async for event in aproject_agent_events(
                graph,
                {"messages": [HumanMessage(content="go")]},
                {"configurable": {"thread_id": "t"}, "recursion_limit": 50},
                response_format=schema,
                structured_strategy=strategy,
            )
        ]

    return asyncio.run(go())


def test_oversized_int_is_reprompted_then_conforms_in_two_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _NativeModel([f'{{"value": {_INT64_OVER}}}', '{"value": 7}'])
    events = _project(model, _INT_SCHEMA, cap=3, monkeypatch=monkeypatch)

    finals = [event for event in events if isinstance(event, StructuredFinal)]
    assert [final.data for final in finals] == [{"value": 7}]
    assert model.calls == 2
    # No raw validation error escaped the projection; the single final carries the validated value.
    assert not any(isinstance(event, StructuredOutputUnresolvedFinal) for event in events)
    # The second model call's messages carry the appended re-prompt HumanMessage.
    assert any(isinstance(message, HumanMessage) for message in model.seen[1][1:])


def test_enum_violation_is_reprompted_then_conforms(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _NativeModel(['{"color": "blue"}', '{"color": "green"}'])
    events = _project(model, _ENUM_SCHEMA, cap=3, monkeypatch=monkeypatch)
    finals = [event for event in events if isinstance(event, StructuredFinal)]
    assert [final.data for final in finals] == [{"color": "green"}]
    assert model.calls == 2


def test_malformed_json_is_reprompted_then_conforms(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _NativeModel(["not json at all", '{"value": 7}'])
    events = _project(model, _INT_SCHEMA, cap=3, monkeypatch=monkeypatch)
    finals = [event for event in events if isinstance(event, StructuredFinal)]
    assert [final.data for final in finals] == [{"value": 7}]
    assert model.calls == 2


def test_never_conforming_native_payload_ends_on_the_typed_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _NativeModel([f'{{"value": {_INT64_OVER}}}'])
    events = _project(model, _INT_SCHEMA, cap=3, monkeypatch=monkeypatch)

    terminal = events[-1]
    assert isinstance(terminal, StructuredOutputUnresolvedFinal)
    assert terminal.attempts == 4  # cap + 1
    assert model.calls == 4
    # The reviewed gap: a raw JsonSchemaValidationError must NOT escape — the typed outcome stands.
    assert not any(isinstance(event, StructuredFinal) for event in events)


def test_projection_suppresses_message_frames_under_the_native_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _NativeModel(['{"value": 7}'])
    events = _project(model, _INT_SCHEMA, cap=3, monkeypatch=monkeypatch)
    assert not any(isinstance(event, MessageDelta | MessageFinal) for event in events)
    structured = [event for event in events if isinstance(event, StructuredFinal)]
    assert [final.data for final in structured] == [{"value": 7}]


def _patch_invoke_seams(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, cap: int) -> None:
    monkeypatch.setattr(
        "tai42_agents._internal.structured.agents_limits_settings",
        lambda: SimpleNamespace(structured_output_reprompt_cap=cap),
    )
    monkeypatch.setattr(
        bta, "llm_provider_settings", lambda: SimpleNamespace(llm="openai", checkpoint="c", checkpoint_conn_string="s")
    )
    monkeypatch.setattr(bta, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))

    async def _get_llm(*, provider: str, **kwargs: Any) -> BaseChatModel:
        return model

    monkeypatch.setattr(bta, "get_llm_async", _get_llm)
    saver = InMemorySaver()

    async def _get_checkpointer(*, provider: str, conn_string: str) -> Any:
        return saver

    monkeypatch.setattr(bta, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=_get_checkpointer))

    async def _no_overflow(*, system_prompt: Any) -> list[Any]:
        return []

    monkeypatch.setattr(bta, "context_overflow_middlewares", _no_overflow)
    monkeypatch.setattr(bta, "logging_settings", lambda: SimpleNamespace(is_enabled_for=lambda level: False))
    monkeypatch.setattr(
        bta,
        "init_langgraph_config",
        lambda config: fake_run_trace({"configurable": {"thread_id": "t"}, "recursion_limit": 50}),
    )


def test_invoke_face_never_conforming_returns_the_typed_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _NativeModel([f'{{"value": {_INT64_OVER}}}'])
    _patch_invoke_seams(monkeypatch, model, cap=3)

    result = asyncio.run(
        invoke_tools_agent(system_message="", user_message=["go"], tools=[], response_format=_INT_SCHEMA)
    )
    assert isinstance(result.outcome, StructuredOutputUnresolvedFinal)
    assert result.outcome.attempts == 4
    assert model.calls == 4


def test_invoke_face_violating_then_conforming_returns_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _NativeModel([f'{{"value": {_INT64_OVER}}}', '{"value": 7}'])
    _patch_invoke_seams(monkeypatch, model, cap=3)

    result = asyncio.run(
        invoke_tools_agent(system_message="", user_message=["go"], tools=[], response_format=_INT_SCHEMA)
    )
    assert result.outcome is None
    assert result.structured == {"value": 7}
    assert model.calls == 2


def test_no_jsonschema_validation_error_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole point of the revision: the well-formed-but-violating native payload is caught
    # in-node, so JsonSchemaValidationError never propagates out of the face.
    model = _NativeModel([f'{{"value": {_INT64_OVER}}}'])
    _patch_invoke_seams(monkeypatch, model, cap=1)
    try:
        result = asyncio.run(
            invoke_tools_agent(system_message="", user_message=["go"], tools=[], response_format=_INT_SCHEMA)
        )
    except JsonSchemaValidationError as exc:  # pragma: no cover - the revision closes this path
        pytest.fail(f"a raw JsonSchemaValidationError escaped the native face: {exc}")
    assert isinstance(result.outcome, StructuredOutputUnresolvedFinal)
