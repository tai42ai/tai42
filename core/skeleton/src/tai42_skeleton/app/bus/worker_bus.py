"""The worker-bus object: identity/claim state and construction over the publish and subscribe surfaces."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from collections.abc import Callable

from tai42_skeleton.app.bus.env_readers import WorkerBusEnvReadersMixin
from tai42_skeleton.app.bus.exit_latch import ExitHold, ExitLatch, ExitRefusal
from tai42_skeleton.app.bus.life import _Heartbeat
from tai42_skeleton.app.bus.models import (
    WorkerIdentity,
    WorkerKind,
    WorkerRow,
    WorkerState,
    _PresenceValue,
    _utcnow_iso,
)
from tai42_skeleton.app.bus.publish import WorkerBusPublishMixin
from tai42_skeleton.app.bus.subscribe import WorkerBusSubscribeMixin
from tai42_skeleton.app.bus_settings import BusSettings
from tai42_skeleton.config.deployment_env import published_deployment_keys, published_env_readers

logger = logging.getLogger(__name__)

# What a forked job child does instead of ending its member's life: a change it runs is bounded,
# so the change hands its roll, the writer's restart included, to the member it acts for.
_HANDED_TO_THE_MEMBER = "a change it runs hands its roll, the writer's restart included, to that member"


class WorkerBus(WorkerBusPublishMixin, WorkerBusSubscribeMixin, WorkerBusEnvReadersMixin):
    """The one app-owned internal fan-out primitive over Redis pub/sub.

    Bound to this process's kind + pid at construction; its slot ``name`` and
    ``generation`` are minted on the first successful claim at subscribe time and
    then IMMUTABLE for the life of the held claim (a lost claim ends that life and
    re-mints a new one). :meth:`publish` reads the one identity to echo-skip the
    publisher's own broadcast and synthesize the self entry; the subscriber, presence
    writer, and echo-skip all read the SAME attribute, so the two-holder drift is
    structurally impossible. Constructed once per process by the lifecycle.

    The census life the subscription claims (:mod:`~tai42_skeleton.app.bus.life`) lasts as long
    as the process: the subscription's deliberate end leaves the heartbeat, the claim and the
    presence row in place, and :meth:`leave_census`, the lifespan's last shutdown step before
    its resource teardown, ends it.
    """

    def __init__(
        self,
        settings: BusSettings,
        *,
        kind: WorkerKind,
        exit_drain: Callable[[], float | None],
        pid: int | None = None,
        local: bool = False,
        reconnect_backoff_initial: float = 0.5,
        reconnect_backoff_max: float = 30.0,
        reconnect_backoff_factor: float = 2.0,
    ) -> None:
        """Bind the bus to this process's ``kind`` and ``pid``, minting a claim token and reconnect backoff.

        ``exit_drain`` reads the drain this process declared
        (:meth:`~tai42_skeleton.app.lifecycle.exit_drain.ExitDrain.seconds`), announced when it
        marks itself recycling.
        Registers an ``os.register_at_fork`` hook that re-derives the identity in
        a forked child; the busless ``local`` variant self-mints a member identity.
        """
        self._settings = settings
        self._kind = kind
        self._exit_drain = exit_drain
        self._pid = pid if pid is not None else os.getpid()
        self._local = local
        self._backoff_initial = reconnect_backoff_initial
        self._backoff_max = reconnect_backoff_max
        self._backoff_factor = reconnect_backoff_factor
        # A per-process claim token minted once at construction; the claim key is won
        # by ``SET <token> NX`` and only this token may renew or release it.
        self._claim_token = uuid.uuid4().hex
        # Minted on the first successful claim (subscribe time); ``None`` until then
        # for a real bus. The busless variant self-mints below.
        self._identity: WorkerIdentity | None = None
        # The bus member a forked child acts for, kept by the at-fork hook; ``None`` in a member
        # (which acts for itself).
        self._member_identity: WorkerIdentity | None = None
        self._joined_at: str | None = None
        # The in-memory presence-state object, alive from the first claim until the life ends
        # (:meth:`leave_census`), and dropped on a transport or lost-slot exit until the next connect.
        self._presence: _PresenceValue | None = None
        # Set in a fork child of a member parent that had not yet claimed: it has no
        # name to derive a non-member identity from, and an in-hook raise is
        # unraisable, so any later bus use raises loudly at the use site instead.
        self._poisoned = False
        # Single-shot post-terminal-reply slot: an op handler arms it during its
        # callback, and the subscription loop fires it AFTER the op's terminal reply
        # ships (recycle's graceful self-exit). At most one op is in flight on the
        # single subscription task, so there is never more than one armed action.
        self._post_reply_slot: Callable[[], object] | None = None
        # Why this process life is leaving, set once by the decision to leave, and the exits owed to
        # this worker's own in-flight changes (:meth:`defer_exit`), decided under one lock.
        self._exit_latch = ExitLatch()
        # The census announce of this life's leaving (:meth:`mark_recycling`), started in the same
        # step as the latch's first pull (:meth:`_begin_leaving`); ``None`` while the life is not leaving.
        self._leaving_announce: asyncio.Task[None] | None = None
        # This life's heartbeat: started on each connect, kept through a deliberate end of
        # the subscription, stopped on a transport or lost-slot exit and by :meth:`leave_census`.
        self._heartbeat: _Heartbeat | None = None
        if local:
            # The busless variant self-mints a full member identity at construction —
            # no claim, no INCR, no presence, no heartbeat (there is no connection).
            self._identity = WorkerIdentity(name=f"{kind.value}-1", kind=kind, pid=self._pid, generation=1)
            self._joined_at = _utcnow_iso()
        else:
            # Fork safety: a forked child inherits this bus object, so it re-derives its
            # own non-member identity in the child (see _fork_child_nonmember). One
            # registration per real bus instance; the bound hook is inherited across
            # further forks, so a re-derived child that forks again re-derives.
            os.register_at_fork(after_in_child=self._fork_child_nonmember)

    def _fork_child_nonmember(self) -> None:
        """Re-derive this bus's identity to an explicit NON-MEMBER in a forked child.

        Registered as an ``os.register_at_fork`` after-in-child hook at
        construction. A child inherits the parent's ``WorkerBus`` — identity included — across
        ``os.fork()``. Left shared, a fleet op the child publishes would carry the
        PARENT's identity and be echo-skipped by the parent's own subscription. A
        derived name ``{parent-name}/fork-{pid}`` at generation 0 makes the child's
        published op a foreign identity the parent applies instead of skipping, and
        can never collide with a member: the child claims nothing, registers no
        presence, and its inherited subscription task stays dormant. A child inherits
        only the forking thread, so the parent's heartbeat (its thread, or its task of the
        parent's loop) does not run there: its inherited handle is dropped and never stopped
        or joined in the child.

        The name is derived ONLY from a parent that HAS an identity (a member parent
        post-claim, or a local bus). A member parent that has not yet claimed has no
        name to derive from — and a raise here is unraisable — so the identity is
        POISONED instead: any bus use in the child raises loudly at the use site.

        The child keeps the member it acts for (:attr:`member_identity`): the parent itself
        when the parent is a member, else the member the parent kept — the top of the lineage
        however deep the forks go.
        """
        self._heartbeat = None
        # A child's life is its own: it inherits no departure, and no deferred exit, of the parent's.
        self._exit_latch = ExitLatch()
        self._leaving_announce = None
        parent = self._identity
        if parent is None:
            self._poisoned = True
            self._identity = None
            return
        if parent.member:
            self._member_identity = parent
        self._identity = WorkerIdentity(
            name=f"{parent.name}/fork-{os.getpid()}",
            kind=parent.kind,
            pid=os.getpid(),
            generation=0,
            member=False,
        )

    @classmethod
    def local(cls, kind: WorkerKind = WorkerKind.serve) -> WorkerBus:
        """The no-op variant for a single-worker / file-mode / no-backend / no-bus process.

        :meth:`publish` returns a local-only result, :meth:`subscribe` parks,
        :meth:`census` returns just this process's one synthesized row. Legal
        only under the boot rules that permit a busless deployment. The
        ``{kind}-1`` generation-1 identity is self-minted at construction. It holds no
        presence, so it announces no exit drain.
        """
        return cls(BusSettings(), kind=kind, exit_drain=lambda: None, local=True)

    @property
    def settings(self) -> BusSettings:
        """The bus settings this bus was built under: the fleet it is a member of (:attr:`BusSettings.fleet`)."""
        return self._settings

    @property
    def heartbeat_ttl(self) -> float:
        """This bus's presence-key TTL, the freshness cadence :func:`presence_fresh` gates against.

        The ONE bound a stale check reads, so no consumer hardcodes a threshold of its own.
        """
        return self._settings.heartbeat_ttl

    @property
    def identity(self) -> WorkerIdentity:
        """This process's bus identity.

        Raises before the slot is claimed (a real bus pre-subscribe), or in a
        fork child of an unclaimed member parent (a poisoned identity) — never a
        silent ``None``/placeholder name on the wire.
        """
        if self._poisoned:
            raise RuntimeError(
                "worker bus: forked from a member parent that had not yet claimed a slot — "
                "no name to derive a non-member identity from"
            )
        identity = self._identity
        if identity is None:
            raise RuntimeError("worker bus: identity is not minted yet — the slot has not been claimed")
        return identity

    @property
    def member_identity(self) -> WorkerIdentity:
        """The bus member this process acts for: itself, or the member a forked child descends from.

        The writer of a change is that member — a job child forked by a backend worker writes
        on that worker's behalf: the roll excludes it, the reply reports it, and the change's
        roll, the writer's own restart included, is handed to it
        (:func:`~tai42_skeleton.config.roll_handoff.hand_off_roll`). Raises as :attr:`identity`
        does before a claim or in a poisoned child.
        """
        identity = self.identity
        if identity.member:
            return identity
        member = self._member_identity
        if member is None:
            raise RuntimeError(f"worker bus: the non-member {identity.name!r} carries no member it acts for")
        return member

    @property
    def departing(self) -> str | None:
        """Why this process life is leaving — the reason its decision to leave carried — else ``None``."""
        return self._exit_latch.departing

    @property
    def exit_deferred(self) -> str | None:
        """The change this worker's exit is deferred to — the oldest one held — else ``None``.

        Set while a change this worker runs is in flight, from the change's start until the
        edge answering it settles (:meth:`defer_exit`): a recycle op then leaves the worker to
        that change, whose reply is answered before the worker leaves.
        """
        return self._exit_latch.oldest_hold

    @property
    def owns_exit(self) -> bool:
        """Whether this process has an exit of its own to defer: ``False`` only in a forked job child (a non-member)."""
        return not self._poisoned and (self._identity is None or self._identity.member)

    def defer_exit(self, reason: str) -> ExitHold:
        """Hold this worker's exit for its own in-flight change ``reason``, until that change is answered.

        Returns the hold, owned by the change until it is released (:meth:`release_deferred_exit`):
        by the writer's departure, which converts it into the exit, or by the change's edge once
        it answered without owing the exit. The exit latch leaves every hold to its owner. A
        process that does not own its exit (:attr:`owns_exit`: a forked job child) raises.
        """
        self._require_own_exit(f"has no exit of its own to defer ({reason})")
        return self._exit_latch.hold(reason)

    def defer_exit_unless_departing(self, reason: str) -> ExitHold | ExitRefusal:
        """Hold this worker's exit for ``reason`` as :meth:`defer_exit` does, unless it decided to leave.

        The check and the hold are one step against every decision to leave, so a change this
        returns a hold for is answered before the worker leaves; a worker that decided to leave
        returns the ``already_leaving`` refusal and holds nothing. A forked job child raises.
        """
        self._require_own_exit(f"has no exit of its own to defer ({reason})")
        return self._exit_latch.hold_unless_departing(reason)

    def release_deferred_exit(self, hold: ExitHold) -> None:
        """Release ``hold``: its change no longer holds this worker's exit. Raises when ``hold`` is not held."""
        self._exit_latch.release(hold)

    async def deferred_exits_settled(self) -> None:
        """Return once no change holds this worker's exit (:meth:`defer_exit`): at once when none does."""
        await self._exit_latch.settled()

    def request_graceful_exit(self, reason: str) -> bool:
        """Decide to leave for ``reason`` and start this worker's graceful stop at once: ``True`` when this call did.

        The first decision in a process life begins this life's leaving (:meth:`_begin_leaving`):
        it records ``reason``, sends the single SIGTERM a process life sends itself
        (:meth:`signal_departure`) and starts the ``recycling`` announce on the census. Every later
        call sends and writes nothing: it logs at INFO that the worker is already leaving, naming
        both reasons, and returns ``False``. The latch leaves every deferred exit
        (:meth:`defer_exit`) held by its change, which releases it: the stop that follows lets that
        change finish within the worker's drain and cuts it at the drain's cap (for a serve worker,
        uvicorn's ``timeout_graceful_shutdown``); a door that must wait for the held changes decides
        through :meth:`decide_departure` instead. A forked job child (a non-member) owns no exit and
        raises: a change it runs is bounded, so it hands its roll, the writer's restart included, to
        the member it acts for
        (:func:`~tai42_skeleton.config.roll_handoff.hand_off_roll`).
        """
        self._require_own_exit(f"cannot end its member's life ({reason}) — {_HANDED_TO_THE_MEMBER}")
        refusal = self._begin_leaving(reason, unless_held=False, signal_now=True)
        if refusal is not None:
            logger.info(
                "worker bus: this worker is already leaving (%s) — not signalled again for %s", refusal.reason, reason
            )
            return False
        return True

    def decide_departure(self, reason: str) -> ExitRefusal | None:
        """Decide to leave for ``reason`` only when no in-flight change holds the exit: ``None`` when this call did.

        The decision is one step against every hold taken (:meth:`defer_exit_unless_departing`),
        so once it is taken no change starts that this exit would not wait for. Otherwise the
        refusal says why: the worker is ``already_leaving``, or its exit is ``exit_deferred`` to
        its own in-flight change. A decision taken begins this life's leaving
        (:meth:`_begin_leaving`), the ``recycling`` announce on the census included, and is
        final: its caller awaits the announce (:meth:`announce_departure`) and sends the signal
        (:meth:`signal_departure`) once its own steps before the stop are done. A forked job
        child raises.
        """
        self._require_own_exit(f"cannot end its member's life ({reason}) — {_HANDED_TO_THE_MEMBER}")
        return self._begin_leaving(reason, unless_held=True, signal_now=False)

    def stop_began(self, reason: str) -> bool:
        """Begin this life's leaving for a stop decided outside the process: ``True`` when this call began it.

        The stop's first signal already arrived (or the app's lifespan shutdown began with no
        signal of ours), so this sends none: it pulls the latch and starts the ``recycling``
        announce (:meth:`_begin_leaving`), after which a recycle op answers ``already_leaving`` and
        a carried roll is refused. A change in flight keeps its hold and finishes within the drain
        the stop allows; what outlasts the cap is cut by the stop, not by this. A life already
        leaving is left alone: logged at INFO naming both reasons, ``False``. A process that does
        not own its exit (:attr:`owns_exit`: a forked job child) never ends its member's life: it
        does nothing, logged at INFO, ``False``.
        """
        if not self.owns_exit:
            logger.info("worker bus: a non-member process does not end its member's life — %s ignored here", reason)
            return False
        refusal = self._begin_leaving(reason, unless_held=False, signal_now=False)
        if refusal is not None:
            logger.info("worker bus: this worker is already leaving (%s) — %s begins nothing", refusal.reason, reason)
            return False
        logger.info("worker bus: this worker is leaving (%s)", reason)
        return True

    def _begin_leaving(self, reason: str, *, unless_held: bool, signal_now: bool) -> ExitRefusal | None:
        """Pull this life's exit latch for ``reason`` and, on the first pull, start the census announce of it.

        The one step every door that begins this life's leaving goes through, so the latch and the
        ``recycling`` row flip together: the announce (:meth:`mark_recycling`) is started as a task
        on the running loop in the same step as the pull, and kept for :meth:`leaving_announced`.
        ``unless_held`` pulls only when no in-flight change holds the exit
        (:meth:`ExitLatch.pull_unless_held`); ``signal_now`` sends the departure's signal between
        the pull and the announce, so a signal that cannot be sent clears the latch, announces
        nothing and raises. Returns ``None`` when this call pulled the latch, else the refusal.
        Raises off a running loop, before the latch is touched.
        """
        loop = asyncio.get_running_loop()
        if unless_held:
            refusal = self._exit_latch.pull_unless_held(reason)
        else:
            already = self._exit_latch.pull(reason)
            refusal = None if already is None else ExitRefusal(kind="already_leaving", reason=already)
        if refusal is not None:
            return refusal
        if signal_now:
            self.signal_departure()
        announce = loop.create_task(self.mark_recycling(), name="tai-worker-bus-leaving-announce")
        announce.add_done_callback(_log_failed_announce)
        self._leaving_announce = announce
        return None

    async def leaving_announced(self) -> None:
        """Return once this life's leaving is announced on the census, raising what the announce raised.

        Waits for the announce the door that began the leaving started (:meth:`_begin_leaving`);
        a caller cancelled meanwhile leaves the announce running. A process that does not own its
        exit (a forked job child) has no row and returns at once; one whose life is not leaving
        raises.
        """
        if not self.owns_exit:
            return
        announce = self._leaving_announce
        if announce is None:
            raise RuntimeError("worker bus: this life is not leaving — there is no announce to wait for")
        await asyncio.shield(announce)

    async def announce_departure(self) -> None:
        """Wait for the ``recycling`` announce of the departure :meth:`decide_departure` took, before its signal.

        The decision is final, so an announce that fails sends the departure's signal first, then raises.
        """
        try:
            await self.leaving_announced()
        except Exception:
            self.signal_departure()
            raise

    def signal_departure(self) -> None:
        """Send the single SIGTERM of the departure this process life decided to its own pid.

        Its installed graceful handler (uvicorn's shutdown for a serve worker, the backend's drain
        for a backend worker) lets in-flight work finish before the process ends, within the
        drain the worker announces. Raises when no departure was decided; a signal that cannot be sent
        clears the decision, since the worker is not leaving, and raises.
        """
        reason = self._exit_latch.departing
        if reason is None:
            raise RuntimeError("worker bus: no departure was decided — there is nothing to signal")
        logger.info("worker bus: this worker leaves gracefully (%s) — SIGTERM to its own pid %d", reason, self._pid)
        try:
            os.kill(self._pid, signal.SIGTERM)
        except BaseException:
            self._exit_latch.unlatch()
            raise

    def _require_own_exit(self, what: str) -> None:
        """Raise in a process that does not own its exit (:attr:`owns_exit`: a forked job child)."""
        if not self.owns_exit:
            raise RuntimeError(f"worker bus: a non-member process {what}")

    def arm_post_reply(self, action: Callable[[], object]) -> None:
        """Arm the single-shot slot the subscription loop fires once the current op's terminal reply was sent.

        Fires whether that reply shipped or failed to ship and whether the op applied or failed,
        so only a decision already taken is armed (recycle's post-reply self-exit); a cancelled op
        fires nothing. Called from an op handler during its callback; the last arming wins.
        """
        self._post_reply_slot = action

    def _local_row(self) -> WorkerRow:
        identity = self.identity
        joined_at = self._joined_at if self._joined_at is not None else _utcnow_iso()
        return WorkerRow(
            name=identity.name,
            kind=identity.kind,
            pid=identity.pid,
            generation=identity.generation,
            joined_at=joined_at,
            beat_at=_utcnow_iso(),
            state=WorkerState.ready,
            # With no bus the fleet is this process: its own names and readers are the fleet's.
            deployment_keys=list(published_deployment_keys()),
            env_readers=published_env_readers().digest,
            last_op=None,
            # The lone busless worker is this live process — always fresh, so it clears
            # the same freshness predicate every other row is judged by (never stale).
            pttl_ms=int(self._settings.heartbeat_ttl * 1000),
        )


def _log_failed_announce(announce: asyncio.Task[None]) -> None:
    """Log at ERROR a leaving announce that raised or was cancelled: the census row may still read ``ready``."""
    if announce.cancelled():
        logger.error("worker bus: the recycling announce of this leaving life was cancelled before it wrote")
        return
    exc = announce.exception()
    if exc is not None:
        logger.error("worker bus: the recycling announce of this leaving life failed", exc_info=exc)
