"""The request DTOs for the preset doors — the emitted spec's requestBody schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from tai42_contract.manifest import ExtensionElement
from tai42_contract.states.binding import StateBinding
from tai42_contract.template import TemplatedText


class PresetCreate(BaseModel):
    """A preset-creation request.

    ``description`` is the bound tool's LLM-facing docstring — REQUIRED non-empty on every
    create. ``extensions`` is the list of extension combos (each element an extension name or a
    ``{"name", "config"}`` mapping binding author config). ``output_schema`` is the optional
    author-set OUTPUT JSON Schema (an object schema).
    """

    name: str
    base_tool: str
    description: str
    fixed_kwargs: dict[str, Any] = {}
    extensions: list[list[ExtensionElement]] = []
    output_schema: TemplatedText | dict[str, Any] | None = None
    input_schema: TemplatedText | dict[str, Any] | None = None
    state_binding: StateBinding | None = None


class PresetVersionSave(BaseModel):
    """A new-preset-version request.

    At least one field must be present; an omitted field carries forward, an explicit ``[]``
    clears (the store sentinel rule). ``output_schema`` carries forward when omitted, clears on
    an explicit ``null``, and wins on an explicit object schema; ``input_schema`` follows the
    SAME carry-forward rule (omitted carries, ``null`` clears, an object schema wins).
    ``description`` carries forward when omitted and is SET by an explicit non-empty string (an
    explicit ``""`` is rejected — the resulting description is validated non-empty on every save).
    """

    fixed_kwargs: dict[str, Any] | None = None
    extensions: list[list[ExtensionElement]] | None = None
    output_schema: TemplatedText | dict[str, Any] | None = None
    input_schema: TemplatedText | dict[str, Any] | None = None
    description: str | None = None
    #: Omitted carries the active binding forward; an explicit ``null`` clears it; a
    #: binding sets it — the presence flag (``state_binding_provided``) tells absent from
    #: an explicit ``null``, mirroring ``input_schema``.
    state_binding: StateBinding | None = None


class PresetRollback(BaseModel):
    """A rollback request — the target version to make active."""

    version: int


class PresetRename(BaseModel):
    """A rename request — the new preset (tool) name."""

    new_name: str


class PresetValidate(BaseModel):
    """A preset validation (dry-run) request — the full create field set.

    When a preset named ``name`` already exists the door validates a NEW VERSION: then
    ``base_tool`` / ``description`` carry forward from the active body (a provided value that
    differs is rejected) and any absent field merges from it, exactly as the save-version route
    merges.
    """

    name: str
    base_tool: str | None = None
    description: str | None = None
    fixed_kwargs: dict[str, Any] | None = None
    extensions: list[list[ExtensionElement]] | None = None
    output_schema: TemplatedText | dict[str, Any] | None = None
    input_schema: TemplatedText | dict[str, Any] | None = None
    state_binding: StateBinding | None = None


class PresetVersionTags(BaseModel):
    """Replace a preset version's ``tags`` annotation.

    Labels on an immutable version body — no rebind.
    """

    tags: list[str]
