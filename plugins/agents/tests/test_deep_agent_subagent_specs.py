"""Subagent-spec resolution for ``langchain_deep_agent``.

JSON ``DeepSubAgentSpec`` and the programmatic neutral ``SubAgentSpec`` both resolve
to core ``ResolvedSubAgentSpec`` — tool *names* turned into live tools (one level of
nesting), ``response_format`` schema validation, and the delivery-scope rule on a
subagent's own tools. Async code is driven with ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.tools import StructuredTool
from pydantic import ValidationError
from tai42_contract.agent.base import SubAgentSpec as NeutralSubAgentSpec
from tai42_contract.template import TemplatedText
from tests._deep_agent_fakes import _client_tool
from tests._delivery_scope import assert_delivery_scoped, probe_tool

from tai42_agents.langchain_deep_agent.spec import InlineSkill, ResolvedSubAgentSpec
from tai42_agents.langchain_deep_agent.tool_spec import (
    DeepSubAgentSpec,
    _neutral_to_internal,
    _to_internal,
    resolve_subagent_specs,
)

# ===========================================================================
# tool_spec: DeepSubAgentSpec -> ResolvedSubAgentSpec
# ===========================================================================


def test_resolve_subagent_specs_resolves_tool_names_and_nesting(app_tools: Any) -> None:
    for name in ("search", "fetch", "summarize"):
        app_tools.client_tools[name] = _client_tool(name)
    specs = [
        DeepSubAgentSpec(
            name="researcher",
            description="does research",
            system_prompt=TemplatedText(content="research things"),
            tools=["search", "fetch"],
            response_format={
                "title": "Summary",
                "type": "object",
                "properties": {"summary": {"type": "string"}},
            },
            subagents=[
                DeepSubAgentSpec(
                    name="summarizer",
                    description="summarizes",
                    system_prompt=TemplatedText(content="summarize"),
                    tools=["summarize"],
                )
            ],
        )
    ]

    resolved = asyncio.run(resolve_subagent_specs(specs))

    assert len(resolved) == 1
    parent = resolved[0]
    assert isinstance(parent, ResolvedSubAgentSpec)
    assert [tool.name for tool in parent.tools] == ["search", "fetch"]
    assert all(isinstance(tool, StructuredTool) for tool in parent.tools)

    assert len(parent.subagents) == 1
    child = parent.subagents[0]
    assert isinstance(child, ResolvedSubAgentSpec)
    assert [tool.name for tool in child.tools] == ["summarize"]

    assert parent.response_format == {
        "title": "Summary",
        "type": "object",
        "properties": {"summary": {"type": "string"}},
    }
    assert child.response_format is None


def test_resolve_subagent_specs_passes_inline_skills_through(app_tools: Any) -> None:
    specs = [
        DeepSubAgentSpec(
            name="researcher",
            description="does research",
            system_prompt=TemplatedText(content="research"),
            inline_skills=[InlineSkill(name="demo", content="# demo body")],
        )
    ]
    (resolved,) = asyncio.run(resolve_subagent_specs(specs))
    assert resolved.inline_skills is not None
    assert resolved.inline_skills[0].name == "demo"
    assert resolved.inline_skills[0].content == "# demo body"


def test_deep_subagent_spec_rejects_unknown_key() -> None:
    """The JSON sub-agent shape sets ``extra="forbid"``, so a per-sub key deep cannot
    honor — e.g. a per-sub ``strategy`` — is a loud validation error rather than a
    silently dropped field, matching ``DeepAgentInput``'s own strictness at the run door."""
    with pytest.raises(ValidationError):
        DeepSubAgentSpec.model_validate(
            {"name": "s", "description": "d", "system_prompt": {"content": "p"}, "strategy": "vote"}
        )
    with pytest.raises(ValidationError):
        DeepSubAgentSpec.model_validate(
            {"name": "s", "description": "d", "system_prompt": {"content": "p"}, "totally_unknown_key": 1}
        )


def test_resolve_subagent_specs_empty_returns_empty() -> None:
    assert asyncio.run(resolve_subagent_specs(None)) == []
    assert asyncio.run(resolve_subagent_specs([])) == []


def test_resolve_subagent_specs_rejects_response_format_without_title() -> None:
    specs = [
        DeepSubAgentSpec(
            name="researcher",
            description="does research",
            system_prompt=TemplatedText(content="research"),
            response_format={"type": "object", "properties": {"x": {"type": "string"}}},
        )
    ]
    with pytest.raises(ValueError, match="top-level 'title'"):
        asyncio.run(resolve_subagent_specs(specs))


# ===========================================================================
# _neutral_to_internal / _to_internal
# ===========================================================================


def test_neutral_to_internal_rejects_strategy() -> None:
    spec = NeutralSubAgentSpec(name="s", description="d", system_prompt=TemplatedText(content="p"), strategy="vote")
    with pytest.raises(ValueError, match="strategy"):
        asyncio.run(_neutral_to_internal(spec))


def test_neutral_to_internal_resolves_tools_and_coerces_inline_skills(app_tools: Any) -> None:
    app_tools.client_tools["search"] = _client_tool("search")
    spec = NeutralSubAgentSpec(
        name="s",
        description="d",
        system_prompt=TemplatedText(content="p"),
        tool_names=["search"],
        inline_skills=[{"name": "demo", "content": "# demo"}],
        skills=["/skills/ref/"],
    )
    internal = asyncio.run(_neutral_to_internal(spec))
    assert isinstance(internal, ResolvedSubAgentSpec)
    assert [t.name for t in internal.tools] == ["search"]
    assert internal.skills == ["/skills/ref/"]
    assert internal.inline_skills is not None
    assert isinstance(internal.inline_skills[0], InlineSkill)
    assert internal.inline_skills[0].name == "demo"


def test_to_internal_resolves_json_and_neutral_subagent_specs(app_tools: Any) -> None:
    """``_to_internal`` resolves the JSON ``DeepSubAgentSpec`` (tool NAMES, no live
    tools) the public SSE run door validates, and the programmatic ``NeutralSubAgentSpec``
    — both faces accept both shapes."""
    app_tools.client_tools["search"] = _client_tool("search")
    json_spec = DeepSubAgentSpec(
        name="helper", description="d", system_prompt=TemplatedText(content="p"), tools=["search"]
    )
    resolved = asyncio.run(_to_internal(json_spec))
    assert isinstance(resolved, ResolvedSubAgentSpec)
    assert resolved.name == "helper"
    # The neutral shape still resolves (both faces accept both).
    neutral = NeutralSubAgentSpec(name="native", description="d", system_prompt=TemplatedText(content="p"))
    same = asyncio.run(_to_internal(neutral))
    assert same.name == "native"


def test_subagent_spec_tools_are_delivery_scoped(app_tools: Any) -> None:
    """A SUBAGENT's tool is dispatched inside the parent's turn, so it carries the rule too."""
    probe, seen = probe_tool("calc")
    app_tools.client_tools["calc"] = probe

    resolved = asyncio.run(
        resolve_subagent_specs(
            [DeepSubAgentSpec(name="sub", description="d", system_prompt=TemplatedText(content="s"), tools=["calc"])]
        )
    )
    assert_delivery_scoped(resolved[0].tools[0], seen)
