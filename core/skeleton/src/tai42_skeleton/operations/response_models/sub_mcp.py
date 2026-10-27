"""Response models for the sub-MCP operations: the listing, registration and removal results."""

from __future__ import annotations

from pydantic import BaseModel, RootModel


class SubMcpListingEntry(BaseModel):
    """One registered sub-MCP app: its durable ``tools`` + ``transport`` and its served mount.

    ``mount_url`` is the URL the app is served at (its route-table key) and ``access_pattern``
    the dynamic route pattern that maps every sub-path of the mount to that key, so a client
    maps the mount without rebuilding either.
    """

    tools: list[str]
    transport: str
    mount_url: str
    access_pattern: str


class SubMcpMapListing(RootModel[dict[str, SubMcpListingEntry]]):
    """The registered sub-MCP apps as a dynamic slug-keyed MAP (not an ``items`` list)."""


class SubMcpRegistrationResult(BaseModel):
    """The register/reload result: the ``slug`` mounted and the ``tools`` it exposes on ``transport``."""

    slug: str
    tools: list[str]
    transport: str


class SubMcpRemovalResult(BaseModel):
    """The unregister result: the ``slug`` removed and the ``removed`` flag.

    Always true — a slug present nowhere is a 404 instead.
    """

    slug: str
    removed: bool
