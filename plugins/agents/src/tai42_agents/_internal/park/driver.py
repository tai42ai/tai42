"""Park capability, the drive-side park finalizer, and the ``agent_resume`` driver.

Three concerns behind a park-capable agent run, over the durable park index (one
entry per interaction, a per-super-step barrier, a single-winner drive lease):

* :func:`build_park_identity` — decide whether a run can be parked (durable checkpoint
  provider AND a fully JSON-serializable rebuild identity AND a configured park Redis)
  and, if so, capture the rebuild identity.
* :func:`park_continuation` + :func:`finalize_drive` — the drive wrapper: bind the resume
  continuation for the run's duration (run face only), then after the drive stops
  classify the pending interrupt — a park (persist the durable index, surface a suspended
  outcome) or a plain HITL interrupt (today's behavior).
* :func:`agent_resume` + :func:`_drive_completed_barrier` — the continuation the
  flow-blind platform fires with ``{interaction_id, answer}``: buffer into the super-step
  barrier, win the single drive lease, rebuild the same compiled graph on the same thread,
  and resume the park interrupt BY ID with all M answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from langgraph.types import StateSnapshot
from tai42_contract.agent.events import InterruptFinal, StreamEvent, SuspendedFinal
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    reset_park_completion,
    reset_resume_continuation_tool,
    set_park_completion,
    set_resume_continuation_tool,
)
from tai42_kit.llm.settings import llm_provider_settings

from tai42_agents._internal.park.errors import (
    AgentResumeBarrierNotFoundError,
    AgentResumeDriveInProgressError,
    AgentResumeInterruptNotPendingError,
    AgentResumeParkEntryNotFoundError,
    ParkExpiryExceedsRetentionError,
)
from tai42_agents._internal.park.index import (
    barrier_ttl_seconds,
    buffer_answer,
    compute_superstep_id,
    finalize_resolved_superstep,
    heartbeat_drive_claim,
    is_resolved_tombstone,
    persist_superstep,
    read_barrier,
    read_park_entry,
    release_claim,
    try_claim_drive,
)
from tai42_agents._internal.park.middleware import AGENT_PARK_PAYLOAD_KEY
from tai42_agents.settings import agents_park_redis_settings

logger = logging.getLogger(__name__)

# Registered name of the hidden tool that resumes an async-parked agent run. Bound as the
# driver continuation (``set_resume_continuation_tool``) so a flow-blind platform
# ``ask_user(async)`` stamps it onto the parked interaction and later invokes it with
# ``{interaction_id, answer}`` to resume auto-pilot. Must equal the bound tool name.
AGENT_RESUME_TOOL_NAME: Final[str] = "agent_resume"

# Checkpoint providers that survive a cross-worker resume: a parked run's paused graph
# must be readable by whatever worker later fires its continuation. ``memory``/``sqlite``
# are single-process and disqualified.
DURABLE_CHECKPOINT_PROVIDERS: Final[frozenset[str]] = frozenset({"redis", "postgres"})


class ParkIdentity:
    """Everything a park-capable run needs to record a park and rebuild it on resume.

    PROVIDER-FREE: the identity carries no LangGraph fact (no checkpoint provider, no
    recursion limit). Every engine-specific rebuild datum lives inside ``rebuild_kwargs``
    (a JSON-serializable blob the engine's own ``aresume_park`` reads back), so BOTH the
    LangGraph engines and ``claude_code`` record and resume a park through this one shape.

    ``retention_bound`` is CALLER-COMPUTED: the latest wall-time every store backing this
    parked run is guaranteed to still hold it (a LangGraph engine passes its checkpoint
    horizon, the durable-workspace engines the min of checkpoint and workspace). ``None``
    means keep-forever (unbounded). The generic persist path gates each ask deadline
    against it without touching any provider.

    ``bind`` gates whether an async ask under this run may park at all: it binds the resume
    continuation so a parked ask re-enters through ``agent_resume``. Both the ``run`` and
    ``astream`` faces return the park RECEIPT to their caller and resume out of band. Whether
    the resumed run's FINAL text is delivered anywhere is a SEPARATE matter of the completion
    tool: with none bound the resumed run's side effects are its only product and the final
    text is delivered nowhere; a caller that needs the answer must invoke through a
    completion-bound door (e.g. the conversation turn). A run with no resume path bound refuses
    an async ask loudly pre-persist rather than parking with no way to resume.
    """

    __slots__ = (
        "agent_name",
        "bind",
        "completion_tool",
        "rebuild_kwargs",
        "retention_bound",
        "thread_id",
    )

    def __init__(
        self,
        *,
        agent_name: str,
        thread_id: str,
        rebuild_kwargs: dict[str, Any],
        bind: bool,
        completion_tool: str | None = None,
        retention_bound: datetime | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.thread_id = thread_id
        self.rebuild_kwargs = rebuild_kwargs
        self.bind = bind
        # The registered tool a clean terminal drive fires with the final answer, so a
        # deferred response is delivered out of band. ``None`` = the driver's caller
        # receives the resumed result directly (the run face), no completion fire.
        self.completion_tool = completion_tool
        # The latest wall-time every store backing this park is guaranteed to still hold it;
        # ``None`` = keep-forever. Gated against each ask deadline at persist time.
        self.retention_bound = retention_bound


def _thread_id(config: dict[str, Any]) -> str | None:
    return config.get("configurable", {}).get("thread_id")


def _min_horizon(left: datetime | None, right: datetime | None) -> datetime | None:
    """The nearer of two retention horizons, treating ``None`` as unbounded (keep-forever)
    on that side — so the min of ``None`` and a datetime is the datetime."""
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def build_park_identity(
    *,
    agent_name: str,
    config: dict[str, Any],
    checkpoint_provider: str | None,
    has_live_tools: bool,
    rebuild_kwargs: dict[str, Any],
    recursion_limit: int | None,
    bind: bool,
    completion_tool: str | None = None,
    extra_retention_horizon: datetime | None = None,
) -> ParkIdentity | None:
    """Capture the park identity for a LangGraph run, or ``None`` when it cannot be parked.

    A LangGraph-only convenience: it folds the LangGraph facts INWARD (the resolved durable
    checkpoint provider and the ``recursion_limit`` are pinned into ``rebuild_kwargs``, which
    the engine's ``aresume_park`` reads back) and constructs the provider-free
    :class:`ParkIdentity` with a computed ``retention_bound``. ``claude_code`` does not use
    this helper — it constructs a :class:`ParkIdentity` directly (no LangGraph checkpoint).

    A run is park-capable only when its paused graph can be reached and rebuilt by a
    later, possibly different, worker: the checkpoint provider must be durable, the park
    index Redis must be configured, and the rebuild identity must be fully
    JSON-serializable (a run carrying live ``tools=`` cannot be rebuilt from names, so it
    is not park-capable — an async ask under it dies loudly pre-persist, zero state).

    ``checkpoint_provider`` is the caller's value or ``None``; the effective provider is
    resolved here so durability is judged (and the resume recompiles) against the concrete
    provider, never a defaulted-away one. ``retention_bound`` is the checkpoint retention
    horizon narrowed by ``extra_retention_horizon`` (a durable-workspace engine passes its
    volume horizon so the bound is ``min(checkpoint, workspace)``; ``None`` = no extra
    bound, so the bound is the checkpoint horizon alone)."""
    resolved_provider = checkpoint_provider or llm_provider_settings().checkpoint
    if resolved_provider not in DURABLE_CHECKPOINT_PROVIDERS:
        return None
    if agents_park_redis_settings().redis_url is None:
        # No durable park index to record the park into — refuse capability so the async
        # ask refuses loudly pre-persist rather than parking into a store that cannot hold
        # it (a half-park with no way to resume).
        return None
    if has_live_tools:
        return None
    thread_id = _thread_id(config)
    if thread_id is None:
        return None
    try:
        json.dumps(rebuild_kwargs)
    except (TypeError, ValueError):
        # A live object slipped into the rebuild identity — not rebuildable on a fresh
        # worker, so not park-capable.
        return None
    # Pin the LangGraph facts into the rebuild identity so the resume recompiles over the
    # same checkpointer and step bound the park was written under. The provider-free
    # identity carries neither; the engine's ``aresume_park`` reads them back out.
    pinned = {**rebuild_kwargs, "checkpoint_provider": resolved_provider, "recursion_limit": recursion_limit}
    retention_bound = _min_horizon(_checkpoint_retention_horizon(resolved_provider), extra_retention_horizon)
    return ParkIdentity(
        agent_name=agent_name,
        thread_id=thread_id,
        rebuild_kwargs=pinned,
        bind=bind,
        completion_tool=completion_tool,
        retention_bound=retention_bound,
    )


@contextlib.contextmanager
def park_continuation(park: ParkIdentity | None) -> Iterator[None]:
    """Bind the resume continuation for the duration of a park-capable run's drive.

    A flow-blind platform ``ask_user(async)`` raised by a tool this run drives reads the
    bound continuation to stamp ``continuation_tool`` onto the parked interaction, so a
    later answer re-enters through ``agent_resume``. Bound only when the run is
    park-capable AND its face delivers the resume (``bind``); a no-op otherwise, so a
    non-park-capable or streaming run's async ask refuses loudly pre-persist. The
    continuation is set and reset around each drive."""
    if park is None or not park.bind:
        yield
        return
    token = set_resume_continuation_tool(AGENT_RESUME_TOOL_NAME)
    try:
        yield
    finally:
        reset_resume_continuation_tool(token)


def _collect_pending_interrupts(snapshot: Any) -> list[tuple[str, Any]]:
    """Every pending interrupt in a snapshot as ``(id, value)``, descending into subgraph
    tasks so a park raised inside a subagent stack is seen too (read with
    ``subgraphs=True``)."""
    pending: list[tuple[str, Any]] = []

    def _walk(snap: Any) -> None:
        for task in snap.tasks or []:
            for item in task.interrupts:
                pending.append((item.id, item.value))
            if isinstance(task.state, StateSnapshot):
                _walk(task.state)

    _walk(snapshot)
    return pending


def _park_interactions(value: Any) -> dict[str, Any] | None:
    """The ``{interaction_id: expiry}`` map of a park interrupt's value, or ``None`` when
    the interrupt is a plain HITL interrupt (recognized by the reserved value shape, never
    a name)."""
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
    record it against raises loudly rather than stranding the park."""
    if not interrupt_on and (park is None or not park.bind):
        return []

    snapshot = await agent.aget_state(config, subgraphs=True)
    pending = _collect_pending_interrupts(snapshot)
    if not pending:
        return []

    classified = [(iid, value, _park_interactions(value)) for iid, value in pending]
    parks = [(iid, interactions) for iid, _value, interactions in classified if interactions is not None]
    hitl = [(iid, value) for iid, value, interactions in classified if interactions is None]

    events: list[StreamEvent] = []
    if parks:
        if park is None or not park.bind:
            raise RuntimeError(
                "a run produced an async-park interrupt with no park identity bound — "
                "an async ask parked without a durable resume path"
            )
        await persist_park(park, parks)
        union: dict[str, Any] = {}
        for _interrupt_id, interactions in parks:
            union.update(interactions)
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
    """The earliest park deadline across the siblings (ISO-8601), or ``None`` when none
    carried one."""
    deadlines = [v for v in interactions.values() if v is not None]
    if not deadlines:
        return None
    return min(deadlines)


def _checkpoint_retention_horizon(provider: str) -> datetime | None:
    """The latest wall-time a parked graph's checkpoint is guaranteed to still exist, or
    ``None`` when retention is unbounded (keep-forever). LangGraph-only.

    ``redis`` is an idle-TTL saver: the checkpoint is swept ``checkpoint_ttl_minutes`` after
    its last read/write. The park write is itself a write, so it (re)starts that idle clock —
    the deadline comparison against ``now + ttl`` is sound. ``checkpoint_ttl_minutes is None``
    means keep-forever, so no horizon bounds it. ``postgres`` carries no TTL on its saver, so
    it too is keep-forever. Any other provider is not park-capable (never reaches here); an
    unexpected one raises rather than assuming a retention it cannot know."""
    if provider == "postgres":
        return None
    if provider == "redis":
        ttl_minutes = llm_provider_settings().checkpoint_ttl_minutes
        if ttl_minutes is None:
            return None
        return datetime.now(UTC) + timedelta(minutes=ttl_minutes)
    raise RuntimeError(f"unexpected checkpoint provider {provider!r} at park-persist time")


def _gate_expiry_within_retention(retention_bound: datetime | None, interactions: dict[str, Any]) -> None:
    """Refuse the whole super-step LOUDLY if any parked ask outlives the ``retention_bound``
    — a deadline beyond it, or (under a bounded retention) no deadline at all — before a
    single index key is written, so an unresumable park never persists.

    A ``None`` bound (keep-forever) bounds nothing, so every deadline passes."""
    if retention_bound is None:
        return
    for interaction_id, expiry in interactions.items():
        if expiry is None:
            raise ParkExpiryExceedsRetentionError(interaction_id, None, retention_bound)
        if datetime.fromisoformat(expiry) > retention_bound:
            raise ParkExpiryExceedsRetentionError(interaction_id, expiry, retention_bound)


def assert_park_capable(identity: ParkIdentity, *, durable: bool, retention_bound: datetime | None) -> None:
    """The pre-ask structural gate a directly-constructed park (``claude_code``) calls before
    any async ask: raise LOUDLY (pre-persist, zero state) when the run is not park-capable —
    not ``durable`` (its workspace/state is ephemeral), its ``rebuild_kwargs`` is not
    JSON-serializable (so a fresh worker cannot rebuild it), or ``bind`` is false (no resume
    path). ``retention_bound`` is accepted for parity with :func:`persist_park` and to keep
    the caller's computed bound at hand; the gate itself never persists. So an async ask under
    a non-park-capable run dies loudly here rather than half-parking with no way to resume."""
    if not durable:
        raise RuntimeError(
            f"agent {identity.agent_name!r} cannot park an async ask: the run's workspace/state is "
            "ephemeral, so a parked run could never be resumed"
        )
    try:
        json.dumps(identity.rebuild_kwargs)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"agent {identity.agent_name!r} cannot park an async ask: its rebuild identity is not "
            "JSON-serializable, so a fresh worker could not rebuild the run"
        ) from exc
    if not identity.bind:
        raise RuntimeError(
            f"agent {identity.agent_name!r} cannot park an async ask: no resume continuation is bound, "
            "so a parked ask would have no way to re-enter"
        )
    # Retain the caller-computed bound reference (the deadline gate runs in persist_park).
    _ = retention_bound


async def persist_park(identity: ParkIdentity, parks: list[tuple[str, dict[str, Any]]]) -> None:
    """Write the durable park index for a suspended super-step: one park entry per suspended
    interaction plus the super-step barrier all the answers converge on. The provider-free,
    engine-neutral persist seam BOTH the LangGraph engines and ``claude_code`` share.

    ``parks`` is every distinct park interrupt of the super-step, each as
    ``(interrupt_id, {interaction_id: expiry})`` — one entry for a single park, many for
    parallel subagent parks. Their interactions form the super-step's union, keyed by the ONE
    super-step id every continuation routes to; each park entry carries the interrupt that ITS
    interaction targets, so the resume feeds each interrupt its own answers. Each entry also
    carries the agent, thread, and the rebuild identity needed to reconstruct the run (any
    engine-specific fact — a LangGraph checkpoint provider / recursion limit — rides inside
    ``rebuild_kwargs``, never a top-level entry field). Keyed by interaction id, so a re-run
    super-step re-parking the same interaction rewrites identically rather than corrupting the
    index. Entries and the barrier are written in ONE MULTI/EXEC (:func:`persist_superstep`),
    all-or-nothing; each entry's TTL is sized to ITS ask's deadline and the barrier's TTL to
    the LATEST deadline, so the barrier expires no earlier than every entry it must outlive.

    Gated up front by :func:`_gate_expiry_within_retention` against ``identity.retention_bound``:
    one ask whose deadline outlives the retention (or lacks one under a bounded retention) fails
    the whole park with zero index state written."""
    interrupt_by_interaction: dict[str, str] = {}
    union: dict[str, Any] = {}
    for interrupt_id, interactions in parks:
        for interaction_id, expiry in interactions.items():
            union[interaction_id] = expiry
            interrupt_by_interaction[interaction_id] = interrupt_id
    _gate_expiry_within_retention(identity.retention_bound, union)
    superstep_id = compute_superstep_id(union.keys())
    entries: dict[str, dict[str, Any]] = {}
    for interaction_id, _expiry in union.items():
        entries[interaction_id] = {
            "agent_name": identity.agent_name,
            "thread_id": identity.thread_id,
            "superstep_id": superstep_id,
            # The interrupt THIS interaction's answer targets — its own park interrupt, so a
            # multi-interrupt super-step resumes each interrupt by id.
            "interrupt_id": interrupt_by_interaction[interaction_id],
            "rebuild_kwargs": identity.rebuild_kwargs,
            # The completion tool a clean terminal drive fires with the final answer; carried
            # forward onto every entry so a re-park keeps delivering. ``None`` = no completion
            # (the run face's caller receives the resumed result directly).
            "completion_tool": identity.completion_tool,
        }

    expected = dict(union)
    expiries = {
        interaction_id: (datetime.fromisoformat(expiry) if expiry is not None else None)
        for interaction_id, expiry in union.items()
    }
    await persist_superstep(
        entries, identity.thread_id, superstep_id, expected, expiries, barrier_ttl_seconds(expiries.values())
    )


def _is_suspended_receipt(result: Any) -> bool:
    """Whether a drive outcome is a re-park RECEIPT (the run parked again) rather than a
    clean terminal answer — the discriminator for whether a completion fires now or is
    carried forward to the new park entry."""
    return isinstance(result, dict) and result.get("status") == "suspended"


def _completion_id(thread_id: str, superstep_id: str) -> str:
    """A deterministic completion-delivery id for a resolved super-step, so a lease-lapse
    re-drive fires the completion under the SAME id and the delivery ledger dedupes it to one
    record. Derived from the (thread_id, superstep_id) that uniquely name the super-step every
    redelivery re-drives."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tai42:agent-park-completion:{thread_id}:{superstep_id}"))


async def agent_resume(interaction_id: str, answer: Any) -> Any:
    """Buffer one answer into its super-step barrier and, when it is the LAST of the M
    answers, drive the parked run to completion or the next park — exactly once.

    This is the driver continuation the flow-blind platform invokes when an async
    ``ask_user`` is answered (or expires): it carries only ``{interaction_id, answer}``.
    The park index reverses the id to the parked thread, the super-step, and the interrupt
    the answers target. The answer is buffered idempotently in the super-step barrier; a
    super-step cannot advance until every interaction in it holds a resume value, so
    partial drives buy no progress and are skipped. When the barrier is complete, one
    caller wins the drive lease and feeds ALL M answers into the graph in a single
    ``Command(resume={interrupt_id: {interaction_id: answer}})``. A redelivered answer is a
    no-op; a still-pending sibling is never re-driven. An ``answer`` equal to
    ``EXPIRY_ANSWER`` buffers the same way. Single-suspend is the M=1 degenerate case.

    Returns ``{"status": "buffered", "remaining": k}`` while siblings are outstanding, the
    driven run's outcome when this caller won the drive lease and drove, or
    ``{"status": "already_resolved"}`` when the park key holds a resolved tombstone — the
    super-step already drove cleanly and a losing sibling's orphaned due-record is being
    redelivered — so the platform clears the due-record with no alarm instead of storming on a
    key it would read as permanently dropped.

    Raises loudly (never a bare KeyError, never a benign shape) on an interaction with no
    park entry, an interrupt the super-step does not expect or that is no longer pending, a
    missing barrier, or — when the barrier is complete but another live worker holds the
    drive lease — :class:`AgentResumeDriveInProgressError`, so the platform keeps this
    continuation's durable retry ticket for the reaper to redeliver until the live drive
    completes or its lease expires."""
    entry = await read_park_entry(interaction_id)
    if entry is None:
        raise AgentResumeParkEntryNotFoundError(interaction_id)
    if is_resolved_tombstone(entry):
        return {"status": "already_resolved"}

    thread_id = entry["thread_id"]
    superstep_id = entry["superstep_id"]

    try:
        present, total = await buffer_answer(thread_id, superstep_id, interaction_id, answer)
    except KeyError as exc:
        raise AgentResumeInterruptNotPendingError(interaction_id, entry["interrupt_id"]) from exc

    if present < total:
        return {"status": "buffered", "remaining": total - present}

    token = str(uuid.uuid4())
    if not await try_claim_drive(thread_id, superstep_id, token):
        raise AgentResumeDriveInProgressError(thread_id, superstep_id)

    result, expected = await _drive_completed_barrier(entry, thread_id, superstep_id, token)
    # A completion tool was bound when this run parked (a deferred-response delivery path): on a
    # CLEAN TERMINAL drive AWAIT the completion handoff with the final answer BEFORE finalizing
    # the index. The handoff's durable point is the delivery record commit, deduped by the
    # stable completion id, so a crash before it leaves the index LIVE for an idempotent
    # redelivery to re-drive and re-reach the handoff, and a crash after it lands on the
    # tombstone. A re-park carried the tool forward onto the new entry, so it is NOT fired here.
    completion_tool = entry.get("completion_tool")
    if completion_tool is not None and not _is_suspended_receipt(result):
        await tai42_app.tools.run_tool(
            completion_tool,
            {"thread_id": thread_id, "result": result, "completion_id": _completion_id(thread_id, superstep_id)},
        )
    # Tombstone the M park entries and drop the barrier + lease in ONE atomic step, so a crash
    # can never leave a partial tombstone set. A re-park wrote a fresh barrier + entries under
    # its own super-step id, so finalizing this super-step never touches the new one.
    await finalize_resolved_superstep(thread_id, superstep_id, list(expected))
    return result


async def _drive_completed_barrier(
    entry: dict[str, Any],
    thread_id: str,
    superstep_id: str,
    token: str,
) -> tuple[Any, dict[str, Any]]:
    """Drive a completed super-step once, holding the drive lease. Reads the M buffered
    answers, verifies the stored interrupt is still pending, then feeds the whole
    ``{interaction_id: answer}`` map into a single resume. Returns ``(result, expected)`` — the
    drive outcome and the ``{interaction_id: expiry}`` map its caller finalizes over (after any
    clean-terminal completion handoff). On failure releases the lease and leaves the index so a
    retry reclaims and re-drives (LangGraph preserves already-resolved RESUME writes, so a
    repeat drive resumes identically).

    The lease heartbeat starts BEFORE the read-barrier / compile / ``aget_state`` prefix: a
    cold compile that outruns the lease TTL must not lapse the lease while this caller still
    holds the drive, or a redelivery would reclaim and double-drive. The heartbeat is always
    stopped in the ``finally``, its own failure suppressed so it can never mask an
    already-computed drive result."""
    heartbeat = asyncio.create_task(heartbeat_drive_claim(thread_id, superstep_id, token))
    try:
        barrier = await read_barrier(thread_id, superstep_id)
        if barrier is None:
            await release_claim(thread_id, superstep_id, token)
            raise AgentResumeBarrierNotFoundError(thread_id, superstep_id)

        expected: dict[str, Any] = barrier["expected"]
        outputs: dict[str, Any] = barrier["outputs"]

        agent = tai42_app.agents.get_agent(entry["agent_name"])
        resume_park = getattr(agent, "aresume_park", None)
        if resume_park is None:
            await release_claim(thread_id, superstep_id, token)
            raise RuntimeError(
                f"agent {entry['agent_name']!r} bound the resume continuation but exposes no aresume_park face"
            )

        # Group the buffered answers by the interrupt each targets: every park entry stores its
        # interaction's own interrupt, so a multi-interrupt super-step (parallel subagent parks)
        # feeds each interrupt its own ``{interaction_id: answer}`` map in ONE langgraph resume.
        resume_map: dict[str, dict[str, Any]] = {}
        for interaction_id in expected:
            parked = await read_park_entry(interaction_id)
            if parked is None:
                await release_claim(thread_id, superstep_id, token)
                raise AgentResumeParkEntryNotFoundError(interaction_id)
            resume_map.setdefault(parked["interrupt_id"], {})[interaction_id] = outputs[interaction_id]

        # Bind the stored completion tool for the drive's duration so a re-park re-persists
        # a fresh index carrying it forward — the deferred-response delivery survives every
        # re-park. An agent delivers through the bridge thread it runs under, so it carries no
        # opaque completion context. ``None`` (the run face) binds nothing. The entry always
        # carries the ``completion_tool`` field (written on every persisted park entry).
        completion_token = set_park_completion(entry["completion_tool"])
        try:
            result = await resume_park(
                rebuild_kwargs=entry["rebuild_kwargs"],
                thread_id=thread_id,
                resume_map=resume_map,
            )
        except BaseException:
            await release_claim(thread_id, superstep_id, token)
            raise
        finally:
            reset_park_completion(completion_token)
    finally:
        await _stop_drive_heartbeat(heartbeat)

    return result, expected


async def _stop_drive_heartbeat(heartbeat: asyncio.Task) -> None:
    """Cancel and await the lease-heartbeat task. A ``CancelledError`` is the expected stop;
    any OTHER exception the heartbeat raised is suppressed and logged, never re-raised — the
    drive already has its result and a dying heartbeat must not overwrite it with a
    failure."""
    heartbeat.cancel()
    try:
        await heartbeat
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning(
            "Agent drive-lease heartbeat task raised; suppressed so it cannot mask the drive result",
            exc_info=True,
        )
