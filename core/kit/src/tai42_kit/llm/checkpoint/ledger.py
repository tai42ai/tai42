"""The finished-thread ledger: one checkpoint store's per-thread retention record, on both horizons.

A thread's owner — the code that minted its id — marks it finished at its terminal, with the time it
becomes deletable: its finished retention (the owner's declared one, else the platform's) after the
mark; the checkpoint sweep deletes it at or after that due time. Every consumer that starts a run on
a thread marks it active first (removes it from the ledger), so a thread used again never sits in
the ledger while it is live. Where the store's saver does not carry the waiting retention as a key
TTL, starting a run also records the owner's declared waiting retention, and the ledger lists the
threads past their waiting horizon for the sweep. The ledger lives in the same store as the
checkpoints, so a store reset clears both; each store's registration opens its own.
"""

from __future__ import annotations

from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from tai42_kit.llm.checkpoint.retention import ThreadRetention, platform_retention
from tai42_kit.llm.settings import llm_provider_settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence

    from langgraph.checkpoint.base import BaseCheckpointSaver

# Thread ids per declared-waiting lookup of the generic stale-thread walk (a store binding one
# placeholder per id stays within its limits).
_DECLARED_WAITING_PAGE: Final = 1000


class FinishedThreadLedger(Protocol):
    """The per-thread retention record of one checkpoint store.

    It holds the finished marks and their due times, the declared waiting retentions, and lists the
    threads past their waiting horizon. Every method that takes thread ids takes any sequence; an
    empty one changes nothing, on every provider.
    """

    async def start(self, thread_ids: Sequence[str], retention: ThreadRetention | None) -> None:
        """A run starts on ``thread_ids``: remove their finished mark and record their declared waiting.

        The waiting is recorded wherever the store's saver does not carry it as a key TTL; ``None``
        (the platform's) removes a recorded one. A store whose saver stamps the waiting as the TTL of
        every key it writes records nothing.
        """
        ...

    async def mark(self, thread_ids: Sequence[str], due_at: datetime) -> None:
        """Record ``thread_ids`` as finished and deletable at or after ``due_at`` (a later mark moves it)."""
        ...

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        """Up to ``limit`` thread ids due at or before ``cutoff``, earliest due first."""
        ...

    async def forget(self, thread_ids: Sequence[str]) -> None:
        """Remove ``thread_ids`` from the ledger: their finished mark and their declared waiting."""
        ...

    async def declared_waiting(self, thread_ids: Sequence[str]) -> dict[str, int]:
        """The recorded declared waiting minutes of those of ``thread_ids`` that have one."""
        ...

    async def stale_threads(self, saver: BaseCheckpointSaver, *, now: datetime, default_minutes: int) -> list[str]:
        """The threads whose newest checkpoint is older than their waiting retention before ``now``, by id.

        A thread's waiting retention is the one its owner declared at run start, else
        ``default_minutes``; its newest checkpoint is the newest in any of its namespaces. A store with
        an index of its own answers from it; otherwise ``saver`` (the store's own) is walked. ``now``
        must be timezone-aware.
        """
        ...


def as_utc(at: datetime) -> datetime:
    """``at`` in UTC; a naive time raises ``ValueError``."""
    if at.tzinfo is None:
        raise ValueError("a finished-thread ledger time must be timezone-aware")
    return at.astimezone(UTC)


async def stale_threads_by_walk(
    saver: BaseCheckpointSaver,
    declared_waiting: Callable[[Sequence[str]], Awaitable[dict[str, int]]],
    *,
    now: datetime,
    default_minutes: int,
) -> list[str]:
    """The stale-thread listing of a store without an index of its own, by id.

    Reads the newest checkpoint ``ts`` of every thread, in every namespace, through
    ``saver.alist(None)``, and compares it with the thread's ``declared_waiting`` (looked up page by
    page), else ``default_minutes``, before ``now``.
    """
    bound = as_utc(now)
    newest_by_thread: dict[str, datetime] = {}
    every_tuple = cast("AsyncGenerator[Any]", saver.alist(None))
    async with aclosing(every_tuple) as tuples:
        async for tup in tuples:
            thread_id = tup.config["configurable"]["thread_id"]
            ts = datetime.fromisoformat(tup.checkpoint["ts"])
            current = newest_by_thread.get(thread_id)
            if current is None or ts > current:
                newest_by_thread[thread_id] = ts
    stale: list[str] = []
    thread_ids = sorted(newest_by_thread)
    for start in range(0, len(thread_ids), _DECLARED_WAITING_PAGE):
        page = thread_ids[start : start + _DECLARED_WAITING_PAGE]
        declared = await declared_waiting(page)
        stale.extend(
            thread_id
            for thread_id in page
            if newest_by_thread[thread_id] < bound - timedelta(minutes=declared.get(thread_id, default_minutes))
        )
    return stale


async def _ledger(provider: str | None, conn_string: str | None) -> FinishedThreadLedger:
    from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry

    settings = llm_provider_settings()
    resolved_provider = settings.checkpoint if provider is None else provider
    resolved_conn = settings.checkpoint_conn_string if conn_string is None else conn_string
    return await checkpoint_registry().ledger(resolved_provider, resolved_conn)


async def mark_threads_finished(
    thread_ids: Sequence[str],
    *,
    retention: ThreadRetention | None = None,
    provider: str | None = None,
    conn_string: str | None = None,
) -> None:
    """Mark ``thread_ids`` finished now, in the ledger of the store that holds them.

    They become deletable their finished retention after now: ``retention``'s (the owner's
    declaration, see :func:`~tai42_kit.llm.checkpoint.retention.resolve_retention`), else the
    platform's. ``provider`` and ``conn_string`` resolve independently: ``None`` is the deployment's
    configured checkpoint provider, respectively its configured connection string. A caller that
    checkpoints on a per-run provider passes it. An empty sequence is a no-op; a store error
    propagates.
    """
    if not thread_ids:
        return
    finished_minutes = (retention or platform_retention()).finished_minutes
    ledger = await _ledger(provider, conn_string)
    await ledger.mark(list(thread_ids), datetime.now(UTC) + timedelta(minutes=finished_minutes))


async def mark_threads_active(
    thread_ids: Sequence[str],
    *,
    retention: ThreadRetention | None = None,
    provider: str | None = None,
    conn_string: str | None = None,
) -> None:
    """Take ``thread_ids`` out of the ledger before a run starts on them, recording ``retention``'s waiting.

    ``retention`` is the owner's declaration for the threads (``None``: the platform's); the store
    records its waiting where the waiting horizon is swept. ``provider`` and ``conn_string`` resolve
    as in :func:`mark_threads_finished`.
    """
    if not thread_ids:
        return
    ledger = await _ledger(provider, conn_string)
    await ledger.start(list(thread_ids), retention)
