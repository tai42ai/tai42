"""The stage frames a park sits under, sorted at the ask by where each preset's result reappears.

Each preset dispatch with an output stage pushes a frame for the duration of its forward. The ask
that parks reads the stack: a frame of the asking run is an answer stage, a frame at the door is a
terminal stage, a frame below which a driver crossed the frame's routing is a chained stage, and
any other frame is adopted by its parent's driver and stores nothing. Inside a drive the park
inherits the drive's terminal stages and the chained stages whose keys it is still nested under.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from tai42_contract.interactions import (
    ChainedResume,
    open_nested_run_face,
    reset_chained_resume,
    reset_resume_continuation_tool,
    set_chained_resume,
    set_resume_continuation_tool,
)
from tai42_contract.presets import PresetOutputStage, PresetParkStages
from tai42_contract.tools import TOOL_META_PAUSES

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.binding.surface import remove_surface_tool
from tai42_skeleton.tools.stage_frames import (
    capture_park_stages,
    drive_output_stages,
    run_stage_scope,
    stage_frame,
)

KEY = "tai42:chained-park:k"
OTHER = "tai42:chained-park:other"


def _stage(preset: str) -> PresetOutputStage:
    return PresetOutputStage(preset=preset, output_schema={"type": "object"})


@contextmanager
def _bound(tool: str) -> Iterator[None]:
    token = set_resume_continuation_tool(tool)
    try:
        yield
    finally:
        reset_resume_continuation_tool(token)


@contextmanager
def _routed(key: str) -> Iterator[None]:
    token = set_chained_resume(ChainedResume(delivery_tool="face", chain_key=key, asked_by=("caller",)))
    try:
        yield
    finally:
        reset_chained_resume(token)


def test_a_frame_of_the_asking_run_is_an_answer_stage() -> None:
    with run_stage_scope(), _bound("driver"), stage_frame(_stage("p")):
        stages = capture_park_stages(())
    assert stages == PresetParkStages(answer=[_stage("p")])


def test_a_frame_at_the_door_is_a_terminal_stage() -> None:
    with run_stage_scope(), stage_frame(_stage("p")), _bound("driver"):
        stages = capture_park_stages(())
    assert stages == PresetParkStages(terminal=[_stage("p")])


def test_a_frame_whose_routing_a_driver_crossed_below_it_is_a_chained_stage() -> None:
    with run_stage_scope(), _bound("caller"), _routed(KEY), stage_frame(_stage("p")), _bound("nested"):
        stages = capture_park_stages((KEY,))
    assert stages == PresetParkStages(chained={KEY: [_stage("p")]})


def test_a_frame_of_a_nested_run_resuming_through_its_parent_is_adopted_and_stores_nothing() -> None:
    with run_stage_scope(), _bound("parent"), stage_frame(_stage("p")) as frame:
        token = open_nested_run_face()
        try:
            stages = capture_park_stages(())
        finally:
            reset_resume_continuation_tool(token)
    assert stages is None
    assert frame.adopted is True


def test_frames_are_listed_innermost_first() -> None:
    with run_stage_scope(), stage_frame(_stage("outer")), stage_frame(_stage("inner")), _bound("driver"):
        stages = capture_park_stages(())
    assert stages == PresetParkStages(terminal=[_stage("inner"), _stage("outer")])


def test_a_dispatch_that_returned_leaves_no_frame_for_a_later_park() -> None:
    with run_stage_scope(), _bound("driver"):
        with stage_frame(_stage("returned")):
            pass
        assert capture_park_stages(()) is None


def test_a_re_park_inside_a_drive_keeps_the_terminal_and_the_chained_stages_it_is_still_under() -> None:
    drive = PresetParkStages(
        answer=[_stage("answered")],
        terminal=[_stage("door")],
        chained={KEY: [_stage("kept")], OTHER: [_stage("left")]},
    )
    with drive_output_stages(drive), _bound("driver"), stage_frame(_stage("own")):
        stages = capture_park_stages((KEY,))
    assert stages == PresetParkStages(
        answer=[_stage("own")], terminal=[_stage("door")], chained={KEY: [_stage("kept")]}
    )


def test_a_drive_opens_an_empty_frame_stack() -> None:
    with run_stage_scope(), stage_frame(_stage("visiting")), drive_output_stages(None), _bound("driver"):
        assert capture_park_stages(()) is None


@pytest.fixture
def _clean_server() -> Iterator[None]:
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            remove_surface_tool(app._fast_mcp, tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


@pytest.mark.usefixtures("_clean_server")
async def test_sibling_preset_calls_each_classify_only_their_own_frame() -> None:
    barrier = asyncio.Barrier(2)
    schema = {"type": "object"}

    async with app.app_context(Manifest.model_validate({})):

        @app.tools.tool(force=True)
        async def sibling_base(tag: str) -> dict[str, Any]:
            """Wait until both siblings are inside their presets, then classify as an ask would."""
            await barrier.wait()
            stages = capture_park_stages(())
            return {"tag": tag, "stages": stages.model_dump() if stages is not None else None}

        @app.tools.tool(force=True, meta={TOOL_META_PAUSES: True})
        async def sibling_driver() -> dict[str, Any]:
            """A stand-in driver running two preset calls concurrently."""
            from tai42_skeleton.tools.stage_frames import _STAGE_FRAMES

            with _bound("sibling_driver"):
                before = _STAGE_FRAMES.get()
                first, second = await asyncio.gather(
                    app.tools.run_tool("p1", {"tag": "one"}), app.tools.run_tool("p2", {"tag": "two"})
                )
                after = _STAGE_FRAMES.get()
            return {"first": first, "second": second, "before": list(before), "after": list(after)}

        await app.preset_manager.register("p1", "sibling_base", {}, [], "first sibling", output_schema=schema)
        await app.preset_manager.register("p2", "sibling_base", {}, [], "second sibling", output_schema=schema)

        result = await app.tools.run_tool("sibling_driver", {})

    def _answer(preset: str) -> dict[str, Any]:
        return PresetParkStages(answer=[PresetOutputStage(preset=preset, output_schema=schema)]).model_dump()

    assert result["first"] == {"tag": "one", "stages": _answer("p1")}
    assert result["second"] == {"tag": "two", "stages": _answer("p2")}
    assert result["before"] == []
    assert result["after"] == []
