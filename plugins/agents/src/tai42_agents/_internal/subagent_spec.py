"""The in-process sub-agent descriptor the live-tools faces accept."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from tai42_contract.agent import PresetSpec
from tai42_contract.template import TemplatedText
from tai42_contract.tools import ClientTool


class SubAgentSpec(BaseModel):
    """Neutral sub-agent descriptor — the in-process, live-tools shape.

    Carries name/description/system_prompt/tool_names/tools(live)/presets/skills/
    inline_skills/response_format/strategy/subagents — the fields a live sub-agent
    needs. ``inline_skills`` items are plain dicts (``{"name", "content"}``). JSON
    callers use the richer ``DeepSubAgentSpec`` (full field set) instead of this type.

    ``tools`` are live :class:`~tai42_contract.tools.ClientTool` handles.

    ``response_format`` is a live pydantic class (in-process), the ``TemplatedText | dict``
    authored-schema union (a JSON caller's forced-output schema — inline, or named by stored
    ``id`` and rendered/parsed to its schema at the point of use), or unset.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str
    description: str = ""
    system_prompt: TemplatedText | None = None
    tool_names: list[str] = []
    tools: list[ClientTool] = []
    presets: list[PresetSpec] = []
    skills: list[str] = []
    inline_skills: list[dict[str, Any]] = []
    response_format: type[BaseModel] | TemplatedText | dict[str, Any] | None = None
    strategy: str | None = None
    subagents: list[SubAgentSpec] = []
