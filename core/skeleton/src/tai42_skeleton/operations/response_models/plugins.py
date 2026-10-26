"""Response models for the plugins operations."""

from __future__ import annotations

from pydantic import Field, RootModel

from tai42_skeleton.plugins.registry import StudioPluginManifest


class StudioPluginListingEntry(StudioPluginManifest):
    """One installed plugin's parsed, validated ``StudioPluginManifest`` plus where its own routes are served."""

    mounts: dict[str, str] = Field(
        description='The plugin\'s own route items and their resolved absolute bases, `{item: "/api/<base>"}`.'
    )


class StudioPluginListing(RootModel[list[StudioPluginListingEntry]]):
    """The bare-list body of ``list_studio_plugins``: one entry per installed plugin."""
