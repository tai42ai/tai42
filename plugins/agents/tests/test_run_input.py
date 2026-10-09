"""The ``langchain_deep_agent`` JSON run-door contract.

``DeepAgentInput`` schema strictness (``extra="forbid"``, content-kwargs
normalization) and the undeclared-keyword refusal both faces enforce. Async code is
driven with ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError
from tai42_contract.agent.base import PresetSpec
from tai42_contract.template import TemplatedText
from tests._deep_agent_fakes import _drain_astream

from tai42_agents.langchain_deep_agent.agent import DeepAgent
from tai42_agents.langchain_deep_agent.run_input import DeepAgentInput


def test_deep_agent_input_rejects_unknown_key() -> None:
    """``DeepAgentInput`` sets ``extra="forbid"`` so an unknown key at the run door is a
    loud validation error rather than a silently dropped typo."""
    with pytest.raises(ValidationError):
        DeepAgentInput.model_validate({"totally_unknown_key": 1})
    # A field the runtime does not honor (deep applies no composition strategy) is
    # simply not part of the schema, so it is rejected like any other unknown key.
    with pytest.raises(ValidationError):
        DeepAgentInput.model_validate({"strategy": "vote"})


def test_deep_agent_input_empty_content_kwargs_normalize_to_none() -> None:
    """An empty ``user_content_kwargs`` dict from the JSON door reads as absent — the
    builders treat {} as no mark, so the field normalizes to None rather than a
    set-but-empty value the unhonored-reject face would misread."""
    validated = DeepAgentInput.model_validate({"user_content_kwargs": {}})
    assert validated.user_content_kwargs is None
    # A non-empty mark is a real value and rides through unchanged.
    marked = DeepAgentInput.model_validate({"user_content_kwargs": {"cache_control": {"type": "ephemeral"}}})
    assert marked.user_content_kwargs == {"cache_control": {"type": "ephemeral"}}


# Keywords the deep agent does not declare. Both faces refuse each by name, never drop it.
_UNDECLARED_CASES = [
    ("presets", [PresetSpec(name="p", base_tool="calc")]),
    ("strategy", "vote"),
    ("system_content_kwargs", {"cache_control": {"type": "ephemeral"}}),
    ("resume_checkpoint_id", "cp-7"),
]


@pytest.mark.parametrize(("param", "value"), _UNDECLARED_CASES)
def test_run_refuses_an_undeclared_keyword(param: str, value: Any) -> None:
    with pytest.raises(TypeError, match=rf"langchain_deep_agent\.run got unknown run keyword argument\(s\): {param}"):
        asyncio.run(DeepAgent().run(user_message=TemplatedText(content="go"), **{param: value}))


@pytest.mark.parametrize(("param", "value"), _UNDECLARED_CASES)
def test_astream_refuses_an_undeclared_keyword(param: str, value: Any) -> None:
    with pytest.raises(
        TypeError, match=rf"langchain_deep_agent\.astream got unknown run keyword argument\(s\): {param}"
    ):
        _drain_astream(DeepAgent().astream(user_message=TemplatedText(content="go"), **{param: value}))


def test_run_names_every_undeclared_keyword_at_once() -> None:
    with pytest.raises(TypeError, match=r"unknown run keyword argument\(s\): presets, strategy"):
        asyncio.run(
            DeepAgent().run(
                user_message=TemplatedText(content="go"),
                presets=[PresetSpec(name="p", base_tool="calc")],
                strategy="vote",
            )
        )
