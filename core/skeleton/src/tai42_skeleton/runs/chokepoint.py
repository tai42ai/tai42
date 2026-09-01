"""The run-lifecycle chokepoint that writes the runs-index row.

Entered by the ``run_tool`` binding around the OUTERMOST registered-preset dispatch
only (a nested sub-preset dispatch is part of its parent's run, never its own row).
It writes the START row at dispatch entry and the terminal outcome at exit, reading
the run's identity from the ambient attribution the conversation seam deposited.

FAIL-SAFE BY CONSTRUCTION
-------------------------
Enumeration must never break a run. Every store touch is guarded: a store/DB error
at START logs and degrades to an unrecorded run (the dispatch proceeds); a terminal
write error logs and is swallowed. The ``current_trace_id()`` sample is a guard query
(returns ``None``, never raises). When the skeleton Postgres component is not
configured the chokepoint is a clean no-op — the runs index is simply OFF, like the
rest of the durable-store surface.

TRACE ID — "captured once available"
------------------------------------
``trace_id`` deep-links to the monitoring trace but is best-effort: a preset whose
body opens no trace has none, and even for one that does the ambient trace may not be
active at the exact START/END sample points. The id is sampled at START and, if still
absent, re-sampled at the terminal write and backfilled; if neither sample sees a
trace the row is still fully enumerable by ``run_id`` and its attribution, only
without a deep link.

LIFECYCLE CORRELATION — the ``interaction_id`` column
-----------------------------------------------------
A dispatch that async-parks (returns a ``SuspendedInteraction``) terminates
``parked``, and its answer later fires a SEPARATE resume dispatch — a second row.
The two rows of that logical run are joined by the park's interaction id, the one
identifier both sides carry: the parked row records it from the sentinel at the
terminal write, and the resume row records it at START from the ambient
:func:`resume_origin` deposit the interactions continuation drive lays around its
``run_tool`` re-entry. First-set wins on the column (the store COALESCEs), so a
resume that parks AGAIN keeps its origin id and the new park's id is not separately
recorded — a single-park lifecycle joins fully; a multi-park chain is not end-to-end
walkable by this column alone. No correlation available → NULL, never an error.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from tai42_contract.interactions import SuspendedInteraction
from tai42_kit.db import component_store_configured

from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.runs.models import RunOutcome
from tai42_skeleton.runs.store import get_run_index_store
from tai42_skeleton.tools.attribution import get_run_attribution

logger = logging.getLogger(__name__)

# The ambient resume-origin deposit (the run-attribution / interaction-origin
# ContextVar discipline): the interactions continuation drive sets the parked
# interaction's id around the ``run_tool`` re-entry that resumes it, so the resume
# dispatch's START row can name the lifecycle it continues. ``None`` outside a
# continuation fire; a detached resume task carries the deposit on its context copy.
_resume_origin: ContextVar[str | None] = ContextVar("tai42_runs_resume_origin", default=None)


def get_resume_origin() -> str | None:
    """The interaction id whose resolution fired the current dispatch, or ``None``
    outside a continuation resume — a plain contextvar read, never raises."""
    return _resume_origin.get()


@contextmanager
def resume_origin(interaction_id: str) -> Iterator[None]:
    """Deposit ``interaction_id`` as the ambient resume origin for the wrapped
    dispatch, resetting in a ``finally`` (token discipline). Entered by the
    interactions continuation drive around its ``run_tool`` re-entry."""
    token = _resume_origin.set(interaction_id)
    try:
        yield
    finally:
        _resume_origin.reset(token)


def _safe_trace_id() -> str | None:
    """The active monitoring trace id, or ``None`` — a guard query that never raises.

    Imported lazily: monitoring reaches back across the app and the writer's
    ``current_trace_id`` is contractually a returns-``None``-never-raises guard, so a
    missing/failed backend simply yields no deep link."""
    try:
        from tai42_skeleton.monitoring import get_monitoring

        return get_monitoring().writer.current_trace_id()
    except Exception:
        logger.debug("runs-index: current_trace_id sample failed", exc_info=True)
        return None


class RunRecord:
    """A live run row's handle: the binding calls :meth:`observe` with the dispatch
    result so a park (a ``SuspendedInteraction`` return) is recorded as ``parked``
    rather than ``success`` — capturing the sentinel's ``interaction_id`` as the
    row's lifecycle-correlation key. A dispatch that raises never reaches ``observe``
    — the chokepoint records ``aborted`` (a cancellation) or ``error`` (any other
    escape) from the exception path."""

    def __init__(self) -> None:
        self.recorded = False
        self.outcome: RunOutcome = "success"
        self.interaction_id: str | None = None

    def observe(self, result: object) -> None:
        if isinstance(result, SuspendedInteraction):
            self.outcome = "parked"
            self.interaction_id = result.interaction_id
        else:
            self.outcome = "success"
            self.interaction_id = None


@asynccontextmanager
async def record_outermost_preset_run(preset_name: str, version: int) -> AsyncIterator[RunRecord]:
    """Write one runs-index row around an outermost registered-preset dispatch.

    On enter: mint a ``run_id``, read the ambient attribution's ``user_id`` /
    ``session_id`` and the ambient resume origin (the parked interaction id when this
    dispatch is a continuation fire, else NULL), sample the trace id, and INSERT the
    ``running`` START row. On exit: UPDATE the terminal outcome — ``aborted`` if a
    cancellation escaped the block, ``error`` if anything else raised, else the
    observed outcome (``success`` / ``parked``, a park carrying its interaction id) —
    with ``ended_at`` and a backfilled trace id. A no-op (still yields a handle) when
    the store is OFF or the START write fails."""
    record = RunRecord()
    if not component_store_configured(SKELETON_COMPONENT):
        yield record
        return

    store = get_run_index_store()
    run_id = secrets.token_urlsafe(16)
    attribution = get_run_attribution()
    user_id = attribution.user_id if attribution is not None else None
    session_id = attribution.session_id if attribution is not None else None
    started = datetime.now(UTC)
    start_trace_id = _safe_trace_id()

    started_ok = False
    try:
        await store.insert_start(
            run_id,
            preset_name,
            version,
            trace_id=start_trace_id,
            user_id=user_id,
            session_id=session_id,
            interaction_id=get_resume_origin(),
            started_at=started.isoformat(),
        )
        started_ok = True
    except Exception:
        # Enumeration must never break a run: a failed START write degrades to an
        # unrecorded run, logged loudly, and the dispatch proceeds untouched.
        logger.warning(
            "runs-index: failed to record run start for preset %r; run left unrecorded", preset_name, exc_info=True
        )

    if not started_ok:
        # Yield OUTSIDE the ``except`` above: a ``yield`` inside a handler would leave
        # the swallowed START error active, so any exception the dispatch raises would
        # be chained onto it as ``__context__`` — corrupting the run's error (and its
        # secret-redaction invariants). Recording nothing terminal for an unrecorded run.
        yield record
        return

    record.recorded = True
    escape_outcome: RunOutcome | None = None
    try:
        yield record
    except asyncio.CancelledError:
        # A cancellation (user abort, shutdown drain, epoch retire) is a distinct
        # terminal ``aborted`` — cancelled is not failed — and MUST propagate
        # immediately: recorded in the ``finally`` below, never swallowed here.
        escape_outcome = "aborted"
        raise
    except BaseException:
        # Any other escape (tool failure, SystemExit) is a terminal ``error`` for
        # this run — recorded before the exception propagates.
        escape_outcome = "error"
        raise
    finally:
        outcome: RunOutcome = escape_outcome if escape_outcome is not None else record.outcome
        ended = datetime.now(UTC)
        # Backfill the trace id only when START missed it — a body that opened its trace
        # after START may make it sampleable now.
        end_trace_id = None if start_trace_id is not None else _safe_trace_id()
        try:
            # On the ``aborted`` path this write runs while the CancelledError is
            # propagating — a best-effort UNSHIELDED attempt, the tool-run drain
            # idiom (a drain cancels each task exactly once, then waits, so the
            # await normally completes). Deliberately no shield: recording must
            # never block a cancellation. If a second cancellation lands mid-write,
            # the ``except Exception`` below does not catch it, the CancelledError
            # propagates, and the row honestly stays ``running`` (the same
            # crash-interrupted posture an abrupt process death leaves).
            await store.update_outcome(
                run_id, outcome, ended.isoformat(), trace_id=end_trace_id, interaction_id=record.interaction_id
            )
        except Exception:
            logger.warning(
                "runs-index: failed to record terminal outcome %r for run %s", outcome, run_id, exc_info=True
            )
