"""The drains: a subject's outstanding pending saves are finished before anything reads or runs on it.

Read side: a record read or write that meets an unapplied save on its subject applies it first
(help-along) and runs again; a failed save raises :class:`StatePendingSaveFailedError`, never a
timeout. The whole-state scans raise on, skip or count the held saves, and the rename referee
drains a target. Run side: every run entry waits for the outstanding saves of its door's candidate
subjects — their records AND their deferred calls — unless the same task already drained them;
inside a deferred call a drain skips exactly the saves whose calls wait on that call's save.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal

from tai42_contract.states.errors import StatePendingSaveFailedError, StatePendingSaveTimeoutError
from tai42_contract.states.pending import HeldPendingSave

from tai42_skeleton.states import db as states_db
from tai42_skeleton.states.context import current_state_context
from tai42_skeleton.states.db import states_settings

from .apply import apply_row
from .keys import subject_keys_of, target_key
from .metrics import outbox_metrics
from .models import OutboxPending, OutboxRow, RowApply

if TYPE_CHECKING:
    from tai42_skeleton.states.service.base import _StatesServiceBase

logger = logging.getLogger(__name__)

HeldMode = Literal["raise", "skip", "count"]


def _drain_timeout() -> float:
    return states_settings().outbox_drain_timeout_seconds


def describe_key(key: str) -> str:
    """A record or subject key as an operator reads it: ``tk/tn/kind/key`` (``of state 's'`` for a record)."""
    parts = json.loads(key)
    if len(parts) == 5:
        state, tk, tn, kind, subject = parts
        return f"{tk}/{tn}/{kind}/{subject} of state {state!r}"
    return "/".join(parts)


def _failed(key: str, save_id: int) -> StatePendingSaveFailedError:
    return StatePendingSaveFailedError(
        f"subject {describe_key(key)} has a failed pending save {save_id}; an operator must retry or discard it",
        save_id=str(save_id),
    )


def _held_behind(key: str, save_id: int, held_by: int) -> StatePendingSaveFailedError:
    return StatePendingSaveFailedError(
        f"subject {describe_key(key)} has pending save {save_id} held behind failed pending save {held_by}; "
        f"an operator must retry or discard {held_by}",
        save_id=str(held_by),
    )


def _timed_out(key: str, save_id: int) -> StatePendingSaveTimeoutError:
    return StatePendingSaveTimeoutError(
        f"subject {describe_key(key)} still has pending save {save_id} after {_drain_timeout()}s",
        save_id=str(save_id),
    )


def _raise_unfinished(key: str, row_id: int, outcome: RowApply) -> None:
    """Raise what a reader reads when its help-along could not apply row ``row_id``."""
    if outcome.outcome == "held":
        if outcome.held_by is None:
            raise AssertionError("a held apply names the failed save that holds it")
        raise _held_behind(key, row_id, outcome.held_by)
    if outcome.outcome == "failed":
        raise _failed(key, row_id) from outcome.error
    if outcome.outcome in ("contended", "retry"):
        raise _timed_out(key, row_id) from outcome.error


# -- the read side ----------------------------------------------------------
async def drained[T](service: _StatesServiceBase, call: Callable[[], Awaitable[T]]) -> T:
    """Run ``call``; while it meets an unapplied pending save, drain it and run ``call`` again.

    Bounded by one ``STATES_OUTBOX_DRAIN_TIMEOUT_SECONDS`` deadline. ``call`` must raise
    :class:`OutboxPending` before it writes anything, so running it again repeats nothing.
    """
    started: float | None = None
    deadline = 0.0
    while True:
        try:
            result = await call()
        except OutboxPending as pending:
            if started is None:
                started = monotonic()
                deadline = started + _drain_timeout()
            elif monotonic() >= deadline:
                rows = await service._store.outbox_unapplied_on_records(pending.keys)
                if rows:
                    raise _timed_out(pending.keys[0], rows[0][0]) from None
            await drain_records(service, pending.keys, deadline)
            continue
        if started is not None:
            metrics = outbox_metrics()
            metrics.drain_waits.labels("read").inc()
            metrics.drain_wait_seconds.labels("read").observe(monotonic() - started)
        return result


async def drain_records(service: _StatesServiceBase, keys: Sequence[str], deadline: float) -> None:
    """Apply every row with unapplied records on ``keys``, in id order; a held or failed one raises."""
    key = keys[0]
    for row_id, status in await service._store.outbox_unapplied_on_records(keys):
        if status == "failed":
            raise _failed(key, row_id)
        outcome = await apply_row(service, row_id, deadline=deadline)
        if outcome.outcome not in ("applied", "not_pending"):
            _raise_unfinished(key, row_id, outcome)


def _held_save(row: OutboxRow, state: str, held_by: int) -> HeldPendingSave:
    return HeldPendingSave(
        save_id=str(row.id),
        held_by=str(held_by),
        subjects=[s.canonical for s in row.subjects if s.state == state],
    )


async def drain_state_rows(
    service: _StatesServiceBase, state: str, deadline: float, *, held: HeldMode, scan: str
) -> list[tuple[HeldPendingSave, OutboxRow]]:
    """Apply every row with unapplied records writing ``state``; meet each held save per ``held``.

    ``raise`` raises on the first held save naming the failed save that holds it; ``skip`` collects
    them and logs a WARNING naming them (the scan proceeds over committed records); ``count``
    collects them for the caller. A row whose help-along apply fails here is collected as held by
    itself.
    """
    out: list[tuple[HeldPendingSave, OutboxRow]] = []
    for row in await service._store.outbox_unapplied_on_state(state):
        key = row.record_keys[0]
        held_by: int
        if row.status == "failed":
            held_by = row.id
        else:
            outcome = await apply_row(service, row.id, deadline=deadline)
            if outcome.outcome in ("applied", "not_pending"):
                continue
            if outcome.outcome in ("contended", "retry"):
                raise _timed_out(key, row.id) from outcome.error
            held_by = outcome.held_by if outcome.held_by is not None else row.id
        if held == "raise":
            if held_by == row.id:
                raise _failed(key, row.id)
            raise _held_behind(key, row.id, held_by)
        out.append((_held_save(row, state, held_by), row))
    if held == "skip" and out:
        subjects = held_subjects([h for h, _ in out])
        logger.warning(
            "states: %s on state %r skipped %d subject(s) held by failed pending save(s) %s: %s",
            scan,
            state,
            len(subjects),
            sorted({h.held_by for h, _ in out}),
            subjects,
        )
    return out


def held_subjects(held: Sequence[HeldPendingSave]) -> list[dict[str, Any]]:
    """The distinct subjects of ``held``, in first-seen order, as plain dicts for a log line."""
    seen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for save in held:
        for subject in save.subjects:
            seen.setdefault((subject.target_kind, subject.target_name, subject.kind, subject.key), subject.model_dump())
    return list(seen.values())


async def drain_state(
    service: _StatesServiceBase, state: str, deadline: float, *, held: HeldMode, scan: str
) -> list[HeldPendingSave]:
    """:func:`drain_state_rows`' held saves."""
    return [h for h, _ in await drain_state_rows(service, state, deadline, held=held, scan=scan)]


