"""The run-entry mark a drained entry leaves in its context: what a snapshot of that context keeps.

No subject is forwarded, so the entry runs no drain query and needs no store.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import weakref

from tai42_skeleton.states.outbox.drain import run_entry_drain


async def test_a_finished_entrys_task_is_not_kept_by_a_snapshot_of_its_context() -> None:
    snapshots: list[contextvars.Context] = []

    async def entry() -> None:
        async with run_entry_drain():
            snapshots.append(contextvars.copy_context())

    task = asyncio.create_task(entry())
    await task
    finished = weakref.ref(task)
    del task
    await asyncio.sleep(0)
    gc.collect()

    assert snapshots
    assert finished() is None
