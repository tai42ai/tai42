"""JSON-facing subagent spec for the ``langchain_deep_agent`` tool.

:class:`DeepSubAgentSpec` is the JSON authoring shape mirroring the core
:class:`~tai42_agents.langchain_deep_agent.spec.ResolvedSubAgentSpec` (whose live
``StructuredTool`` field cannot cross the tool boundary): ``tools`` are tool names
resolved through ``tai42_app.tools.get_client_tools``, and ``subagents`` nests one
level deep. ``response_format`` is a JSON Schema dict passed through unchanged.
``resolve_subagent_specs`` resolves these into core ``ResolvedSubAgentSpec`` objects.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from tai42_contract.agent.base import SubAgentSpec as NeutralSubAgentSpec
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText

from tai42_agents._internal.nested_dispatch import scope_nested_dispatch_all
from tai42_agents._internal.reject import resolve_response_format
from tai42_agents._internal.resolve_tools import resolve_tools
from tai42_agents.langchain_deep_agent.spec import InlineSkill, ResolvedSubAgentSpec


class DeepSubAgentSpec(BaseModel):
    """A subagent the main deep agent can invoke via its ``task`` tool.

    The JSON-expressible mirror of :class:`ResolvedSubAgentSpec`: ``tools`` are
    client tool names resolved at call time, ``subagents`` nests one level deep.
    ``llm_provider`` / ``llm_kwargs`` are optional (inheriting the main agent's model
    when omitted); ``tools`` inherits the parent's tools when empty.

    ``extra="forbid"`` rejects any unknown key loudly at validation, so a per-sub
    ``strategy`` (which the deepagents sub-agent path cannot honor) is rejected here
    rather than vanishing.

    ``base_url``/``api_key`` in ``llm_kwargs`` route to a caller-chosen model
    endpoint, so expose any agent carrying these kwargs only to trusted callers — an
    injected parent could redirect the model call and leak the key/context.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Identifier the main agent uses to call this subagent.")
    description: str = Field(description="When to use this subagent; read by the main agent to route to it.")
    system_prompt: TemplatedText = Field(
        description="The subagent's own system instructions: inline content or a stored id, plus render kwargs."
    )

    llm_provider: str | None = Field(
        default=None,
        description="LLM provider for this subagent; inherits the main agent's model when None.",
    )
    llm_kwargs: dict[str, Any] | None = Field(
        default=None,
        description="Provider kwargs merged over llm_settings() defaults when llm_provider is set.",
    )
    tools: list[str] = Field(
        default_factory=list,
        description="Client tool names available to this subagent; inherits the parent's tools when empty.",
    )
    skills: list[str] | None = Field(
        default=None,
        description="Skill source paths (under SKILLS_ROOT) loaded only for this subagent.",
    )
    inline_skills: list[InlineSkill] | None = Field(
        default=None,
        description="Skills supplied inline for this subagent: each has a name and SKILL.md "
        "content. Mounted under SKILLS_ROOT and auto-loaded — no template provider "
        "needed, and no need to also list them in skills.",
    )
    interrupt_on: dict[str, Any] | None = Field(
        default=None,
        description="Per-tool human-in-the-loop interrupt config; inherits the main agent's when None.",
    )
    response_format: TemplatedText | dict[str, Any] | None = Field(
        default=None,
        description="JSON Schema for this subagent's forced structured output; free-form text when None.",
    )
    subagents: list[DeepSubAgentSpec] = Field(
        default_factory=list,
        description="Nested subagents this subagent can invoke via its own task tool. "
        "One level only: a nested subagent declaring subagents of its own "
        "is rejected at build time.",
    )


async def _resolve_subagent_spec(spec: DeepSubAgentSpec) -> ResolvedSubAgentSpec:
    """Convert one :class:`DeepSubAgentSpec` into a core :class:`ResolvedSubAgentSpec`.

    Tool names are resolved to ``StructuredTool`` instances and nested subagents
    are resolved recursively. ``response_format`` (a JSON Schema dict) passes
    through to the core spec unchanged.
    """
    resolved_response_format = await resolve_response_format(f"subagent {spec.name!r}", spec.response_format)
    # Delivery-scoped: a subagent's tool is dispatched INSIDE the parent's turn, so it must
    # not capture the completion binding addressing the parent's own deferred answer.
    tools = scope_nested_dispatch_all(await tai42_app.tools.get_client_tools(spec.tools) if spec.tools else [])
    subagents = [await _resolve_subagent_spec(child) for child in spec.subagents]
    return ResolvedSubAgentSpec(
        name=spec.name,
        description=spec.description,
        system_prompt=spec.system_prompt,
        llm_provider=spec.llm_provider,
        llm_kwargs=spec.llm_kwargs,
        tools=tools,
        skills=spec.skills,
        inline_skills=spec.inline_skills,
        interrupt_on=spec.interrupt_on,
        response_format=resolved_response_format,
        subagents=subagents,
    )


async def resolve_subagent_specs(specs: list[DeepSubAgentSpec] | None) -> list[ResolvedSubAgentSpec]:
    """Resolve a list of JSON :class:`DeepSubAgentSpec` into core ``ResolvedSubAgentSpec`` objects."""
    if not specs:
        return []
    return [await _resolve_subagent_spec(spec) for spec in specs]


async def _to_internal(spec: NeutralSubAgentSpec | DeepSubAgentSpec) -> ResolvedSubAgentSpec:
    """Resolve either subagent shape into the core spec.

    A programmatic caller passes :class:`NeutralSubAgentSpec` (live tools), the JSON
    door :class:`DeepSubAgentSpec` (tool names); both faces accept both.
    """
    if isinstance(spec, DeepSubAgentSpec):
        return (await resolve_subagent_specs([spec]))[0]
    return await _neutral_to_internal(spec)


async def _neutral_to_internal(spec: NeutralSubAgentSpec) -> ResolvedSubAgentSpec:
    """Map a neutral (live-tools) sub-agent spec to the internal deepagents spec.

    Resolves the spec's ``tools`` / ``tool_names`` / ``presets`` into a flat
    ``StructuredTool`` list. The internal spec has no ``strategy`` field, so a
    neutral ``strategy`` is rejected rather than dropped silently.
    """
    if spec.strategy is not None:
        raise ValueError(
            f"sub-agent {spec.name!r} sets strategy={spec.strategy!r}, which the "
            "deepagents sub-agent spec cannot carry; pass response_format as a "
            "ToolStrategy on the parent instead."
        )
    if spec.system_prompt is None:
        raise ValueError(
            f"sub-agent {spec.name!r} has no system_prompt; the deepagents sub-agent spec "
            "requires one, so a neutral spec that omits it is refused here rather than run "
            "with empty instructions."
        )
    tools = await resolve_tools(tai42_app.tools, list(spec.tool_names), list(spec.tools), list(spec.presets))
    subagents = [await _neutral_to_internal(child) for child in spec.subagents]
    # Neutral inline_skills are plain dicts; coerce to InlineSkill for the factory.
    resolved_response_format = await resolve_response_format(f"subagent {spec.name!r}", spec.response_format)
    inline_skills = [s if isinstance(s, InlineSkill) else InlineSkill(**s) for s in (spec.inline_skills or [])]
    return ResolvedSubAgentSpec(
        name=spec.name,
        description=spec.description,
        system_prompt=spec.system_prompt,
        tools=tools,
        skills=list(spec.skills) or None,
        inline_skills=inline_skills or None,
        response_format=resolved_response_format,
        subagents=subagents,
    )