# -- the run side -----------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _EntryMark:
    """The subjects one task has drained at its run entries."""

    task: asyncio.Task[Any]
    subjects: frozenset[str]


@dataclass(frozen=True, slots=True)
class _ApplyingSave:
    """The pending save whose deferred calls the current execution runs, under one claim."""

    save_id: int
    subject_keys: frozenset[str]
    claim: str


_run_entry_mark: ContextVar[_EntryMark | None] = ContextVar("tai42_states_run_entry_mark", default=None)
# Inherited by every task a deferred call starts; a drain honours it only while the save still runs under the claim.
_applying_save: ContextVar[_ApplyingSave | None] = ContextVar("tai42_states_applying_save", default=None)


def _states_on() -> bool:
    """Whether the states feature is on, read through its module at call time."""
    return states_db.states_store_configured()


@asynccontextmanager
async def run_entry_drain() -> AsyncIterator[None]:
    """Wait for the ambient subject's outstanding saves at a run entry, unless THIS task already drained them.

    A mark set by another task (a ``create_task``/``gather`` child inherits a copy of it) never skips a drain.
    """
    ctx = current_state_context()
    keys = frozenset(subject_keys_of(ctx.candidates)) if ctx is not None and _states_on() else frozenset()
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("run_entry_drain must run inside an asyncio task")
    mark = _run_entry_mark.get()
    drained_keys = mark.subjects if mark is not None and mark.task is task else frozenset()
    missing = keys - drained_keys
    if missing:
        await drain_subjects(
            live_states_service(), missing, deadline=monotonic() + _drain_timeout(), applying=_applying_save.get()
        )
    token = _run_entry_mark.set(_EntryMark(task=task, subjects=drained_keys | keys))
    try:
        yield
    finally:
        _run_entry_mark.reset(token)


