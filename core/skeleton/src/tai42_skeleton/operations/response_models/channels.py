"""Response models for the channels operations."""

from __future__ import annotations

from pydantic import BaseModel


class ChannelEntry(BaseModel):
    """One registered channel: its registry name and the hint for a route's ``our_identity``."""

    name: str
    identity_hint: str | None


class ChannelListing(BaseModel):
    """The registered channels, sorted by name."""

    channels: list[ChannelEntry]
