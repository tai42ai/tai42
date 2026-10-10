"""Checkpoint retention — the one sweep that deletes expired checkpoint threads of the deployment's store.

Two horizons. The platform's two values (the kit ``LLMProviderSettings``) are every thread's
default and ceiling; a thread's owner may declare a shorter retention for it
(``tai42_kit.llm.checkpoint.retention``):

- finished: a thread its owner marked finished in the store's finished-thread ledger is deleted at
  the due time stored with the mark (its finished retention after the mark, the owner's declared
  one or ``checkpoint_retention_finished_minutes``), on every provider;
- waiting: any other thread is deleted its waiting retention after its newest checkpoint — the
  waiting its owner declared at run start, else ``checkpoint_retention_waiting_minutes``.
  ``postgres``/``sqlite`` are swept here; ``redis`` expires the thread by its key TTL (stamped
  with the declared waiting at write) and ``memory`` keeps it for the process lifetime, so the
  waiting horizon is skipped for both.

Before any delete, every registered live-thread filter (``tai42_kit.llm.checkpoint.liveness``) is asked:
a thread a consumer reports live is spared — logged at WARNING, counted on
``tai42_checkpoint_sweep_spared_total`` and listed in the result — and a finished one stays in the
ledger so the next sweep checks it again.

The sweep covers the deployment's configured provider and connection string only: the one store
every process shares. One interleaving is accepted: a sweep that read a thread past its horizon
before a run restarted on the same id, and deletes after the restart began, can delete the thread
the restart has just begun to re-checkpoint — a compound event on a thread already past its
horizon, whose next drive re-checkpoints.

As an operation it projects as a tool, so it is runnable by name through
``/api/schedules``. Native recurrence additionally needs a ``schedule_task``-branched
vehicle (a deployment manifest wraps the tool with the backend's schedule extension);
external cron of ``tai checkpoints sweep`` is the busless alternative.
"""

from __future__ import annotations

import logging
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast

from tai42_kit.llm.checkpoint import checkpoint_provider_facts, live_threads
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.settings import llm_provider_settings

from tai42_skeleton.interactions import checkpoint_liveness  # noqa: F401 -- registers the interactions filter
from tai42_skeleton.operations import operation
from tai42_skeleton.operations.checkpoint_metrics import sweep_spared_counter
from tai42_skeleton.operations.response_models_group_c import CheckpointSweepResult

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from langgraph.checkpoint.base import BaseCheckpointSaver
    from tai42_kit.llm.checkpoint import FinishedThreadLedger
    from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource

logger = logging.getLogger(__name__)

# Candidate threads handled per page (one ledger read and one live-thread filter pass each).
_PAGE: Final = 1000

# ``adelete_thread`` removes every document the thread holds when it is called; a checkpoint
# written meanwhile survives it, so the sweep deletes again until none remains, at most this
# many rounds.
_MAX_DELETE_ROUNDS: Final = 100


class CheckpointSweepError(RuntimeError):
    """A thread could not be removed from the checkpoint store."""


async def _delete_thread(saver: BaseCheckpointSaver, thread_id: str) -> None:
    for _ in range(_MAX_DELETE_ROUNDS):
        await saver.adelete_thread(thread_id)
        remaining = False
        tuples_of_thread = cast("AsyncGenerator[Any]", saver.alist({"configurable": {"thread_id": thread_id}}, limit=1))
        async with aclosing(tuples_of_thread) as tuples:
            async for _tup in tuples:
                remaining = True
                break
        if not remaining:
            return
    raise CheckpointSweepError(f"thread {thread_id} still holds checkpoints after {_MAX_DELETE_ROUNDS} delete rounds")