@contextmanager
def outbox_apply_scope(save_id: int, subject_keys: frozenset[str], claim: str) -> Iterator[None]:
    """Bind the execution of save ``save_id``'s deferred calls under ``claim``.

    The binding task's own run entries skip the drain of the save's subjects (its claim ordered them); a drain beneath
    it, in any task, skips only the saves whose calls wait on this one.
    """
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("outbox_apply_scope must run inside an asyncio task")
    mark_token = _run_entry_mark.set(_EntryMark(task=task, subjects=subject_keys))
    save_token = _applying_save.set(_ApplyingSave(save_id=save_id, subject_keys=subject_keys, claim=claim))
    try:
        yield
    finally:
        _applying_save.reset(save_token)
        _run_entry_mark.reset(mark_token)


def current_applying_save() -> _ApplyingSave | None:
    """The deferred-call scope bound to the current execution, or ``None``."""
    return _applying_save.get()


def live_states_service() -> _StatesServiceBase:
    """The live app's states service; the outbox's sweep, run entries and referees reach it here."""
    from tai42_skeleton.app.instance import built_app

    app = built_app()
    if app is None:
        raise RuntimeError("the states outbox runs inside the app; no app is built")
    return app._states_service


async def _live_scope(service: _StatesServiceBase, applying: _ApplyingSave | None) -> _ApplyingSave | None:
    """The scope while its save still runs its calls under its claim; a lapsed one is dropped."""
    if applying is None:
        return None
    status = await service._store.outbox_status(applying.save_id)
    if status is None or status[0] != "running" or status[1] != applying.claim:
        return None
    return applying


def _waits_on(applying: _ApplyingSave, row_id: int, row_keys: Sequence[str]) -> bool:
    """Whether a row's calls wait directly on the applying save (newer, sharing a subject)."""
    return row_id > applying.save_id and not applying.subject_keys.isdisjoint(row_keys)


async def drain_subjects(
    service: _StatesServiceBase,
    keys: frozenset[str] | Sequence[str],
    deadline: float,
    *,
    applying: _ApplyingSave | None = None,
) -> None:
    """Finish every outstanding save on the subject ``keys`` — records and deferred calls — in id order.

    A failed save, or one held behind a failed save, raises at once; only a save still working can
    time out. ``applying`` (inside a deferred call) skips its own save and the saves whose calls
    wait on it, while its save still runs under its claim.
    """
    applying = await _live_scope(service, applying)
    rows = await service._store.outbox_outstanding_on_subjects(sorted(keys))
    if not rows:
        return
    started = monotonic()
    for row_id, status, row_keys in rows:
        if applying is not None and row_id == applying.save_id:
            continue
        key = next((k for k in row_keys if k in keys), next(iter(keys)))
        await _finish_row(service, row_id, status, row_keys, key, deadline, applying)
    metrics = outbox_metrics()
    metrics.drain_waits.labels("run_entry").inc()
    metrics.drain_wait_seconds.labels("run_entry").observe(monotonic() - started)


async def _finish_row(
    service: _StatesServiceBase,
    row_id: int,
    status: str,
    row_keys: Sequence[str],
    key: str,
    deadline: float,
    applying: _ApplyingSave | None,
) -> None:
    """Wait for one outstanding row: apply its records, then wait for its calls."""
    if status == "failed":
        raise _failed(key, row_id)
    if status == "pending":
        await _help_along(service, row_id, key, deadline)
        now = await service._store.outbox_status(row_id)
        if now is None:
            return
        if now[0] == "failed":
            raise _failed(key, row_id)
    await _wait_calls(service, row_id, row_keys, key, deadline, applying)


