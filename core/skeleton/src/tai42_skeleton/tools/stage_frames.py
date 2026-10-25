"""The preset stage frames a park captures, and the stages a platform drive binds.

A preset dispatch pushes its frame for the duration of its forward. A park raised beneath it sorts
the frames it sits under by where each preset's result reappears after the resume: the answer the
driver's continuation receives, the resumed drive's terminal, a chained run's terminal fired along
its routing, or a nested run's result its parent's driver adopts.

The frames live in a context variable holding an immutable tuple, so concurrent sibling calls each
see only their own ancestors' frames. The outermost dispatch of a run opens an empty stack and
records the door's run face (:func:`run_stage_scope`); a platform drive of a park binds the stages
the park captured and opens an empty stack of its own (:func:`drive_output_stages`).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from tai42_contract.interactions import RunFailed, chained_lineage, current_run_face, get_chained_resume
from tai42_contract.presets import PresetOutputStage, PresetParkStages, preset_output_failed_outcome
from tai42_kit.presets import PresetOutputError, run_output_stages


@dataclass(eq=False)
class StageFrame:
    """One preset dispatch in flight: its stage and where it sits, read when the dispatch started.

    ``face`` is the run face the dispatch ran under, ``routing_key`` the chain routing bound around
    it, ``lineage_len`` how many chained calls it had crossed. ``adopted`` is set by the ask a park
    beneath it classifies as a nested run its parent's driver adopts.
    """

    stage: PresetOutputStage
    face: object
    routing_key: str | None
    lineage_len: int
    adopted: bool = False


# An immutable tuple per context: sibling tasks each see exactly their own ancestors' frames.
_STAGE_FRAMES: ContextVar[tuple[StageFrame, ...]] = ContextVar("tai42_preset_stage_frames", default=())


# The run face of the door the current run entered through; ``None`` inside a platform drive,
# whose door stages are carried by the drive's own stages.
_DOOR_FACE: ContextVar[object | None] = ContextVar("tai42_preset_door_face", default=None)
# The stages of the park a platform drive is driving; ``None`` outside a drive.
_DRIVE_STAGES: ContextVar[PresetParkStages | None] = ContextVar("tai42_preset_drive_stages", default=None)


@contextmanager
def run_stage_scope() -> Iterator[None]:
    """Open a run's own empty frame stack and record its door face, for the run's outermost dispatch."""
    frames_token = _STAGE_FRAMES.set(())
    face_token = _DOOR_FACE.set(current_run_face())
    try:
        yield
    finally:
        _DOOR_FACE.reset(face_token)
        _STAGE_FRAMES.reset(frames_token)


@contextmanager
def drive_output_stages(stages: PresetParkStages | None) -> Iterator[None]:
    """Bind ``stages``, the stages of the park being driven, around the drive, with an empty frame stack."""
    stages_token = _DRIVE_STAGES.set(stages)
    frames_token = _STAGE_FRAMES.set(())
    face_token = _DOOR_FACE.set(None)
    try:
        yield
    finally:
        _DOOR_FACE.reset(face_token)
        _STAGE_FRAMES.reset(frames_token)
        _DRIVE_STAGES.reset(stages_token)


def current_drive_output_stages() -> PresetParkStages | None:
    """The stages of the park the current platform drive is driving, or ``None`` outside a drive."""
    return _DRIVE_STAGES.get()


@contextmanager
def stage_frame(stage: PresetOutputStage) -> Iterator[StageFrame]:
    """Push ``stage``'s frame for the duration of the preset's forward and yield it."""
    routing = get_chained_resume()
    frame = StageFrame(
        stage=stage,
        face=current_run_face(),
        routing_key=routing.chain_key if routing is not None else None,
        lineage_len=len(chained_lineage()),
    )
    token = _STAGE_FRAMES.set((*_STAGE_FRAMES.get(), frame))
    try:
        yield frame
    finally:
        _STAGE_FRAMES.reset(token)


def capture_park_stages(chain_keys: Sequence[str]) -> PresetParkStages | None:
    """The stages a park raised here records, sorted by where each preset's result reappears.

    Each frame of the current stack, innermost first, is an ``answer`` stage when it ran under the
    asking run's face, a ``terminal`` stage when it ran under the door's face, a ``chained`` stage
    (under its routing's key) when a driver crossed that routing below it, and is otherwise marked
    adopted: its parent's driver adopts the nested run's park, and the frame files its stage on the
    returned park object. Inside a drive the park also keeps the drive's terminal stages and the
    drive's chained stages whose keys are in ``chain_keys``, the chained calls the park records;
    the drive's answer stages belonged to the answered ask. ``None`` when nothing is recorded.
    """
    ask_face = current_run_face()
    lineage = chained_lineage()
    door_face = _DOOR_FACE.get()
    answer: list[PresetOutputStage] = []
    terminal: list[PresetOutputStage] = []
    chained: dict[str, list[PresetOutputStage]] = {}
    for frame in reversed(_STAGE_FRAMES.get()):
        if frame.face is ask_face:
            answer.append(frame.stage)
        elif door_face is not None and frame.face is door_face:
            terminal.append(frame.stage)
        elif frame.routing_key is not None and frame.routing_key in lineage[frame.lineage_len :]:
            chained.setdefault(frame.routing_key, []).append(frame.stage)
        else:
            frame.adopted = True
    drive = _DRIVE_STAGES.get()
    if drive is not None:
        terminal.extend(drive.terminal)
        for key, stages in drive.chained.items():
            if key in chain_keys:
                chained.setdefault(key, []).extend(stages)
    if not (answer or terminal or chained):
        return None
    return PresetParkStages(answer=answer, terminal=terminal, chained=chained)


async def settle_chained_terminal(chain_key: str, result: Any) -> Any:
    """``result`` with the current drive's chained stages for ``chain_key`` applied.

    A ``RunFailed`` passes through; a refusal settles to a ``RunFailed`` naming the preset.
    """
    drive = _DRIVE_STAGES.get()
    stages = drive.chained.get(chain_key, []) if drive is not None else []
    failure: dict[str, Any] | None = None
    try:
        return await run_output_stages(stages, result)
    except PresetOutputError as exc:
        failure = preset_output_failed_outcome(exc.preset, str(exc))
    return RunFailed(outcome=failure)
