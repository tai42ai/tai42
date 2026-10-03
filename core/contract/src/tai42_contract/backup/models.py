"""Vendor-neutral data shapes for the backup contract.

Export PAYLOADS stay shape-agnostic (``Any``) — their concrete shape is a
host-side product concern, and this contract must stay vendor-free. A section's
import REPORT is typed: :class:`BackupSectionReport` is the one shape every
importer returns and the one the platform reads, so a restore's outcome never
travels as an opaque object the platform digs keys out of. The section
descriptor listed for the UI is :class:`BackupSectionInfo`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tai42_contract.app.responses import FanoutSummary


class BackupSectionInfo(BaseModel):
    """Descriptor for one registered backup section, returned by ``AppBackup.sections`` for the UI to render.

    ``name`` is the section's unique registration key. ``secret`` marks a
    section whose exported payload carries credentials/secrets, so a caller can
    warn on or gate its handling.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    secret: bool


class BackupSectionReport(BaseModel):
    """One section's import outcome — the typed result every importer returns.

    The platform reads ONLY these fields: ``created``/``updated``/``skipped`` are
    the per-record counts (``skipped`` is the per-record REJECTION count, each also
    carried in ``errors``), ``errors`` is the per-record failure messages, and
    ``fanout`` is the platform's own per-worker report for a section whose restore
    broadcasts a fleet eviction (``None`` for every section that does not).

    ``details`` is an OPEN map for an importer's OWN counts and data — a
    per-entity breakdown, a ``skipped_existing`` tally, minted credentials to
    surface once — so a backend declares its own shape there without the platform
    knowing it. The typed surface is CLOSED (``extra="forbid"``): an importer that
    returns a stray top-level field is a wrong shape the registry refuses loudly
    rather than silently dropping. ``validate_assignment`` coerces ``fanout`` on
    assignment, so a mutation building the report cannot leave an unvalidated shape.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    fanout: FanoutSummary | None = None
