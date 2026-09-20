"""The drive-side park machinery: bind the resume continuation, detach unparked chains, and classify interrupts.

Binds the resume continuation for a run's drive, detaches the chains it never
parked on, and classifies a stopped drive's pending interrupt.

:func:`park_continuation` / :func:`park_step_binding` / :func:`park_drive` bind the resume
continuation (and, for a park-capable drive, the chained-park claims ledger) around a drive;
:func:`bind_resume_per_step` re-enters a binding around each step of a streaming drive;
:func:`detach_dead_chains` tombstones the claims a drive left unparked; :func:`finalize_drive`
turns a pending interrupt into the terminal park/HITL events over :func:`~persist_park`.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any

from langgraph.types import StateSnapshot
from tai42_contract.agent.events import InterruptFinal, StreamEvent, SuspendedFinal
from tai42_contract.interactions import (
    chained_park_claims,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
)

from tai42_agents._internal.park.capability import ParkIdentity
from tai42_agents._internal.park.errors import AgentParkNotHostableError
from tai42_agents._internal.park.index import detach_chained_parks
from tai42_agents._internal.park.middleware import AGENT_PARK_PAYLOAD_KEY
from tai42_agents._internal.park.persist import persist_park
from tai42_agents._internal.park.resume import AGENT_RESUME_TOOL_NAME

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def park_continuation(park: ParkIdentity | None) -> Iterator[None]:
    """Bind the resume continuation for the duration of a park-capable run's drive.

    A flow-blind platform ``ask_user(async)`` raised by a tool this run drives reads the
    bound continuation to stamp ``continuation_tool`` onto the parked interaction, so a
    later answer re-enters through ``agent_resume``. The resume tool name is bound only when
    the run is park-capable AND its face delivers the resume (``bind``); otherwise ``None`` is
    bound — NOT a no-op. Binding ``None`` SHADOWS any ambient resume continuation the caller
    left bound, so a non-park-capable (or streaming) run nested under a park-capable one cannot
    inherit that binding and mint a park it has no way to resume: its async ask sees no bound
    continuation and refuses loudly pre-persist. The continuation is set and reset around each
    drive.

    This is a SYNC context manager binding ONE contextvar and never awaiting, so it can be
    re-entered per drive-step by :func:`bind_resume_per_step` (the binding cannot span a
    generator's yields). The chained-park claims ledger a park-capable drive also needs is a
    WHOLE-DRIVE resource — it accumulates across steps and is reconciled once the drive stops —
    so it is bound alongside this, not folded in: :func:`park_step_binding` composes both for a
    streaming drive, and a coroutine drive binds :func:`~tai42_contract.interactions.chained_park_claims`
    directly around its ``await``.
    """
    name = AGENT_RESUME_TOOL_NAME if park is not None and park.bind else None
    token = set_resume_continuation_tool(name)
    try:
        yield
    finally:
        reset_resume_continuation_tool(token)


@contextlib.contextmanager
def park_step_binding(park: ParkIdentity | None, claims: set[str]) -> Iterator[None]:
    """Per-drive-step binding for a STREAMING drive: the resume continuation and the chained-park claims ledger.

    Both are re-entered around each ``__anext__`` by :func:`bind_resume_per_step`
    and both released before the step's value is yielded, so neither leaks into
    the consumer. The two bindings differ in what they own. The resume continuation is per-step by nature —
    nothing carries across steps. The claims ledger is WHOLE-DRIVE: every step re-binds the ONE
    ``claims`` set the face owns, so a chained key claimed in an early step survives to the
    reconcile the face runs — :func:`detach_dead_chains` over that same set — when the drive
    stops. A non-park-capable run binds ``None`` and claims nothing; its set stays empty.
    """
    with park_continuation(park), chained_park_claims(claims):
        yield


@contextlib.asynccontextmanager
async def park_drive(park: ParkIdentity | None) -> AsyncIterator[None]:
    """Whole-drive park binding for a COROUTINE drive.

    A run face that awaits the drive to a result, never yielding to an external
    consumer. Binds the resume continuation and the chained-park claims ledger for the drive's duration,
    and detaches the dead chains when it stops (whether it returned or raised). Safe to hold
    across the drive's ``await`` points: a coroutine stays in one task, so unlike a streaming
    generator nothing leaks past a yield — a streaming face binds per step via
    :func:`park_step_binding` and detaches around its own loop instead.
    """
    claims: set[str] = set()
    with park_continuation(park), chained_park_claims(claims):
        try:
            yield
        finally:
            await detach_dead_chains(claims)


async def detach_dead_chains(claims: set[str]) -> None:
    """Tombstone the chained keys this drive claimed but never parked on.

    So a later terminal lands benignly instead of hunting a park that will never
    exist. Failures are logged and swallowed: the drive already has its result (or its exception), and
    a detach that could not run must not replace either — the undetached chain falls back to the
    delivery tool's own at-least-once retry tail. ``CancelledError`` propagates: a drive being
    torn down has no time to write, and swallowing it would fight the cancellation.
    """
    if not claims:
        return
    try:
        await detach_chained_parks(sorted(claims))
    except Exception:
        logger.warning(
            "Agent drive left %d chained call(s) unparked and could not detach them; a later terminal will "
            "retry against an absent park entry until the platform gives up",
            len(claims),
            exc_info=True,
        )


async def bind_resume_per_step[StreamItemT](
    binding: Callable[[], AbstractContextManager[Any]],
    events: AsyncIterator[StreamItemT],
) -> AsyncGenerator[StreamItemT]:
    """Yield from ``events`` with a resume-continuation ``binding`` re-entered around each step.

    So the continuation is bound WHILE a step is computed but never leaks across a
    yield. The binding cannot live in the yielding generator's own ``with`` body: PEP 568 (per-send
    context isolation for async generators) is unimplemented, so a ``ContextVar`` set there lands
    in the CONSUMER's task and persists across every yield — leaking the binding into the consumer,
    and on an abandoned stream stranding it bound forever (its ``Token`` then resets from a foreign
    context, raising on ``aclose``). Entering ``binding`` around each ``__anext__`` scopes the
    contextvar to the step that drives the tool dispatch and resets it before the value is handed
    to the consumer, so the consumer never observes it and a mid-stream ``aclose`` unwinds cleanly.
    ``binding`` is a zero-arg factory (a fresh context manager per step), e.g.
    ``lambda: park_step_binding(park, claims)`` for a park-capable LangGraph drive or
    ``lambda: _resume_continuation(threaded)`` for the ``claude_code`` engine.
    """
    step = events.__aiter__()
    try:
        while True:
            with binding():
                try:
                    item = await step.__anext__()
                except StopAsyncIteration:
                    break
            yield item
    finally:
        aclose = getattr(step, "aclose", None)
        if aclose is not None:
            await aclose()


def collect_pending_interrupts(snapshot: Any) -> list[tuple[str, Any]]:
    """Every pending interrupt in a snapshot as ``(id, value)``, descending into subgraph tasks.

    So a park raised inside a subagent stack is seen too (read with
    ``subgraphs=True``).
    """
    pending: list[tuple[str, Any]] = []

    def _walk(snap: Any) -> None:
        for task in snap.tasks or []:
            pending.extend((item.id, item.value) for item in task.interrupts)
            if isinstance(task.state, StateSnapshot):
                _walk(task.state)

    _walk(snapshot)
    return pending


def _park_interactions(value: Any) -> dict[str, Any] | None:
    """The ``{interaction_id: expiry}`` map of a park interrupt's value, or ``None`` for a plain HITL interrupt.

    The plain-HITL case is recognized by the reserved value shape, never a name.
    """
    if isinstance(value, dict) and AGENT_PARK_PAYLOAD_KEY in value:
        return value[AGENT_PARK_PAYLOAD_KEY]["interactions"]
    return None


async def finalize_drive(
    agent: Any,
    config: dict[str, Any],
    interrupt_on: dict[str, Any] | None,
    park: ParkIdentity | None,
) -> list[StreamEvent]:
    """Classify a stopped drive's pending interrupt into the terminal park/HITL events.

    A pending interrupt whose value carries the reserved park payload is a PARK: persist
    the durable park index and surface one :class:`SuspendedFinal`. Any other pending
    interrupt is a HITL pause, surfaced as :class:`InterruptFinal` (today's behavior). The
    state read is skipped entirely unless the run can pause — ``interrupt_on`` is set, or
    the run is park-capable and binds — so a plain run pays no extra read.

    Every distinct park interrupt pending at once (e.g. two parallel task-tool subagents that
    each async-ask) is collected into ONE super-step: a single durable index over the union of
    all their interactions and one :class:`SuspendedFinal`, resumed by feeding each interrupt
    its own answers in one langgraph resume map. A park interrupt with no park identity to
    record it against raises loudly rather than stranding the park.
    """
    if not interrupt_on and (park is None or not park.bind):
        # Skipping the read cannot swallow a park: the park hook interrupts only for a marker
        # whose resume owner is the continuation bound here, and nothing binds one unless the
        # run is park-capable AND delivers the resume — exactly the case excluded here. A run
        # that cannot host a park gets the hook's model-visible refusal instead of an
        # interrupt, so there is no pending park interrupt to classify.
        return []

    snapshot = await agent.aget_state(config, subgraphs=True)
    pending = collect_pending_interrupts(snapshot)
    if not pending:
        return []

    classified = [(iid, value, _park_interactions(value)) for iid, value in pending]
    parks = [(iid, interactions) for iid, _value, interactions in classified if interactions is not None]
    hitl = [(iid, value) for iid, value, interactions in classified if interactions is None]

    events: list[StreamEvent] = []
    if parks:
        if park is None or not park.bind:
            raise AgentParkNotHostableError(
                "a run produced an async-park interrupt with no park identity bound — "
                "an async ask parked without a durable resume path"
            )
        # The receipt reports the deadlines the index actually holds — a chained key's
        # inherited horizon is clamped at persist, and a caller reading the receipt must see
        # the same date the park will really be kept to.
        union = await persist_park(park, parks)
        events.append(
            SuspendedFinal(
                interaction_ids=sorted(union),
                thread_id=park.thread_id,
                expiry_at=_earliest_expiry(union),
            )
        )
    for interrupt_id, value in hitl:
        events.append(InterruptFinal(interrupt_id=interrupt_id, payload=value))
    return events


def _earliest_expiry(interactions: dict[str, Any]) -> str | None:
    """The earliest park deadline across the siblings (ISO-8601), or ``None`` when none carried one."""
    deadlines = [v for v in interactions.values() if v is not None]
    if not deadlines:
        return None
    return min(deadlines)
