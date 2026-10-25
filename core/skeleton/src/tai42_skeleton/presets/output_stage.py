"""A preset's output stage applied in its live transform.

A preset dispatch that returns runs its output stage on the result here. A preset dispatch whose
base parks never sees its real result: its frame (:mod:`tai42_skeleton.tools.stage_frames`) is
captured when the park is raised, and the stage is applied where that result reappears after the
resume.
"""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools.base import ToolResult
from tai42_contract.interactions import ResumeBuffered, SuspendedInteraction
from tai42_contract.presets import PresetOutputStage
from tai42_kit.interactions.park_marker import read_resume_buffered_marker, read_suspended_interaction_marker
from tai42_kit.presets import PresetOutputError, run_output_stage

from tai42_skeleton.tools.binding.result import _tool_result_value
from tai42_skeleton.tools.reveal_gate import (
    inprocess_reveal_gate,
    secret_was_revealed,
    stowed_park,
    stowed_reveal_payload,
)
from tai42_skeleton.tools.stage_frames import StageFrame


def _is_park(result: Any) -> bool:
    """Whether a preset's forwarded ``result`` is a park signal rather than the tool's output.

    In process the dispatch's reveal gate is armed: it stows the park object (a
    ``SuspendedInteraction`` or a ``ResumeBuffered``) and ``result`` is only its flattened
    projection, so the park is read off the gate by TYPE alone, and a result that merely looks
    like a marker is the tool's output. On the MCP edge no gate is armed and the park rides
    ``result.structured_content`` as its reserved marker. A park passes through unvalidated here;
    the preset's output stage is captured when the park is raised and applied to the result that
    reappears after the resume: the answer, the run's terminal, a chained run's terminal, or a
    nested run's cascade.
    """
    if inprocess_reveal_gate.get() is not None:
        has_park, park = stowed_park()
        return has_park and isinstance(park, (SuspendedInteraction, ResumeBuffered))
    structured = getattr(result, "structured_content", None)
    return (
        read_suspended_interaction_marker(structured) is not None or read_resume_buffered_marker(structured) is not None
    )


def _file_adopted_stage(stage: PresetOutputStage) -> None:
    """Append ``stage`` to the in-process park object the dispatch stowed, for the driver that adopts it."""
    if inprocess_reveal_gate.get() is None:
        return
    has_park, park = stowed_park()
    if has_park and isinstance(park, SuspendedInteraction):
        park.output_stages.append(stage)


async def apply_output_stage_live(frame: StageFrame, result: ToolResult) -> ToolResult:
    """Run ``frame``'s stage over a preset's forwarded ``result`` and return what the preset answers.

    A park and an error result pass through untouched; a park raised under an adopted frame carries
    the frame's stage on the in-process park object (the MCP marker never carries stages). Under
    the armed in-process gate a stowed secret-bearing payload is judged as it is (a map over it is
    refused) and ``result`` is returned. Otherwise the stage runs on the value an in-process
    caller receives; with an output map the mapped value is returned as a new result (an object
    as structured content, any other JSON value in the wrap form). A refusal raises
    :class:`~fastmcp.exceptions.ToolError` carrying the stage's text with nothing chained.
    """
    stage = frame.stage
    if _is_park(result):
        if frame.adopted:
            _file_adopted_stage(stage)
        return result
    if result.is_error:
        return result
    has_payload, payload = stowed_reveal_payload()
    text: str | None = None
    mapped: Any = None
    try:
        if has_payload:
            await run_output_stage(stage, payload)
        else:
            mapped = await run_output_stage(stage, _tool_result_value(result), carries_secret=secret_was_revealed())
    except PresetOutputError as exc:
        text = str(exc)
    if text is not None:
        raise ToolError(text)
    if has_payload or stage.output_map is None:
        return result
    if isinstance(mapped, dict):
        return ToolResult(structured_content=mapped)
    return ToolResult(content=mapped, structured_content={"result": mapped}, meta={"fastmcp": {"wrap_result": True}})
