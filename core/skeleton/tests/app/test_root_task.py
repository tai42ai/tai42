"""``spawn_root_task`` starts a task in a fresh context, inheriting no ambient scope of its spawner."""

from __future__ import annotations

import asyncio
import contextvars

from tai42_skeleton.app.root_task import spawn_root_task

_VAR: contextvars.ContextVar[str] = contextvars.ContextVar("root_task_var", default="default")


async def test_spawn_root_task_runs_in_a_fresh_context() -> None:
    # A fresh-root task reads the default of a contextvar the spawner set; an ordinary
    # ``create_task`` copies the spawner's context and reads the set value. The contrast is
    # the whole point of the helper.
    seen: dict[str, str] = {}

    async def _body(key: str) -> None:
        seen[key] = _VAR.get()

    token = _VAR.set("spawner-value")
    try:
        root = spawn_root_task(_body("root"))
        copied = asyncio.create_task(_body("copied"))
        await asyncio.gather(root, copied)
    finally:
        _VAR.reset(token)

    assert seen["root"] == "default"
    assert seen["copied"] == "spawner-value"


async def test_spawn_root_task_forwards_the_name() -> None:
    async def _noop() -> None:
        return None

    task = spawn_root_task(_noop(), name="my-root")
    assert task.get_name() == "my-root"
    await task
