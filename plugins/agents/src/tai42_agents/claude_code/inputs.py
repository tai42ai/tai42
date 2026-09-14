"""The tool-face parameter models for ``claude_code``: the run input and its inline
subagent/skill shapes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from tai42_contract.template import TemplatedText


class InlineSkillShape(BaseModel):
    """A skill authored inline on the tool face: a charset-valid name + a ``SKILL.md`` body."""

    model_config = ConfigDict(extra="forbid")
    name: str
    content: str = ""


class SubagentSpecShape(BaseModel):
    """A subagent the caller declares on the tool face — mapped to the SDK AgentDefinition."""

    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    system_prompt: TemplatedText | None = None
    tool_names: list[str] = Field(default_factory=list)


class ClaudeCodeInput(BaseModel):
    """JSON tool-face parameters for ``claude_code``.

    SECURITY INVARIANT: ``thread_id`` is NOT a field — workspace identity must never be
    derivable from unauthenticated caller input. ``thread_id`` arrives ONLY as a trusted
    in-process ``run``/``astream`` kwarg (the conversation bridge), so a tool-face call always
    gets a fresh ephemeral workspace. ``extra="forbid"`` rejects any unknown key loudly.
    """

    model_config = ConfigDict(extra="forbid")

    user_message: TemplatedText
    system_message: TemplatedText | None = None
    tool_names: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    inline_skills: list[InlineSkillShape] = Field(default_factory=list)
    response_format: TemplatedText | dict[str, Any] | None = None
    max_turns: int | None = None
    subagents: list[SubagentSpecShape] = Field(default_factory=list)
