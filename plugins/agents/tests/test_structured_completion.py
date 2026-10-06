"""``ainvoke_structured``: the single-shot capped loop the graph-less doors share.

Native: bind the kit kwargs, parse the JSON text, validate against the authored schema.
A malformed or schema-violating payload is re-prompted through the same per-run counter as
the graph rail; past the cap it raises ``RepromptCapError`` (which each single-shot face maps
to a typed outcome).
"""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from tai42_kit.llm.structured import plan_structured_output

from tai42_agents._internal import structured as structured_mod
from tai42_agents._internal.outcomes import RepromptCapError
from tai42_agents._internal.structured import ainvoke_structured

_SCHEMA = {
    "title": "Answer",
    "type": "object",
    "properties": {"value": {"type": "integer", "minimum": 0}},
    "required": ["value"],
}


class _NativeFake:
    """Records each ``bind`` kwarg set and answers scripted JSON text."""

    profile = {"structured_output": True}  # noqa: RUF012

    def __init__(self, texts: Sequence[str]) -> None:
        self._texts = list(texts)
        self.calls = 0
        self.bind_kwargs: list[dict[str, Any]] = []

    def bind(self, **kwargs: Any) -> Any:
        self.bind_kwargs.append(kwargs)
        outer = self

        class _Runner:
            async def ainvoke(self, _messages: Any, _config: object = None) -> AIMessage:
                index = min(outer.calls, len(outer._texts) - 1)
                outer.calls += 1
                return AIMessage(content=outer._texts[index])

        return _Runner()


def _llm(texts: Sequence[str]) -> BaseChatModel:
    return cast(BaseChatModel, _NativeFake(texts))


def _cap(monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    monkeypatch.setattr(
        structured_mod, "agents_limits_settings", lambda: SimpleNamespace(structured_output_reprompt_cap=cap)
    )


async def _invoke(llm: BaseChatModel) -> Any:
    plan = plan_structured_output(llm, "openai", _SCHEMA)
    return await ainvoke_structured(llm, plan, [HumanMessage(content="go")])


def test_native_binds_the_kit_kwargs_and_returns_the_validated_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    _cap(monkeypatch, 3)
    fake = _NativeFake(['{"value": 7}'])
    result = asyncio.run(_invoke(cast(BaseChatModel, fake)))
    assert result == {"value": 7}
    assert fake.bind_kwargs
    assert "response_format" in fake.bind_kwargs[0]


def test_malformed_then_valid_is_reprompted(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    _cap(monkeypatch, 3)
    fake = _NativeFake(["not json", '{"value": 7}'])
    result = asyncio.run(_invoke(cast(BaseChatModel, fake)))
    assert result == {"value": 7}
    assert fake.calls == 2


def test_violating_then_valid_is_reprompted(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    _cap(monkeypatch, 3)
    fake = _NativeFake(['{"value": -1}', '{"value": 7}'])
    result = asyncio.run(_invoke(cast(BaseChatModel, fake)))
    assert result == {"value": 7}
    assert fake.calls == 2


def test_past_the_cap_raises_reprompt_cap_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    _cap(monkeypatch, 2)
    fake = _NativeFake(['{"value": -1}'])
    with pytest.raises(RepromptCapError) as excinfo:
        asyncio.run(_invoke(cast(BaseChatModel, fake)))
    assert excinfo.value.attempts == 3  # cap + 1
    assert fake.calls == 3
