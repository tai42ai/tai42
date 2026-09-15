"""The ``langchain_deep_agent`` JSON run-door contract.

:class:`DeepAgentInput` is the tool-face parameter schema, and
:data:`_UNHONORED_REASONS` / :data:`_UNHONORED_COLLECTION_PARAMS` name the two ABC
``run``/``astream`` parameters this agent cannot honor, mapped to the reason each
rejection raises.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from tai42_contract.template import TemplatedText

from tai42_agents.langchain_deep_agent.spec import InlineSkill
from tai42_agents.langchain_deep_agent.tool_spec import DeepSubAgentSpec

# The two ABC ``run``/``astream`` parameters ``langchain_deep_agent`` cannot honor on the
# main agent, mapped to the reason named in the raised error (the keys define this
# agent's unhonored set). ``presets`` is truthiness-checked, ``strategy`` set when
# not ``None``.
_UNHONORED_REASONS: dict[str, str] = {
    "presets": (
        "its tool set is composed from tool_names and live tools on the main agent, not presets, "
        "and it will not silently ignore one"
    ),
    "strategy": "the deepagents runtime applies no composition strategy and will not silently ignore one",
    "system_content_kwargs": (
        "its system prompt is handed to the deepagents factory, never built as a content block through "
        "build_system_message, so it cannot carry content-block keys; use user_content_kwargs instead"
    ),
    "resume_checkpoint_id": (
        "the durable sandbox WORKSPACE volume cannot be forked alongside the LangGraph checkpoint, so "
        "forking the checkpoint past an aborted turn would run a forked graph over post-abort workspace "
        "state — a silent divergence; it is unhonored on the durable deep agent"
    ),
}
_UNHONORED_COLLECTION_PARAMS: frozenset[str] = frozenset({"presets"})


class DeepAgentInput(BaseModel):
    """JSON tool-face parameters for ``langchain_deep_agent``.

    Live ``tools=`` are absent from this JSON schema (a live ``StructuredTool`` is not
    JSON-serializable), but both in-process faces — :meth:`DeepAgent.run` and
    :meth:`DeepAgent.astream` — accept them directly.

    The schema advertises exactly the composable fields ``langchain_deep_agent``'s runtime
    honors — ``subagents``, ``skills``, ``inline_skills``, ``interrupt_on``,
    ``response_format`` alongside the ``tool_names`` / message / provider plumbing.
    It carries no ``strategy`` field: the deepagents runtime has no composition
    strategy to apply (its sub-agent path rejects a per-sub ``strategy`` outright),
    so advertising one would be a schema lie. ``extra="forbid"`` rejects any
    unknown key loudly at validation rather than letting a typo at the run door
    vanish silently.

    ``base_url``/``api_key`` in ``llm_kwargs`` legitimately route to a caller-chosen
    model endpoint; expose any agent or tool carrying these kwargs only to trusted
    callers — an injected parent agent could redirect the model call to a hostile
    endpoint and leak the key/context.
    """

    model_config = ConfigDict(extra="forbid")

    tool_names: list[str] = Field(default_factory=list, description="Client tool names to load.")
    subagents: list[DeepSubAgentSpec] | None = Field(
        default=None, description="Subagents the main agent can invoke via its task tool."
    )
    skills: list[str] | None = Field(default=None, description="Skill source paths under SKILLS_ROOT.")
    inline_skills: list[InlineSkill] | None = Field(
        default=None, description="Skills supplied inline (name + SKILL.md content)."
    )
    system_message: TemplatedText | None = None
    user_message: TemplatedText | None = None
    interrupt_on: dict[str, Any] | None = None
    response_format: TemplatedText | dict[str, Any] | None = Field(
        default=None, description="JSON Schema of the forced structured output (needs a top-level 'title')."
    )
    user_content_kwargs: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Content-block keys merged into the user message's text block (e.g. cache_control "
            "for Anthropic prompt caching). Provider-unknown keys surface as loud provider errors. "
            "On a checkpointed thread the model call keeps only the newest mark (older marks are "
            "stripped), so per-turn marking stays within the provider's breakpoint cap (Anthropic: 4)."
        ),
    )
    llm_provider: str | None = None
    checkpoint_provider: str | None = None
    store_provider: str | None = None
    llm_kwargs: dict[str, Any] | None = None
    langgraph_config: dict[str, Any] | None = None

    @field_validator("user_content_kwargs")
    @classmethod
    def _empty_content_kwargs_is_unset(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Normalize an empty ``user_content_kwargs`` dict to ``None`` so it reads as unset.

        An empty dict carries no content-block keys, matching the builders that treat {} as no mark.
        """
        return value or None
