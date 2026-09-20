"""The ``langchain_deep_agent`` JSON run-door contract.

``DeepAgentInput`` schema strictness (``extra="forbid"``, content-kwargs
normalization) and the unhonored-parameter guard both faces enforce off the shared
``_UNHONORED_REASONS`` / ``_UNHONORED_COLLECTION_PARAMS`` maps. Async code is driven
with ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from pydantic import ValidationError
from tai42_contract.agent import Agent
from tai42_contract.agent.base import PresetSpec
from tai42_contract.template import TemplatedText
from tests._deep_agent_fakes import _drain_astream

from tai42_agents._internal.reject import reject_unhonored
from tai42_agents.langchain_deep_agent.agent import DeepAgent
from tai42_agents.langchain_deep_agent.run_input import (
    _UNHONORED_COLLECTION_PARAMS,
    _UNHONORED_REASONS,
    DeepAgentInput,
)


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


def test_run_rejects_presets() -> None:
    """Main-agent presets are not a composable langchain_deep_agent input; run raises loudly
    rather than silently dropping them."""
    with pytest.raises(RuntimeError, match="does not support presets"):
        asyncio.run(
            DeepAgent().run(user_message=TemplatedText(content="go"), presets=[PresetSpec(name="p", base_tool="calc")])
        )


def test_run_rejects_strategy() -> None:
    """langchain_deep_agent applies no composition strategy; run raises rather than ignoring one."""
    with pytest.raises(RuntimeError, match="does not support strategy"):
        asyncio.run(DeepAgent().run(user_message=TemplatedText(content="go"), strategy="vote"))


def test_run_names_both_offenders_at_once() -> None:
    """A caller passing BOTH unhonored params is named both at once — one message
    lists ``presets`` and ``strategy`` together, so the caller fixes both in one pass
    rather than one raise per run."""
    with pytest.raises(RuntimeError, match=r"does not support presets, strategy"):
        asyncio.run(
            DeepAgent().run(
                user_message=TemplatedText(content="go"),
                presets=[PresetSpec(name="p", base_tool="calc")],
                strategy="vote",
            )
        )


def test_astream_rejects_presets() -> None:
    """Main-agent presets are not a composable langchain_deep_agent input; astream raises loudly
    rather than silently dropping them (parity with run)."""
    with pytest.raises(RuntimeError, match="does not support presets"):
        _drain_astream(
            DeepAgent().astream(
                user_message=TemplatedText(content="go"), presets=[PresetSpec(name="p", base_tool="calc")]
            )
        )


def test_astream_rejects_strategy() -> None:
    """langchain_deep_agent applies no composition strategy; astream raises rather than ignoring
    one (parity with run)."""
    with pytest.raises(RuntimeError, match="does not support strategy"):
        _drain_astream(DeepAgent().astream(user_message=TemplatedText(content="go"), strategy="vote"))


# Every key in ``_UNHONORED_REASONS`` paired with a representative SET value. Both
# faces reject each, so dropping a key from the map fails a case here.
_UNHONORED_CASES = [
    ("presets", [PresetSpec(name="p", base_tool="calc")]),
    ("strategy", "vote"),
    ("system_content_kwargs", {"cache_control": {"type": "ephemeral"}}),
    # The durable sandbox WORKSPACE cannot be forked alongside the checkpoint, so a set
    # resume_checkpoint_id is unhonored (loud reject) on the durable deep agent.
    ("resume_checkpoint_id", "cp-7"),
]


@pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
def test_run_rejects_every_unhonored_param(param: str, value: Any) -> None:
    """run rejects every key in the guard's reasons map, naming it and the run face."""
    with pytest.raises(RuntimeError, match=rf"langchain_deep_agent\.run does not support .*\b{param}\b"):
        asyncio.run(DeepAgent().run(user_message=TemplatedText(content="go"), **{param: value}))


@pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
def test_astream_rejects_every_unhonored_param(param: str, value: Any) -> None:
    """astream rejects the same full set as run — parity — naming the astream face."""
    with pytest.raises(RuntimeError, match=rf"langchain_deep_agent\.astream does not support .*\b{param}\b"):
        _drain_astream(DeepAgent().astream(user_message=TemplatedText(content="go"), **{param: value}))


def test_unhonored_cases_cover_the_full_reasons_map() -> None:
    """Every key in the reasons map has a parametrized reject case; a key added without
    a test fails here immediately."""
    assert {param for param, _ in _UNHONORED_CASES} == set(_UNHONORED_REASONS)


# The unhonored params whose ABC ``Agent.run`` default is an empty collection (``()`` /
# ``""``), read from the contract signature — the independent source of truth for which
# unhonored params are collection-typed. Intersected with this agent's reasons map it is
# exactly the set ``_UNHONORED_COLLECTION_PARAMS`` must classify as collections. The
# empty-collection test below parametrizes from HERE, not from the frozenset, so a member
# dropped from the frozenset (reclassifying it as a scalar) turns a case red rather than
# silently vanishing.
_EMPTY_COLLECTION_ABC_DEFAULTS = frozenset(
    name
    for name, parameter in inspect.signature(Agent.run).parameters.items()
    if isinstance(parameter.default, (tuple, list, str)) and not parameter.default
)
_COLLECTION_REJECT_PARAMS = sorted(_UNHONORED_REASONS.keys() & _EMPTY_COLLECTION_ABC_DEFAULTS)


def test_collection_params_match_the_abc_collection_defaults() -> None:
    """``_UNHONORED_COLLECTION_PARAMS`` is exactly this agent's unhonored params whose ABC
    default is an empty collection: no scalar wrongly listed (which would let a meaningful
    falsy value slip through), none dropped (which would over-reject the not-requested
    empty default)."""
    assert set(_UNHONORED_COLLECTION_PARAMS) == set(_COLLECTION_REJECT_PARAMS)


@pytest.mark.parametrize("empty", [[], ""])
@pytest.mark.parametrize("param", _COLLECTION_REJECT_PARAMS)
def test_reject_unhonored_permits_empty_collection_param(param: str, empty: object) -> None:
    """An empty collection is the ABC's "not requested" default for a collection parameter,
    so the guard does not raise for it — in either falsy empty form (``[]`` / ``""``). Were
    the parameter dropped from ``_UNHONORED_COLLECTION_PARAMS`` it would be classified as a
    scalar (set whenever it is not ``None``) and this empty value would raise."""
    reject_unhonored(
        "langchain_deep_agent.run",
        {param: empty},
        _UNHONORED_REASONS,
        collection_params=_UNHONORED_COLLECTION_PARAMS,
    )
