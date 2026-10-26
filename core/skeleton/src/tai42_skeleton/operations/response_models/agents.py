"""Response models for the agents operations."""

from __future__ import annotations

from pydantic import BaseModel, JsonValue


class AgentView(BaseModel):
    """One registered agent.

    ``input_schema`` is the agent's run-tool input JSON schema
    (``ToolInput.model_json_schema()``), a genuinely-open schema object;
    ``spec_runnable`` is the read authorable-marker, never inferred from a name.
    """

    name: str
    description: str
    tool_name: str
    input_schema: JsonValue
    spec_runnable: bool


class AgentListing(BaseModel):
    """The agent-catalog body (``list_agents`` and the spec-runnable filter share it).

    Every listed agent plus their ``total`` count.
    """

    items: list[AgentView]
    total: int
