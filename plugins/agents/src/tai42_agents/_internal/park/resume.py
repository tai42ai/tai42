"""The ``agent_resume`` continuation the flow-blind platform fires, and the outcome conversion.

:func:`agent_resume` buffers one answer into its super-step barrier and, when the barrier is
complete, wins the single drive lease and drives the parked run to its next outcome. At a clean
terminal it cascades the OUTERMOST outcome up — if this run was a nested run of another driver, it
fires the ancestor's captured chain-delivery tool and returns that result. A resumed run that
RAISES reaches its failed terminal the same way (:func:`_fire_failed_terminal`): a chained park
fires its routing ``failed``, an unchained one finalizes its :class:`RunFailed` and raises
:class:`RunTerminalFailed`. It fires NO door completion: the platform captures, binds and delivers
the resumed run's outcome to the run's own address. :func:`to_contract_outcome` maps what the
drive returns to the contract types the faces return. :data:`AGENT_RESUME_TOOL_NAME` names the
bound continuation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from tai42_contract.app import tai42_app
from tai42_contract.conversations import TurnSupersededError
from tai42_contract.interactions import (
    ChainedResume,
    ParkRunTakenDownError,
    ResumeBuffered,
    RunFailed,
    RunTerminalFailed,
    SuspendedInteraction,
    get_chained_resume,
    get_resume_continuation_tool,
    reset_chained_resume,
    set_chained_resume,
)
from tai42_kit.interactions.park_adoption import fire_terminal_chain, supersede_chain_notice
from tai42_kit.interactions.park_index import Barrier, BarrierNotFoundError, DriveLease, LeaseLostError

from tai42_agents._internal.park.capability import chained_resume_from_entry
from tai42_agents._internal.park.errors import (
    AgentResumeBarrierNotFoundError,
    AgentResumeInterruptNotPendingError,
    AgentResumeParkEntryNotFoundError,
    WorkspaceLeaseHeldError,
)
from tai42_agents._internal.park.park_binding import agents_park_index

logger = logging.getLogger(__name__)

# Registered name of the hidden tool that resumes an async-parked agent run. Bound as the
# driver continuation (``set_resume_continuation_tool``) so a flow-blind platform
# ``ask(async)`` stamps it onto the parked interaction and later invokes it with
# ``{interaction_id, ask_result}`` to resume auto-pilot. Must equal the bound tool name.
AGENT_RESUME_TOOL_NAME: Final[str] = "agent_resume"


def is_suspended_receipt(result: Any) -> bool:
    """Whether a drive outcome is a re-park (the run parked again) rather than a clean terminal answer.

    A park crosses as a :class:`SuspendedInteraction` typed value, recognised by TYPE (never a
    status-keyed dict). The discriminator for whether the terminal cascade fires now or the outcome
    is carried forward to the new park entry as a still-suspended re-park.
    """
    return isinstance(result, SuspendedInteraction)


def to_contract_outcome(value: Any, *, raise_failed: bool = False) -> Any:
    """Map what :func:`agent_resume` returns to what a resume face returns, reading types only.

    The two driver-facing tools (:func:`~tai42_agents._internal.park.resume_tool.agent_resume_tool`
    and :func:`~tai42_agents._internal.park.chain.deliver_chained_park`) apply this to what
    :func:`agent_resume` returns. A :class:`RunFailed` — this run's own failed terminal, a replayed
    failed record, or a failure that came UP a cross-driver chain fire — RAISES
    :class:`RunTerminalFailed` carrying its outcome WHOLE and its participant-facing ``reply`` when
    ``raise_failed`` is set (the PLATFORM-continuation face, so the platform's delivery ladder
    delivers FAILED with that reply); a CHAIN-fire face
    leaves it unset so the failure RETURNS by type into the firing run's drive. Every other value (a
    plain terminal, a :class:`SuspendedInteraction`, a :class:`ResumeBuffered`, ``None`` for a
    benign detached landing) is returned as is. No value is ever read by a status word inside it.
    """
    if isinstance(value, RunFailed) and raise_failed:
        raise RunTerminalFailed(value.outcome, reply=value.reply)
    return value


def _aborted_outcome(reason: str) -> RunFailed:
    """The failed terminal of a run torn down mid-drive (a supersede, a cancellation, a kill).

    ``reason`` names the abort kind for the operator trace; the outcome is person-data-free.
    """
    return RunFailed(outcome={"status": "aborted", "reason": reason})


def _failed_run_outcome(exc: BaseException) -> dict[str, Any]:
    """The agents' own failed outcome for a resumed run that raised: the exception's type and text."""
    return {"status": "error", "error_type": type(exc).__name__, "error": str(exc)}


async def _replay_resolution(entry: Mapping[str, Any]) -> Any:
    """Replay what a resolved super-step stored, or ``None`` for a detached tombstone.

    A lapped redelivery of a losing sibling's still-open due record lands on the tombstone the
    first drive left and replays its record's value: a terminal, a re-park, or a :class:`RunFailed`
    the platform face raises as :class:`RunTerminalFailed`. A detached tombstone (a chained key a
    drive claimed but never parked on) has no record and is the benign no-op landing; a resolved
    tombstone whose record is gone raises. An aborted record is what a resume ALREADY in flight when
    the run was killed lands on; the platform delivers nothing for it (the kill revoked its ticket).
    """
    record = await agents_park_index().read_tombstone_resolution(entry)
    return None if record is None else record.value


async def agent_resume(interaction_id: str, answer: Any) -> Any:
    """Buffer one answer into its super-step barrier and, at the LAST of the M answers, drive it once.

    Drives the parked run to its next OUTCOME — exactly once — and returns it. This is the driver
    continuation the flow-blind platform invokes when an async ``ask`` is answered (or expires): it
    carries ``{interaction_id, ask_result}`` and buffers whatever value it is given; an ask's
    value arrives as the ``AskAnswer`` JSON the platform composed. The park index
    reverses the id to the parked thread, the super-step, and the interrupt the answers target.
    The answer is buffered idempotently in the super-step barrier; a super-step cannot advance
    until every interaction in it holds a resume value, so partial drives buy no progress and are
    skipped. When the barrier is complete, one
    caller wins the drive lease and feeds ALL M answers into the graph in a single resume. A
    redelivered answer is a no-op; a still-pending sibling is never re-driven. An expiry (the
    ``AskAnswer`` JSON whose ``answer`` is ``EXPIRY_ANSWER``) buffers the same way.
    Single-suspend is the M=1 degenerate case.

    Returns (the faces map it with :func:`to_contract_outcome`):

    * a :class:`ResumeBuffered` naming the still-unanswered ids while siblings are outstanding;
    * a :class:`SuspendedInteraction` when the drive parked again;
    * the OUTERMOST run's outcome when this caller won the drive and drove it — the leaf's own
      terminal when this run is outermost, or the return of the ancestor's captured chain-delivery
      fire (which cascades the outermost outcome UP) when it was a nested run; a resumed run that
      RAISED fires that chain ``failed`` and returns what the fire returned;
    * a REPLAY of the stored resolution when the park key holds a resolved tombstone (a lapped
      redelivery of a losing sibling's orphaned due-record); ``None`` for a detached tombstone.

    An UNCHAINED resumed run that raised finalizes its :class:`RunFailed` and RAISES
    :class:`RunTerminalFailed` (a cancellation is re-raised as itself after the finalize), so the
    platform delivers FAILED once and clears the due record. Raises loudly on an interaction with
    no park entry, an interrupt the super-step does not expect, a missing barrier, a drive lease
    another worker holds (the kit ``DriveInProgressError``, so the platform keeps the due record
    and redelivers), and the kit ``LeaseLostError`` when a kill reclaimed the lease
    during the drive (no chain routing is fired; the redrive lands on the kill's resolution). A
    raise of the PREPARE step (an entry or the agent missing, no ``aresume_park`` face, an index
    read failing) is a plain raise: the run was never resumed, the lease is released and the index
    stays live for the redelivery.
    """
    index = agents_park_index()
    entry = await index.read_entry(interaction_id)
    if entry is None:
        raise AgentResumeParkEntryNotFoundError(interaction_id)
    if index.tombstone_kind(entry) != "live":
        return await _replay_resolution(entry)

    thread_id = entry["thread_id"]
    superstep_id = entry["superstep_id"]
    try:
        progress = await index.buffer(thread_id, superstep_id, interaction_id, answer)
    except KeyError as exc:
        raise AgentResumeInterruptNotPendingError(interaction_id, entry["interrupt_id"]) from exc
    except BarrierNotFoundError as exc:
        raise AgentResumeBarrierNotFoundError(thread_id, superstep_id) from exc
    if progress.remaining:
        return ResumeBuffered(remaining_ids=progress.remaining)

    return await _drive_superstep(entry, thread_id, superstep_id)


@dataclass(frozen=True)
class _ResumedRun:
    """How the resumed graph ended: its ``result``, or the ``failed`` terminal its raise became."""

    result: Any = None
    failed: RunFailed | None = None
    # A supersede or a cancellation tore the run down mid-drive.
    aborted: bool = False
    # The cancellation to re-raise once the failed terminal is recorded.
    cancelled: asyncio.CancelledError | None = None


async def _drive_superstep(entry: Mapping[str, Any], thread_id: str, superstep_id: str) -> Any:
    """Drive a completed super-step once under its drive lease and record how it resolved.

    The drive runs under the routing the park captured, so its re-park carries it forward and its
    terminal re-enters the same ancestor — unless an inline resumer of a DIFFERENT driver chains
    the run (:func:`_resumer_takeover`): then the drive runs under the resumer's routing, its
    re-park and terminal reach the resumer, and the routing the park captured is fired ``failed``
    so a caller still parked on it ends loudly instead of waiting on a key this run no longer fires.
    """
    index = agents_park_index()
    async with index.claim(thread_id, superstep_id) as lease:
        barrier = await index.read_barrier(thread_id, superstep_id)
        if barrier is None:
            raise AgentResumeBarrierNotFoundError(thread_id, superstep_id)
        members = list(barrier.expected)
        resume_park, resume_map = await _prepare_resume(entry, barrier)
        stored = chained_resume_from_entry(entry)
        takeover = _resumer_takeover(stored)
        drive_routing = takeover if takeover is not None else stored
        resumed = await _run_resumed(entry, drive_routing, resume_park, resume_map)

        # On a take-over the resumer is waiting INLINE on this very drive (it holds no park on its
        # own key yet), so the run's terminal is RETURNED to it rather than fired; and the routing
        # the park captured is superseded — ended ``failed`` so a caller still parked on it ends
        # loudly. Otherwise the drive's own captured routing carries the terminal, as before.
        if takeover is not None:
            if stored is not None:
                await _fire_supersede(stored)
            terminal_routing: ChainedResume | None = None
        else:
            terminal_routing = drive_routing

        if resumed.failed is not None:
            epilogue = _fire_failed_terminal(lease, members, resumed.failed, terminal_routing, aborted=resumed.aborted)
            if resumed.cancelled is not None:
                await asyncio.shield(epilogue)
                raise resumed.cancelled
            outcome = await epilogue
            if terminal_routing is None:
                raise RunTerminalFailed(resumed.failed.outcome, reply=resumed.failed.reply)
            return outcome

        # A kill can reclaim a lease that lapsed during the drive (a heartbeat that
        # could not renew): re-check before the terminal chain fire, so a super-step this drive no
        # longer owns fires no chain routing.
        if not await lease.holds():
            raise LeaseLostError(thread_id, superstep_id)
        result = resumed.result
        if is_suspended_receipt(result):
            # The run parked again: this super-step resolves ``suspended`` and no cascade fires.
            await index.finalize(lease, member_ids=members, resolution="suspended", value=result)
            return result
        result = await _deliver_terminal(result, terminal_routing, stored)
        await index.finalize(lease, member_ids=members, resolution="terminal", value=result)
        return result


async def _deliver_terminal(result: Any, terminal_routing: ChainedResume | None, stored: ChainedResume | None) -> Any:
    """The successful terminal as the drive records and returns it.

    A NESTED run's terminal re-enters the waiting ancestor; the fire's return IS the outermost
    outcome, stored so a redrive replays it. A take-over returns the terminal inline, settled by the
    routing the park captured. A run with no routing returns its terminal unchanged.
    """
    if terminal_routing is not None:
        return await fire_terminal_chain(terminal_routing, result)
    if stored is not None:
        return await tai42_app.interactions.settle_chained_terminal(stored.chain_key, result)
    return result


def _resumer_takeover(stored: ChainedResume | None) -> ChainedResume | None:
    """The resumer's chain routing when an inline resume by a DIFFERENT driver takes the call over.

    A caller that resumes this run INLINE under its OWN resume continuation (not the agents' own
    ``agent_resume``) and a chain routing of its own — one whose key differs from the routing the
    park captured — takes the call over. Returns that routing, or ``None`` for this run's own
    out-of-band resume (nothing ambient is bound) or a same-driver resume (an adopter the engine
    links through its own parent list, which changes nothing here).
    """
    ambient = get_chained_resume()
    if ambient is None:
        return None
    if get_resume_continuation_tool() == AGENT_RESUME_TOOL_NAME:
        return None
    stored_key = stored.chain_key if stored is not None else None
    if ambient.chain_key == stored_key:
        return None
    return ambient


async def _fire_supersede(routing: ChainedResume) -> None:
    """Fire the routing a take-over superseded ``failed``, so a caller still parked on it ends loudly."""
    tool, payload = supersede_chain_notice(routing)
    await tai42_app.tools.run_tool(tool, payload, continues_chain=routing.asked_by)


async def _run_resumed(
    entry: Mapping[str, Any],
    routing: ChainedResume | None,
    resume_park: Callable[..., Awaitable[Any]],
    resume_map: dict[str, dict[str, Any]],
) -> _ResumedRun:
    """Resume the parked graph once; a raise of the resumed run is its failed terminal.

    The captured cross-driver chain routing is re-bound for the drive, so a re-park re-persists a
    fresh index carrying it forward — the terminal always reaches the same ancestor. Three raises
    are re-raised unchanged, as they end no run: the kit ``LeaseLostError`` (a kill owns the
    super-step), :class:`WorkspaceLeaseHeldError` (another drive holds the run's workspace, so
    this resume never started and a redelivery retries it) and
    :class:`~tai42_contract.interactions.ParkRunTakenDownError` (the run was taken down: end no run
    here, finalize nothing — the platform's kill ends the super-step). Every other raise becomes the
    run's :class:`RunFailed`.
    """
    chain_token = set_chained_resume(routing)
    try:
        result = await resume_park(
            rebuild_kwargs=entry["rebuild_kwargs"], thread_id=entry["thread_id"], resume_map=resume_map
        )
    except (LeaseLostError, WorkspaceLeaseHeldError, ParkRunTakenDownError):
        raise
    except asyncio.CancelledError as exc:
        return _ResumedRun(failed=_aborted_outcome(type(exc).__name__), aborted=True, cancelled=exc)
    except TurnSupersededError as exc:
        return _ResumedRun(failed=_aborted_outcome(type(exc).__name__), aborted=True)
    except RunTerminalFailed as exc:
        return _ResumedRun(failed=RunFailed(outcome=exc.outcome, reply=exc.reply))
    except Exception as exc:
        logger.error(
            "resumed agent run of thread %r super-step %r raised; ending it FAILED",
            entry["thread_id"],
            entry["superstep_id"],
            exc_info=True,
        )
        return _ResumedRun(failed=RunFailed(outcome=_failed_run_outcome(exc)))
    finally:
        reset_chained_resume(chain_token)
    return _ResumedRun(result=result)


async def _fire_failed_terminal(
    lease: DriveLease,
    member_ids: Sequence[str],
    failed: RunFailed,
    routing: ChainedResume | None,
    *,
    aborted: bool,
) -> Any:
    """End a run's parked super-step as a failed terminal under ``lease`` (held); return the outermost outcome.

    Fires only while ``lease`` still holds the super-step — a kill that reclaimed it
    has already finalized it ``aborted``, so firing would race it (raises the kit
    ``LeaseLostError``). A CHAINED terminal (``routing`` set) fires that routing ``failed`` with
    ``failed``'s outcome; the waiting ancestor's chain-delivery face resumes it with the failure
    and returns the outermost outcome (a :class:`RunFailed` when it failed too, its own answer when
    it handled the failure, a re-park), which is finalized ``terminal`` and returned; a raise of
    the fire propagates with nothing finalized. A routing of ``None`` (an unchained park, or a
    take-over whose terminal returns to the inline resumer) finalizes ``failed`` itself (``aborted``
    when the run was torn down mid-drive, else ``terminal``) and returns it.
    """
    index = agents_park_index()
    if not await lease.holds():
        raise LeaseLostError(lease.thread_id, lease.superstep)
    if routing is not None:
        outcome = await fire_terminal_chain(routing, failed)
        await index.finalize(lease, member_ids=member_ids, resolution="terminal", value=outcome)
        return outcome
    await index.finalize(lease, member_ids=member_ids, resolution="aborted" if aborted else "terminal", value=failed)
    return failed


async def _prepare_resume(
    entry: Mapping[str, Any], barrier: Barrier
) -> tuple[Callable[..., Awaitable[Any]], dict[str, dict[str, Any]]]:
    """The agent's ``aresume_park`` face and the resume map of a completed barrier.

    The map groups the buffered answers by the interrupt each targets: every park entry stores its
    interaction's own interrupt, so a multi-interrupt super-step (parallel subagent parks) feeds
    each interrupt its own ``{interaction_id: answer}`` map in ONE resume. Raises when the agent is
    not registered, exposes no ``aresume_park`` face, or a member's entry is gone.
    """
    agent = tai42_app.agents.get_agent(entry["agent_name"])
    resume_park = getattr(agent, "aresume_park", None)
    if resume_park is None:
        raise RuntimeError(
            f"agent {entry['agent_name']!r} bound the resume continuation but exposes no aresume_park face"
        )
    index = agents_park_index()
    resume_map: dict[str, dict[str, Any]] = {}
    for interaction_id in barrier.expected:
        parked = await index.read_entry(interaction_id)
        if parked is None or index.tombstone_kind(parked) != "live":
            raise AgentResumeParkEntryNotFoundError(interaction_id)
        resume_map.setdefault(parked["interrupt_id"], {})[interaction_id] = barrier.outputs[interaction_id]
    return resume_park, resume_map
