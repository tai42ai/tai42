"""Response models for the extensions operations: one extension's catalog row and the catalog listing."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, RootModel


class ExtensionView(BaseModel):
    """One registered extension: ``name``, the lowercase ``kind`` value, ``multiple`` and ``config_schema``.

    ``multiple`` says whether one tool may stack several extensions of that kind.
    """

    name: str
    kind: str
    multiple: bool
    config_schema: dict[str, Any] | None = Field(
        description="the JSON Schema of the settings this extension binds as ``config``; ``null`` when it takes none"
    )


class ExtensionListing(RootModel[list[ExtensionView]]):
    """The bare-list body of ``list_extensions`` — one entry per extension."""
