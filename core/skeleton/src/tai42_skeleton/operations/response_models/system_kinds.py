"""Response models for the system-kinds operations."""

from __future__ import annotations

from pydantic import RootModel

from tai42_skeleton.app.kind_status import KindStatus


class SystemKindsListing(RootModel[list[KindStatus]]):
    """The bare-list body of ``list_system_kinds`` — one ``KindStatus`` per pluggable kind, reused as-is."""
