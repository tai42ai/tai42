"""Response models for the tools operations: tool tags, tool schemas and the sync run-tool answer."""

from __future__ import annotations

from pydantic import BaseModel, JsonValue, RootModel
from tai42_contract.interactions import RunAnswer


class ToolTagRow(BaseModel):
    """One tool's declared tags, visibility, and capability badges.

    ``hidden`` and ``badges`` are the tool's OWN declaration (before the tool_meta overlay's override).
    ``schedulable`` is whether the tool's schedule vehicle is registered (the scheduling backend's
    ``schedule_task`` extension is attached to it).
    """

    name: str
    tags: list[str]
    hidden: bool
    badges: list[str]
    schedulable: bool


class ToolTagListResponse(RootModel[list[ToolTagRow]]):
    """The bare array of per-tool tag rows."""


class ToolSchemaView(BaseModel):
    """One tool's input/output JSON schemas and description.

    ``input``/``output`` are arbitrary JSON-schema objects; ``output`` and ``description`` are null when the tool
    declares none.
    """

    input: dict[str, JsonValue]
    output: dict[str, JsonValue] | None
    description: str | None


class ToolsSchemaMap(RootModel[dict[str, ToolSchemaView]]):
    """Every tool's schema, keyed by tool name."""


class RunAnswerResponse(RootModel[RunAnswer]):
    """The sync run-tool body: the run answer, discriminated on ``kind``."""
