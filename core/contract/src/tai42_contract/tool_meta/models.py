"""Record models for the tool-metadata overlay.

The overlay is an UNVERSIONED organizational layer keyed by tool name, separate
from any tool's behavior: a per-tool row (:class:`ToolMetaRecord`) plus a tree of
real folder entities (:class:`FolderRecord`). It applies to ANY tool in the
namespace — native plugin tools, presets, flows alike — never a preset-only
concern. This module holds the SHAPES only; the concrete store that persists and
enforces them (cycle prevention, empty-folder deletes, clean-slate name reclaim)
lives in the skeleton — a contract holds models, never logic.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from tai42_contract.plugins import TAG_RE


class FolderRecord(BaseModel):
    """One folder in the tool-organization tree.

    ``parent_id`` is ``None`` for a root folder and otherwise the ``id`` of the
    containing folder; nesting is endless via the parent chain. Sibling names are
    unique within a parent (root folders share the ``None`` parent), and a move or
    create that would form a cycle is refused by the store. Ids are UUID strings.
    """

    id: str
    name: str
    parent_id: str | None = None


class ToolMetaRecord(BaseModel):
    """The organizational overlay for one tool, keyed by ``tool_name``.

    ``display_name`` overrides the tool's rendered label (``display_name`` when
    set, else the tool name); ``folder_id`` places the tool in a folder (``None``
    = unfiled); ``tags`` is the user's editable categorization, merged with the
    tool's read-only plugin-native tags by the UI. ``hidden`` is TRI-STATE:
    ``None`` = no user opinion (the plugin-declared visibility applies), ``True`` =
    force hidden, ``False`` = force visible (unhides a plugin-hidden tool). Hidden
    is UI visibility only — never a security boundary; the tool stays callable.

    ``badges`` is the operator-overlay half of a tool's INFORMATIONAL capability
    badges (the plugin-declared half rides the live tool's own ``meta``, unioned
    with this set for display). A badge is a free-form label from the same
    charset the plugin tag vocabulary uses (:data:`TAG_RE` — hyphens allowed,
    slashes forbidden). Badges are declarative and advisory only; nothing here
    enforces a capability.
    """

    tool_name: str
    display_name: str | None = None
    folder_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    hidden: bool | None = None
    badges: list[str] = Field(default_factory=list)

    @field_validator("badges")
    @classmethod
    def _check_badges(cls, value: list[str]) -> list[str]:
        for badge in value:
            if not TAG_RE.fullmatch(badge):
                raise ValueError(f"badge {badge!r} must match {TAG_RE.pattern}")
        return value
