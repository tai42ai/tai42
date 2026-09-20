"""Shared rig for the ``langchain_deep_agent`` factory test modules: the fake LLM,
bind assertion, fake runnable and tool, the general-purpose and nested-pair
specs, the inline-skill spec, and the recording chat model.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import pytest
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, PrivateAttr, TypeAdapter, ValidationError
from tai42_contract.template import TemplatedText

from tai42_agents.langchain_deep_agent.spec import InlineSkill, ResolvedSubAgentSpec

_FAKE_LLM = cast(BaseChatModel, "LLM")


_INT64_OVERFLOW = 2**63


def _assert_bound_to_model(strategy: ToolStrategy[Any], model: type[BaseModel]) -> None:
    """A subagent whose response_format was ``model`` emits a bounded ToolStrategy:
    its schema (validated exactly as the tool-calling parse does, via a pydantic
    ``TypeAdapter``) accepts a conforming value, rejects an oversized int under the
    int64 bound, and binds under the model's name so structured tool-name stream
    suppression still matches."""
    adapter = TypeAdapter(strategy.schema)
    assert adapter.validate_python({"x": 1}) == {"x": 1}
    with pytest.raises(ValidationError):
        adapter.validate_python({"x": _INT64_OVERFLOW})
    assert getattr(strategy.schema, "__name__", None) == model.__name__


class _FakeRunnable:
    """A minimal runnable stand-in for a compiled nested subagent.

    ``deepagents``' ``SubAgentMiddleware`` eagerly compiles each nested runnable
    at construction (calling ``with_config``), so the fake ``create_deep_agent``
    must return an object exposing it rather than a bare string.
    """

    def with_config(self, *args: object, **kwargs: object) -> _FakeRunnable:
        return self


def _tool(name: str) -> StructuredTool:
    async def call_tool(**kwargs: object) -> object:
        return kwargs

    return StructuredTool.from_function(
        func=None,
        coroutine=call_tool,
        name=name,
        description="d",
        args_schema={"type": "object", "properties": {}, "required": []},
    )


def _injected_gp(captured: dict[str, Any]) -> dict[str, Any]:
    (gp,) = [s for s in captured["subagents"] if s["name"] == "general-purpose"]
    return cast(dict[str, Any], gp)


def _nested_pair(
    child_tools: list[StructuredTool] | None = None,
    parent_tools: list[StructuredTool] | None = None,
) -> tuple[ResolvedSubAgentSpec, ResolvedSubAgentSpec]:
    child = ResolvedSubAgentSpec(
        name="finder", description="cd", system_prompt=TemplatedText(content="cp"), tools=child_tools or []
    )
    parent = ResolvedSubAgentSpec(
        name="advisor",
        description="d",
        system_prompt=TemplatedText(content="p"),
        tools=parent_tools or [],
        subagents=[child],
    )
    return parent, child


def _inline(name: str, content: str) -> InlineSkill:
    return InlineSkill(name=name, content=content)


class _RecordingChatModel(BaseChatModel):
    """Returns a fixed message per call and records the exact message list each model
    call received; ``bind_tools`` is a no-op so deepagents can bind its built-ins."""

    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)
    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self._seen.append(list(messages))
        message = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self


def _mark_count(messages: list[BaseMessage]) -> int:
    """Number of messages carrying a ``cache_control`` block (a cache breakpoint)."""
    return sum(
        isinstance(m.content, list) and any(isinstance(b, dict) and "cache_control" in b for b in m.content)
        for m in messages
    )
