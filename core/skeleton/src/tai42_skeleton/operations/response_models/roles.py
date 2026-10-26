"""Response models for the roles operations: role delete and role version history."""

from __future__ import annotations

from pydantic import BaseModel, JsonValue
from tai42_contract.versioning import DocumentVersion


class RoleDeleted(BaseModel):
    """A role delete confirmation."""

    name: str
    deleted: bool


class RoleVersionsView(BaseModel):
    """A role's append-only version history plus its who/when/before-after audit trail.

    ``audit`` rows are open audit-event metadata (secret-fenced, kept opaque).
    """

    versions: list[DocumentVersion]
    audit: list[dict[str, JsonValue]]