class _Sweep:
    """One sweep over the deployment's checkpoint store."""

    def __init__(self, provider: str, conn_string: str | None, saver: BaseCheckpointSaver) -> None:
        self.provider = provider
        self.conn_string = conn_string
        self.saver = saver
        self.spared: list[str] = []

    async def page(self, thread_ids: list[str], horizon: str) -> list[str]:
        """Delete every thread of the page no filter claims; return the deleted ids."""
        claimed = await live_threads(self.provider, self.conn_string, thread_ids)
        deleted: list[str] = []
        for thread_id in thread_ids:
            owner = claimed.get(thread_id)
            if owner is not None:
                logger.warning(
                    "checkpoint sweep: thread %s is past the %s horizon but %s reports it live; kept",
                    thread_id,
                    horizon,
                    owner,
                )
                sweep_spared_counter().labels(horizon=horizon, owner=owner).inc()
                self.spared.append(thread_id)
                continue
            await _delete_thread(self.saver, thread_id)
            deleted.append(thread_id)
        return deleted

    async def finished(self, ledger: FinishedThreadLedger, cutoff: datetime) -> list[str]:
        swept: list[str] = []
        seen: set[str] = set()
        while True:
            # Spared threads stay in the ledger, so each read asks past the ones already handled.
            due = await ledger.finished_before(cutoff, limit=len(seen) + _PAGE)
            fresh = [thread_id for thread_id in due if thread_id not in seen]
            if not fresh:
                return swept
            seen.update(fresh)
            deleted = await self.page(fresh, "finished")
            await ledger.forget(deleted)
            swept.extend(deleted)

    async def waiting(self, ledger: FinishedThreadLedger, stale: list[str]) -> list[str]:
        already_spared = set(self.spared)
        candidates = [thread_id for thread_id in stale if thread_id not in already_spared]
        swept: list[str] = []
        for start in range(0, len(candidates), _PAGE):
            deleted = await self.page(candidates[start : start + _PAGE], "waiting")
            await ledger.forget(deleted)
            swept.extend(deleted)
        return swept


async def _stale_threads(
    provider: str, resource: CheckpointResource, saver: Any, now: datetime, default_minutes: int
) -> list[str]:
    if provider == "postgres":
        from tai42_kit.llm.checkpoint import postgres_store

        return await postgres_store.stale_threads(resource.handle, now=now, default_minutes=default_minutes)
    # sqlite: the newest checkpoint of every thread, walked through the saver (development scale).
    newest_by_thread: dict[str, datetime] = {}
    async for tup in saver.alist(None):
        thread_id = tup.config["configurable"]["thread_id"]
        ts = datetime.fromisoformat(tup.checkpoint["ts"])
        current = newest_by_thread.get(thread_id)
        if current is None or ts > current:
            newest_by_thread[thread_id] = ts
    stale: list[str] = []
    thread_ids = sorted(newest_by_thread)
    for start in range(0, len(thread_ids), _PAGE):
        page = thread_ids[start : start + _PAGE]
        declared = await resource.ledger.declared_waiting(page)
        stale.extend(
            thread_id
            for thread_id in page
            if newest_by_thread[thread_id] < now - timedelta(minutes=declared.get(thread_id, default_minutes))
        )
    return stale


@operation(
    summary="Sweep expired checkpoint threads",
    tags=["checkpoints"],
    destructive=True,
    reload_gated=True,
    response_model=CheckpointSweepResult,
)
async def sweep_checkpoints() -> dict[str, Any]:
    """Delete the checkpoint threads of the deployment's store that are past their retention horizon.

    A finished thread (marked by its owner) goes at the due time stored with its mark; on
    ``postgres``/``sqlite`` any thread goes its waiting retention (declared by its owner, else
    ``checkpoint_retention_waiting_minutes``) after its newest checkpoint (``redis`` expires it by key
    TTL and ``memory`` keeps it for the process — both reported in ``skipped``). A thread a
    registered live-thread filter claims is spared and listed. ``waiting_minutes`` and
    ``finished_minutes`` in the result are the platform's values: the default and ceiling of every
    thread's retention.
    """
    settings = llm_provider_settings()
    provider = settings.checkpoint
    conn_string = settings.checkpoint_conn_string
    waiting = settings.checkpoint_retention_waiting_minutes
    finished = settings.checkpoint_retention_finished_minutes
    facts = checkpoint_provider_facts(provider)
    registry = checkpoint_registry()
    resource = await registry.resource(provider, conn_string)
    saver = await registry.get_checkpointer(provider, conn_string)
    now = datetime.now(UTC)

    sweep = _Sweep(provider, conn_string, saver)
    finished_swept = await sweep.finished(resource.ledger, now)

    skipped: str | None = None
    waiting_swept: list[str] = []
    if facts.retention == "sweep":
        stale = await _stale_threads(provider, resource, saver, now, waiting)
        waiting_swept = await sweep.waiting(resource.ledger, stale)
    elif facts.retention == "native_ttl":
        skipped = f"waiting horizon: provider {provider!r} expires threads by their key TTL"
    else:
        skipped = f"waiting horizon: provider {provider!r} keeps threads for the process lifetime"

    return {
        "provider": provider,
        "waiting_minutes": waiting,
        "finished_minutes": finished,
        "finished_swept": finished_swept,
        "waiting_swept": waiting_swept,
        "swept_count": len(finished_swept) + len(waiting_swept),
        "spared": sweep.spared,
        "skipped": skipped,
    }
