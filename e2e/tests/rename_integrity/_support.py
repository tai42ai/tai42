"""Shared helpers for the rename-integrity legs: create a preset (optionally carrying
the backend ``schedule_task`` branch), register a recurring schedule or a hook that
references it, and read the referees union. Every reference maker names its holder so a
leg asserts the exact 409 and the union."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.stack import TaiStack

# A schedule far out of any test's lifetime: the referee reads the RECORD, never a
# firing, so the cadence only has to keep the scheduled preset from running. A plain
# integer interval is representable on every backend (arq/celery/rq).
_INERT_INTERVAL_SECONDS = 315360000  # ~10 years


async def create_preset(stack: TaiStack, name: str, *, schedulable: bool = False, payload: str = "baked") -> dict:
    """Create an ``e2e_echo`` preset; ``schedulable`` attaches the backend
    ``schedule_task`` extension so a ``<name>_schedule_task`` branch exists to schedule."""
    body = {
        "name": name,
        "base_tool": "e2e_echo",
        "description": "rename-integrity echo preset",
        "fixed_kwargs": {"payload": payload},
    }
    if schedulable:
        body["extensions"] = [["schedule_task"]]
    return await stack.api().post("/api/presets", json=body, retry_on_reloading=True)


async def schedule_preset(stack: TaiStack, preset: str, schedule_name: str) -> None:
    """Register an inert recurring schedule whose dispatch target is ``preset`` (its
    ``schedule_task`` branch fires the preset), so the schedule referee holds a rename."""
    await stack.api().post(
        "/api/schedules",
        json={
            "tool_name": preset,
            "tool_kwargs": {},
            "cadence": {"kind": "interval", "every": _INERT_INTERVAL_SECONDS},
            "schedule_name": schedule_name,
        },
        retry_on_reloading=True,
    )


async def unschedule(stack: TaiStack, schedule_name: str) -> None:
    await stack.api().delete(f"/api/schedules/{schedule_name}", retry_on_reloading=True)


async def register_hook(stack: TaiStack, uniq: Callable[[str], str], preset: str) -> str:
    """Register a hook whose ``tool`` is ``preset`` so the hook referee holds a rename;
    returns the hook name (a lowercase URL-segment-safe identifier)."""
    hook_name = uniq("hook").replace("_", "-")
    await stack.api().post(
        "/api/hooks",
        json={
            "name": hook_name,
            "topic": uniq("topic").replace("_", "-"),
            "tool": preset,
            "execution_key": uniq("exec"),
        },
        retry_on_reloading=True,
    )
    return hook_name


async def delete_hook(stack: TaiStack, hook_name: str) -> None:
    await stack.api().delete(f"/api/hooks/{hook_name}", retry_on_reloading=True)


async def referees(stack: TaiStack, preset: str) -> list[str]:
    """The rename preview door's holder summaries for ``preset``."""
    body = await stack.api().get(f"/api/presets/{preset}/referees?action=rename", retry_on_reloading=True)
    assert body["action"] == "rename"
    return [holder["summary"] for holder in body["holders"]]


async def rename_raw(stack: TaiStack, preset: str, new_name: str):
    """Attempt a rename, returning the raw response so a leg reads its status + body."""
    return await stack.api().request_raw("POST", f"/api/presets/{preset}/rename", json={"new_name": new_name})