async def _wait_calls(
    service: _StatesServiceBase,
    row_id: int,
    row_keys: Sequence[str],
    key: str,
    deadline: float,
    applying: _ApplyingSave | None,
) -> bool:
    """Wait until row ``row_id``'s calls finish; ``True`` when its chain waits on the applying save (skipped).

    The row's calls run only once no older outstanding row shares one of its subjects; each such
    blocker is applied (pending), waited for (calls), or raised on (failed), down the chain.
    """
    while True:
        if applying is not None and _waits_on(applying, row_id, row_keys):
            return True
        blocker = await service._store.outbox_blocker(row_id, row_keys)
        if blocker is None:
            if await _poll_unblocked(service, row_id, key, deadline):
                return False
            continue
        blocker_id, blocker_status, blocker_keys = blocker
        if (
            applying is not None
            and blocker_status != "failed"
            and (blocker_id == applying.save_id or _waits_on(applying, blocker_id, blocker_keys))
        ):
            return True
        if blocker_status == "failed":
            raise _failed(key, blocker_id)
        if blocker_status == "pending":
            await _help_along(service, blocker_id, key, deadline)
            continue
        if await _wait_calls(service, blocker_id, blocker_keys, key, deadline, applying):
            return True


async def _help_along(service: _StatesServiceBase, row_id: int, key: str, deadline: float) -> None:
    """Apply row ``row_id``'s records for a waiting reader; what cannot be applied raises."""
    outcome = await apply_row(service, row_id, deadline=deadline)
    if outcome.outcome not in ("applied", "not_pending"):
        _raise_unfinished(key, row_id, outcome)


async def _poll_unblocked(service: _StatesServiceBase, row_id: int, key: str, deadline: float) -> bool:
    """One look at a row nothing older blocks: ``True`` once it is gone; raises when failed or past the deadline."""
    status = await service._store.outbox_status(row_id)
    if status is None:
        return True
    if status[0] == "failed":
        raise _failed(key, row_id)
    if status[0] == "pending":
        await _help_along(service, row_id, key, deadline)
        return False
    if monotonic() >= deadline:
        raise _timed_out(key, row_id)
    await asyncio.sleep(states_settings().outbox_drain_poll_seconds)
    return False


async def drain_target(service: _StatesServiceBase, target_kind: str, target_name: str, deadline: float) -> list[str]:
    """Finish every outstanding save under one conversation target; the lines naming those that cannot finish.

    A pending save is applied; a failed one, or one held behind a failed one, is named; a save
    still running its deferred calls is waited for until the deadline, then named.
    """
    applying = await _live_scope(service, _applying_save.get())
    store = service._store
    poll = states_settings().outbox_drain_poll_seconds
    lines: list[str] = []
    where = f"target {target_kind}/{target_name}"
    for row_id, status, row_keys in await store.outbox_outstanding_on_target(target_key(target_kind, target_name)):
        if applying is not None and (row_id == applying.save_id or _waits_on(applying, row_id, row_keys)):
            continue
        current: str | None = status
        if current == "pending":
            outcome = await apply_row(service, row_id, deadline=deadline)
            if outcome.outcome == "held":
                lines.append(_held_line(outcome.held_by if outcome.held_by is not None else row_id, where))
                continue
            if outcome.outcome == "failed":
                lines.append(_held_line(row_id, where))
                continue
            if outcome.outcome in ("contended", "retry"):
                raise _timed_out(row_keys[0] if row_keys else target_key(target_kind, target_name), row_id)
            now = await store.outbox_status(row_id)
            current = None if now is None else now[0]
        while current in ("calls", "running"):
            if monotonic() >= deadline:
                lines.append(f"pending state save {row_id} under {where} is still running its deferred calls")
                break
            await asyncio.sleep(poll)
            now = await store.outbox_status(row_id)
            current = None if now is None else now[0]
        if current == "failed":
            lines.append(_held_line(row_id, where))
    # A failed save and the saves held behind it name the same holder once.
    return list(dict.fromkeys(lines))


def _held_line(save_id: int, where: str) -> str:
    return f"failed pending state save {save_id} holds writes under {where}; an operator must retry or discard it"
