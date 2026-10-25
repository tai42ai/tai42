"""A preset's extension branch tools over the REAL presets doors: a branch a preset
produces (``<preset>_sync_task``) is refused as another preset's base, a live holder of
that branch name blocks the preset's rename (the rename moves the branch with it), and a
branch of a plain tool stays a valid base.

Runs on the seams stack: its backend loads ``sync_task``, so a preset carrying that
extension binds a ``<preset>_sync_task`` branch, and its hooks door gives the branch a
holder the hook referee reads."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.stack import TaiStack

from ._support import delete_hook, referees, register_hook, rename_raw

pytestmark = pytest.mark.needs("kind:versioning", "probe-tools", "kind:backend", "setting:extension:sync_task")

# A branch of a plain (non-preset) probe tool: the seams stack binds ``sync_task`` onto
# ``e2e_worker_info``.
_PLAIN_TOOL_BRANCH = "e2e_worker_info_sync_task"


async def _create_sync_task_preset(stack: TaiStack, name: str) -> str:
    """Create an ``e2e_echo`` preset carrying ``sync_task``; returns its branch tool name."""
    await stack.api().post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": "e2e_echo",
            "description": "branch-producing echo preset",
            "fixed_kwargs": {"payload": "baked"},
            "extensions": [["sync_task"]],
        },
        retry_on_reloading=True,
    )
    branch = f"{name}_sync_task"
    async with stack.mcp() as mcp:
        assert branch in await mcp.tool_names(), f"the preset never bound its {branch!r} branch"
    return branch


async def test_preset_branch_is_refused_as_a_preset_base(seams_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = seams_stack.api()
    origin = uniq("origin")
    branch = await _create_sync_task_preset(seams_stack, origin)
    expected = f"base tool {branch!r} is derived from preset {origin!r}"

    body = {"name": uniq("derived"), "base_tool": branch, "description": "preset over a preset branch"}
    refused = await api.request_raw("POST", "/api/presets", json=body)
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"] == expected, refused.text
    # The refused preset was never stored.
    assert body["name"] not in [p["name"] for p in await api.get("/api/presets")]

    # The dry-run validate door gives the same verdict.
    verdict = await api.post("/api/presets/validate", json={"name": uniq("draft"), "base_tool": branch})
    assert verdict == {"valid": False, "error": expected}


@pytest.mark.needs("setting:ACCESS_CONTROL_ENABLE=false")
async def test_hook_on_a_preset_branch_blocks_rename(seams_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    name = uniq("branched")
    branch = await _create_sync_task_preset(seams_stack, name)
    hook_name = await register_hook(seams_stack, uniq, branch)
    try:
        blocked = await rename_raw(seams_stack, name, uniq("renamed"))
        assert blocked.status_code == 409, blocked.text
        assert hook_name in blocked.json()["error"], blocked.text
        # The referees door reports the same branch holder for the preset.
        assert any(hook_name in holder for holder in await referees(seams_stack, name))
    finally:
        await delete_hook(seams_stack, hook_name)
    # With the hook gone the rename proceeds.
    proceeded = await rename_raw(seams_stack, name, uniq("renamed"))
    assert proceeded.status_code == 200, proceeded.text


async def test_plain_tool_branch_is_a_valid_preset_base(seams_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = seams_stack.api()
    async with seams_stack.mcp() as mcp:
        assert _PLAIN_TOOL_BRANCH in await mcp.tool_names()
    name = uniq("on_branch")
    created = await api.post(
        "/api/presets",
        json={"name": name, "base_tool": _PLAIN_TOOL_BRANCH, "description": "preset over a plain tool branch"},
        retry_on_reloading=True,
    )
    assert created["base_tool"] == _PLAIN_TOOL_BRANCH
    assert created["conflicted"] is False
    async with seams_stack.mcp() as mcp:
        assert name in await mcp.tool_names()
