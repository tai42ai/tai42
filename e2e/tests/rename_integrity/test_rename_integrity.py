"""Rename integrity over the REAL referees union: a preset rename is BLOCKED (409, every
holder named) while any live reference would be stranded, FAILS LOUDLY when a referee
raises, and PROCEEDS (moving the tool_meta overlay) only when nothing references it.

Drives every reachable holder: a live SCHEDULE and a HOOK (the platform-internal
referees), the FIXTURE-registered plugin referee (holder + raising arms), and the
referees door's UNION of them. The conversation-route + parked-interaction platform
referees are not reachable on this stack (no conversations backend, no live park) and are
covered skeleton-side; the tool-extensions referee likewise has no create door here."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.stack import TaiStack

from ._support import (
    create_preset,
    delete_hook,
    referees,
    register_hook,
    rename_raw,
    schedule_preset,
    unschedule,
)


async def test_schedule_reference_blocks_rename(seams_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    name = uniq("sched")
    schedule_name = uniq("schedule")
    await create_preset(seams_stack, name, schedulable=True)
    await schedule_preset(seams_stack, name, schedule_name)
    try:
        blocked = await rename_raw(seams_stack, name, uniq("renamed"))
        assert blocked.status_code == 409, blocked.text
        error = blocked.json()["error"]
        assert schedule_name in error, error
        # The referees door reports the same live schedule holder.
        assert any(schedule_name in holder for holder in await referees(seams_stack, name))
    finally:
        await unschedule(seams_stack, schedule_name)
    # With the schedule gone the rename proceeds.
    proceeded = await rename_raw(seams_stack, name, uniq("renamed"))
    assert proceeded.status_code == 200, proceeded.text


async def test_hook_reference_blocks_rename(seams_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    name = uniq("hooked")
    await create_preset(seams_stack, name)
    hook_name = await register_hook(seams_stack, uniq, name)
    try:
        blocked = await rename_raw(seams_stack, name, uniq("renamed"))
        assert blocked.status_code == 409, blocked.text
        assert hook_name in blocked.json()["error"], blocked.text
        assert any(hook_name in holder for holder in await referees(seams_stack, name))
    finally:
        await delete_hook(seams_stack, hook_name)


async def test_fixture_referee_blocks_rename_with_holder_text(
    seams_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    # A HOLD-marker name draws the fixture plugin referee's holder text — a plugin-declared
    # reference blocking a rename through the public register_rename_referee seam.
    name = uniq("e2e_ref_hold")
    await create_preset(seams_stack, name)
    blocked = await rename_raw(seams_stack, name, uniq("renamed"))
    assert blocked.status_code == 409, blocked.text
    assert f"e2e fixture reference to {name!r}" in blocked.json()["error"], blocked.text
    # The door reports the same fixture holder in its union.
    assert any(name in holder for holder in await referees(seams_stack, name))


async def test_fixture_referee_raise_fails_rename_loudly(seams_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    # A RAISE-marker name makes the fixture referee raise; a referee exception fails the
    # rename LOUDLY (a 500), never a silent bypass that strands the reference.
    name = uniq("e2e_ref_raise")
    await create_preset(seams_stack, name)
    failed = await rename_raw(seams_stack, name, uniq("renamed"))
    assert failed.status_code >= 500, failed.text
    # The preset is untouched under its old name — the loud failure committed no rename.
    row = await seams_stack.api().get(f"/api/presets/{name}", retry_on_reloading=True)
    assert row["name"] == name


async def test_unreferenced_rename_proceeds_and_tool_meta_moves(
    seams_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = seams_stack.api()
    name = uniq("plain")
    new_name = uniq("renamed")
    await create_preset(seams_stack, name)
    # A tool_meta overlay row keyed on the preset — it must FOLLOW the rename.
    await api.request("PATCH", f"/api/tool-meta/tools/{name}", json={"tags": ["moved"], "display_name": "Before"})

    # No live reference: the referees union is empty and the rename proceeds.
    assert await referees(seams_stack, name) == []
    renamed = await rename_raw(seams_stack, name, new_name)
    assert renamed.status_code == 200, renamed.text

    meta = {row["tool_name"]: row for row in (await api.get("/api/tool-meta"))["meta"]}
    assert name not in meta, "the old overlay key must be gone after a rename"
    assert new_name in meta, "the overlay row must follow the preset to its new name"
    assert meta[new_name]["tags"] == ["moved"]
    assert meta[new_name]["display_name"] == "Before"


async def test_referees_door_returns_union(seams_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    # One preset held by THREE distinct referee sources at once: the fixture plugin referee
    # (HOLD marker), a live schedule, and a hook. The door returns their UNION and the
    # rename 409 names every holder.
    name = uniq("e2e_ref_hold")
    schedule_name = uniq("schedule")
    await create_preset(seams_stack, name, schedulable=True)
    await schedule_preset(seams_stack, name, schedule_name)
    hook_name = await register_hook(seams_stack, uniq, name)
    try:
        holders = await referees(seams_stack, name)
        assert any(f"e2e fixture reference to {name!r}" == holder for holder in holders), holders
        assert any(schedule_name in holder for holder in holders), holders
        assert any(hook_name in holder for holder in holders), holders

        blocked = await rename_raw(seams_stack, name, uniq("renamed"))
        assert blocked.status_code == 409, blocked.text
        error = blocked.json()["error"]
        assert name in error, error
        assert schedule_name in error, error
        assert hook_name in error, error
    finally:
        await unschedule(seams_stack, schedule_name)
        await delete_hook(seams_stack, hook_name)
