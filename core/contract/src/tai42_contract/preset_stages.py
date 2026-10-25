"""A preset's output stage and the stages a park sits under — the shapes, no logic.

A leaf module: the interactions models carry these shapes, so it imports nothing of the
presets package. :mod:`tai42_contract.presets` re-exports every name.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PresetOutputStage(BaseModel):
    """A preset's resolved output stage: the jq output map then the output JSON Schema, applied to a result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preset: str
    output_map: str | None = None
    output_schema: dict[str, Any] | None = None


class PresetParkStages(BaseModel):
    """The output stages of the preset dispatches a park sits under, by where each one's result reappears.

    ``answer``: applied to the park's answer before the driver's continuation receives it.
    ``terminal``: applied to the resumed drive's terminal result.
    ``chained``: applied to a chained run's terminal before it fires along the routing with that chain key.
    Each list is ordered innermost preset first.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: list[PresetOutputStage] = Field(default_factory=list[PresetOutputStage])
    terminal: list[PresetOutputStage] = Field(default_factory=list[PresetOutputStage])
    chained: dict[str, list[PresetOutputStage]] = Field(default_factory=dict[str, list[PresetOutputStage]])


def preset_output_failed_outcome(preset: str, message: str) -> dict[str, Any]:
    """The opaque FAILED outcome of a resumed run whose preset output stage refused the result.

    Carried on :class:`~tai42_contract.interactions.RunTerminalFailed` and recorded whole; it names
    the preset and the refusal text.
    """
    return {"tai42:preset_output_failed": True, "preset": preset, "message": message}


__all__ = ["PresetOutputStage", "PresetParkStages", "preset_output_failed_outcome"]
