"""Response models for the presets operations.

Cover ``/api/presets*``: the preset record rows and detail, the create / rename / delete results,
the version save / rollback / tag results and the draft validation verdict. Existing record
models (``DocumentVersion``) are imported and extended, never redefined here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, RootModel
from tai42_contract.app.responses import FanoutSummary
from tai42_contract.template import TemplatedText
from tai42_contract.versioning.models import DocumentVersion


class PresetRecordView(BaseModel):
    """A preset's record row.

    ``extensions`` is the ordered extension combos; ``conflicted`` marks a quarantined record with its
    ``conflicted_reason`` (``null`` when clean); ``uses`` / ``used_by`` are the sorted other presets this
    body composes and that compose it. ``input_map`` / ``output_map`` are the stored jq programs (inline
    text or a stored-body reference), ``null`` when unset.
    """

    name: str
    base_tool: str
    description: str
    active_version: int
    extensions: list[list[str | dict[str, Any]]]
    output_schema: TemplatedText | dict[str, Any] | None
    input_schema: TemplatedText | dict[str, Any] | None
    input_map: TemplatedText | str | None
    output_map: TemplatedText | str | None
    conflicted: bool
    conflicted_reason: str | None
    uses: list[str]
    used_by: list[str]


class PresetRecordList(RootModel[list[PresetRecordView]]):
    """One row per store-backed preset record, including conflicted rows."""


class PresetDetailView(PresetRecordView):
    """A single preset's record plus its active ``fixed_kwargs``."""

    fixed_kwargs: dict[str, Any]


class PresetCreateResult(PresetRecordView):
    """A created preset's record with the per-worker rebind fan-out report."""

    fanout: FanoutSummary


class PresetVersionSaveResult(DocumentVersion):
    """A newly saved preset version row with the per-worker rebind fan-out report (fields merged, not nested)."""

    fanout: FanoutSummary


class PresetRollbackResult(BaseModel):
    """The version a preset was rolled back to, with the rebind fan-out report."""

    name: str
    active_version: int
    fanout: FanoutSummary


class PresetRenameResult(BaseModel):
    """A renamed preset.

    ``name`` is the new name and ``renamed_from`` the old one; the fan-out report is the new binding's
    propagation.
    """

    name: str
    renamed_from: str
    active_version: int
    fanout: FanoutSummary


class PresetDeleteResult(BaseModel):
    """A deleted preset with the per-worker removal fan-out report."""

    name: str
    deleted: bool
    fanout: FanoutSummary


class PresetValidateVerdict(BaseModel):
    """A preset draft validation verdict — ``valid`` is the absence of an ``error`` (both outcomes answer 200)."""

    valid: bool
    error: str | None


class PresetVersionTagsResult(BaseModel):
    """A preset version's replaced tag annotation."""

    name: str
    version: int
    tags: list[str]
