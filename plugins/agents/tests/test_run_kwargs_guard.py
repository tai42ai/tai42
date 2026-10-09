"""Every agent face declares the run keywords it reads and refuses any other loudly.

Per kind: an undeclared keyword raises ``TypeError`` naming it; every key ``from_tool_input``
produces from a fully-populated ``ToolInput`` is a keyword the guarded face declares; and the
contract keys a kind refuses as unhonored are only the contract's own run keys.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

import pytest
from tai42_contract.agent import Agent
from tai42_contract.template import TemplatedText

from tai42_agents import vqa_agent as vqa_mod
from tai42_agents._internal.run_kwargs import CONTRACT_RUN_KEYS, guard_run_kwargs
from tai42_agents.claude_code.agent import ClaudeCodeAgent
from tai42_agents.langchain_deep_agent.agent import DeepAgent
from tai42_agents.refine_agent import agent as refine_mod
from tai42_agents.refine_agent.agent import RefineAgent
from tai42_agents.retrieval_tools_agent.agent import RetrievalToolsAgent
from tai42_agents.tools_agent import ToolsAgent
from tai42_agents.voting_agent import agent as voting_mod
from tai42_agents.voting_agent.agent import VotingAgent
from tai42_agents.vqa_agent import VqaAgent

_KINDS: list[type[Agent]] = [
    ToolsAgent,
    DeepAgent,
    ClaudeCodeAgent,
    VqaAgent,
    VotingAgent,
    RefineAgent,
    RetrievalToolsAgent,
]


async def _drain(stream: Any) -> None:
    async for _ in stream:
        pass


def _faces(kind: type[Agent]) -> list[tuple[str, Callable[..., Any]]]:
    """The faces of ``kind`` that run the guard, with the call that drives each with ``bogus=1``."""
    agent = kind()
    extra: dict[str, Any] = {"image_url": "http://img", "query": "q"} if kind is VqaAgent else {}
    return [
        ("run", lambda: asyncio.run(agent.run(user_message=TemplatedText(content="hi"), bogus=1, **extra))),
        ("astream", lambda: asyncio.run(_drain(agent.astream(bogus=1, **extra)))),
    ]


@pytest.mark.parametrize("kind", _KINDS, ids=lambda kind: kind.tool_name)
def test_every_face_refuses_an_undeclared_keyword_naming_it(kind: type[Agent]) -> None:
    for _face, call in _faces(kind):
        with pytest.raises(TypeError, match=rf"{kind.tool_name}\.\w+ got unknown run keyword argument\(s\): bogus"):
            call()


def _full_tool_input_keys(kind: type[Agent]) -> set[str]:
    fields: dict[str, Any] = {name: object() for name in kind.ToolInput.model_fields}
    if "system_message" in fields and "system_prompt" in fields:
        fields["system_message"] = None
    return set(kind.from_tool_input(kind.ToolInput.model_construct(**fields)))


def _declared(face: Callable[..., Any]) -> set[str]:
    return {
        name
        for name, param in inspect.signature(face).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }


@pytest.mark.parametrize("kind", _KINDS, ids=lambda kind: kind.tool_name)
def test_every_tool_input_key_is_a_declared_keyword(kind: type[Agent]) -> None:
    # The guarded face is ``astream`` (``run`` drains it, or declares the same keywords).
    assert _full_tool_input_keys(kind) <= _declared(kind.astream)


@pytest.mark.parametrize(
    "unhonored",
    [voting_mod._UNHONORED_REASONS, refine_mod._UNHONORED_REASONS, vqa_mod._UNHONORED_REASONS],
    ids=["voting_agent", "refine_agent", "vqa_agent"],
)
def test_a_kinds_unhonored_keys_are_contract_run_keys(unhonored: dict[str, str]) -> None:
    assert set(unhonored) <= CONTRACT_RUN_KEYS


def test_the_guard_refuses_unknown_keywords_before_unhonored_ones() -> None:
    with pytest.raises(TypeError, match=r"face got unknown run keyword argument\(s\): a, b"):
        guard_run_kwargs("face", {"b": 1, "a": 2}, {"thread_id": "t"}, {"thread_id": "no thread"})
    with pytest.raises(RuntimeError, match="face does not support thread_id: thread_id: no thread"):
        guard_run_kwargs("face", {}, {"thread_id": "t", "tool_names": ()}, {"thread_id": "no thread"})
    guard_run_kwargs("face", {}, {"thread_id": None, "tool_names": ()}, {"thread_id": "no thread", "tool_names": "x"})
