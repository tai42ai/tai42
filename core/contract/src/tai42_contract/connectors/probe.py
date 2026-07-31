"""Result models for pull-based reachability checks of a managed sub-service's
MCP server.

``ToolSummary`` / ``VerifyResult`` are the verbose outcome of a verification:
the served tool list on success, or the specific failure reason otherwise.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToolSummary(BaseModel):
    """One tool the verified MCP server lists — enough for agent/user review."""

    name: str
    description: str = ""


class VerifyResult(BaseModel):
    """Outcome of a verbose verification: the served tools, or the reason not."""

    ok: bool
    tools: list[ToolSummary] = Field(default_factory=list[ToolSummary])
    error: str | None = None
