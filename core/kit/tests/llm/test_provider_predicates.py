"""The provider-keyed history predicates, ``history_middlewares``, and the leading-user rule through a real agent loop.

The neutral synthetic consumer is a ``create_agent`` loop over a scripted chat model and an
in-memory saver: the model sees the marker first while the checkpointed thread never holds it.
"""

from __future__ import annotations

import asyncio
import typing
from collections.abc import Sequence
from typing import Any

import pytest

pytest.importorskip("langgraph")

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr

from tai42_kit.llm import models
from tai42_kit.llm.middleware import history_middlewares
from tai42_kit.llm.middleware.leading_user import _CONVERSATION_START_MARKER, LeadingUserMiddleware
from tai42_kit.llm.middleware.rolling_cache_mark import RollingCacheMarkMiddleware
from tai42_kit.llm.models import cache_mark_key, requires_leading_user_turn, system_prompt_cache_mark

_PROVIDERS = ("anthropic", "mistral", "openai", "google", "xai", "ollama", "huggingface")

# The recorded table (hosted entries are confirmed by the real-LLM leg
# ``e2e/tests/agents/test_leading_user_turn_real_leg.py``).
_LEADING_USER = {
    "anthropic": False,
    "openai": True,
    "google": True,
    "xai": True,
    "mistral": True,
    "ollama": True,
    "huggingface": True,
}
_CACHE_MARK_KEY = {
    "anthropic": "cache_control",
    "openai": "prompt_cache_breakpoint",
    "xai": None,
    "google": None,
    "mistral": None,
    "ollama": None,
    "huggingface": None,
}


def _build_llm_providers() -> set[str]:
    """The provider names ``_build_llm``'s ``match`` accepts, read from its source."""
    import inspect

    source = inspect.getsource(models._build_llm)
    return {line.split('"')[1] for line in source.splitlines() if line.strip().startswith('case "')}


def test_the_tables_cover_every_build_llm_provider() -> None:
    assert _build_llm_providers() == set(_PROVIDERS) == set(_LEADING_USER) == set(_CACHE_MARK_KEY)


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_requires_leading_user_turn_per_provider(provider: str) -> None:
    assert requires_leading_user_turn(provider) is _LEADING_USER[provider]


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_cache_mark_key_per_provider(provider: str) -> None:
    assert cache_mark_key(provider) == _CACHE_MARK_KEY[provider]


def test_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported chat model provider"):
        requires_leading_user_turn("nope")
    with pytest.raises(ValueError, match="Unsupported chat model provider"):
        cache_mark_key("nope")
    with pytest.raises(ValueError, match="Unsupported chat model provider"):
        history_middlewares("nope")


def test_system_prompt_cache_mark_uses_the_providers_key() -> None:
    assert system_prompt_cache_mark("anthropic") == {"cache_control": {"type": "ephemeral"}}
    for provider in _PROVIDERS:
        if provider != "anthropic":
            assert system_prompt_cache_mark(provider) is None


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_history_middlewares_follow_the_predicates(provider: str) -> None:
    stack = history_middlewares(provider)
    leading = [m for m in stack if isinstance(m, LeadingUserMiddleware)]
    rolling = [m for m in stack if isinstance(m, RollingCacheMarkMiddleware)]
    assert len(leading) == (1 if _LEADING_USER[provider] else 0)
    key = _CACHE_MARK_KEY[provider]
    assert [m.mark_key for m in rolling] == ([key] if key else [])
    assert len(stack) == len(leading) + len(rolling)


@pytest.mark.parametrize(("leading", "key"), [(True, "k1"), (True, None), (False, "k2"), (False, None)])
def test_history_middlewares_for_each_answer_of_each_predicate(
    monkeypatch: pytest.MonkeyPatch, leading: bool, key: str | None
) -> None:
    import tai42_kit.llm.middleware as middleware_pkg

    monkeypatch.setattr(middleware_pkg, "requires_leading_user_turn", lambda provider: leading)
    monkeypatch.setattr(middleware_pkg, "cache_mark_key", lambda provider: key)
    stack = history_middlewares("anthropic")
    assert [type(m) for m in stack] == (
        ([LeadingUserMiddleware] if leading else []) + ([RollingCacheMarkMiddleware] if key else [])
    )


class _RecordingChatModel(BaseChatModel):
    """Answers every call with a fixed reply and records the messages each call was sent."""

    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self._seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="reply"))])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self


def _drive(history: Sequence[BaseMessage]) -> tuple[list[BaseMessage], list[BaseMessage]]:
    model = _RecordingChatModel()
    saver = InMemorySaver()
    agent = create_agent(model, tools=[], middleware=[LeadingUserMiddleware()], checkpointer=saver)
    config: Any = {"configurable": {"thread_id": "t"}}
    agent_input: Any = {"messages": list(history)}
    asyncio.run(agent.ainvoke(agent_input, config))
    state = asyncio.run(agent.aget_state(config))
    return model._seen[0], state.values["messages"]


def test_the_model_sees_the_marker_first_and_the_checkpoint_holds_none() -> None:
    sent, stored = _drive([AIMessage("operator opener", id="a"), HumanMessage("reply", id="h")])

    assert isinstance(sent[0], HumanMessage)
    assert sent[0].content == _CONVERSATION_START_MARKER
    assert [m.content for m in sent[1:]] == ["operator opener", "reply"]
    assert all(m.content != _CONVERSATION_START_MARKER for m in stored)
    assert [m.content for m in stored] == ["operator opener", "reply", "reply"]


def test_a_user_first_history_reaches_the_model_unchanged() -> None:
    sent, stored = _drive([HumanMessage("hi", id="h")])
    assert [m.content for m in sent] == ["hi"]
    assert [m.content for m in stored] == ["hi", "reply"]


def test_the_marker_lands_after_leading_system_messages() -> None:
    from tai42_kit.llm.middleware.leading_user import lead_with_user

    system = SystemMessage("rules", id="s")
    ai = AIMessage("operator opener", id="a")
    led = lead_with_user([system, ai])
    assert led is not None
    assert led[0] is system
    assert isinstance(led[1], HumanMessage)
    assert led[1].content == _CONVERSATION_START_MARKER
    assert led[2] is ai


def test_lead_with_user_is_none_when_nothing_to_lead() -> None:
    from tai42_kit.llm.middleware.leading_user import lead_with_user

    assert lead_with_user([]) is None
    assert lead_with_user([HumanMessage("hi")]) is None
    assert lead_with_user([SystemMessage("rules")]) is None


def test_history_middlewares_returns_middleware_instances() -> None:
    from langchain.agents.middleware import AgentMiddleware

    assert all(isinstance(m, AgentMiddleware) for m in history_middlewares("anthropic"))
    assert typing.get_type_hints(history_middlewares)["return"] == list[AgentMiddleware]
