"""Take part in fleet ops: subscribe to the control channel and apply delivered ops.

The subscription claims this process's census life on its first connect (through
:class:`~tai42_skeleton.app.bus.life.WorkerBusLifeMixin`) and keeps it across every
reconnect; its deliberate end leaves only the channel. The heartbeat, the claim and the
presence row stay in place until the life's end, :meth:`WorkerBusLifeMixin.leave_census`,
which the stop runs after its shutdown handlers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.app.bus.life import WorkerBusLifeMixin
from tai42_skeleton.app.bus.models import (
    _OP_PAYLOAD_KEY,
    TRANSPORT_ERRORS,
    LastOp,
    OpOutcome,
    SlotLostError,
    WorkerState,
    _decode,
    _PresenceValue,
    _utcnow_iso,
)
from tai42_skeleton.config.deployment_env import published_deployment_keys, published_env_readers

logger = logging.getLogger(__name__)


class OpApplier(Protocol):
    """Applies one delivered op; ``deadline`` is the ``time.monotonic()`` instant its apply window ends."""

    def __call__(self, op: dict[str, Any], /, *, deadline: float) -> Awaitable[Any]:
        """Apply ``op`` and return its terminal payload; a raise reports the op ``failed``."""
        ...


OpFailurePayload = Callable[[dict[str, Any], Exception], Any]
"""States what a delivered op's failure leaves: its ``failed`` reply's payload for ``(op, failure)``, or ``None``."""


# The seam-package object this submodule reads ``client_ctx`` through at call time, so a
# ``monkeypatch.setattr`` on the ``tai42_skeleton.app.bus`` alias bites the pooled
# connection opened here. Captured as this package instance's own object.
_pkg = sys.modules["tai42_skeleton.app.bus"]

# How long one idle pub/sub poll blocks in the subscribe loop; bounds the latency
# of noticing a cancellation and of the next presence refresh.
_POLL_TIMEOUT = 0.1


class WorkerBusSubscribeMixin(WorkerBusLifeMixin):
    """The subscriber surface of :class:`WorkerBus`: take part in ops under this process's census life.

    Claims the life on the first connect, keeps it across reconnects, and applies delivered
    ops with a two-phase confirmation.
    """

    if TYPE_CHECKING:
        # Op-participation state supplied by the composed WorkerBus (declared here so the
        # mixin type-checks in isolation; the real attributes are set in WorkerBus.__init__).
        _post_reply_slot: Callable[[], object] | None
        _backoff_initial: float
        _backoff_max: float
        _backoff_factor: float

    async def subscribe(
        self,
        callback: OpApplier,
        on_ready: Callable[[], Awaitable[None]] | None = None,
        *,
        failure_payload: OpFailurePayload | None = None,
    ) -> None:
        """Consume the control channel until cancelled, reconnecting with backoff.

        Claims a slot (``{kind}-{n}``, lowest free ordinal) and registers a TTL
        presence key for it, refreshed at ``ttl/3`` under a compare-token renew. Each
        op is applied by awaiting ``callback`` on this task, with a two-phase
        confirmation: a ``received`` ack the instant the op is delivered, then one
        terminal ``applied``/``failed`` when the callback returns (a raising callback
        ⇒ ``failed``, its reply carrying the payload ``failure_payload`` states for the op and
        the failure, when it states one). The callback's ``deadline`` is the receipt instant plus
        the apply window; the bus transit before receipt is not subtracted. The publisher's
        own broadcast is echo-skipped by ``(name, generation)`` — its self entry is
        synthesized from the ``local`` result it hands :meth:`publish`.

        The claim is verified FIRST on every (re)connect: a held claim keeps the same
        name+generation across a reconnect; a lost claim (renew miss) re-mints a NEW
        life. The heartbeat runs, by default, on a thread of its own with its own event
        loop and connection, so a callback or resync that blocks this task's loop
        synchronously (a config reload's rebuild) never lapses the claim or the presence
        row. Under a process prelude it is a task of this task's loop instead, so a hold of
        that loop longer than the claim TTL lapses them (:mod:`tai42_skeleton.app.bus.life`). It starts
        BEFORE the ``on_ready`` resync (the resync budget exceeds the claim TTL, so an
        unrenewed claim would lapse mid-resync); presence is written ``resyncing`` before
        the resync and ``ready`` after, both under the renew-gated heartbeat path. The
        channel is subscribed before the resync, so an op broadcast during it is
        buffered and applied by the message loop. A reconnect the connection retry
        hides (the pub/sub reconnects and re-subscribes inside a read, raising nothing)
        is WARNING-logged and runs the same resync, as an op broadcast while the
        connection was down is never delivered. Presence is
        NOT force-deleted on a reconnect (its TTL carries the identity across a blip).
        A deliberate end (cancellation) leaves only the channel: the heartbeat, the claim
        and the row stay in place until the life's end (:meth:`leave_census`, run by the stop
        after its shutdown handlers) releases the claim and deletes the row.
        A lost slot is routed through a DEDICATED reconnect branch (never the
        transport-error path) that re-enters immediately with no backoff. Each
        transport reconnect attempt is ERROR-logged.
        """
        if self._local:
            await asyncio.Event().wait()
            return

        backoff = self._backoff_initial
        while True:
            established = False

            def _mark_established() -> None:
                nonlocal established
                established = True

            try:
                await self._run_subscription(callback, on_ready, _mark_established, failure_payload)
            except asyncio.CancelledError:
                raise
            except SlotLostError as exc:
                # A lost slot on a (possibly) healthy connection is NOT a transport
                # outage: re-enter IMMEDIATELY with no backoff (the slot is gone,
                # nothing to wait out) and re-mint a new life on re-entry.
                logger.warning("worker bus: slot %s lost; re-minting a new life", exc)
                continue
            except TRANSPORT_ERRORS:
                last = self._identity.name if self._identity is not None else self._kind.value
                logger.error(
                    "worker bus: subscription transport error for %s; reconnecting in %.2fs",
                    last,
                    backoff,
                    exc_info=True,
                )
            else:
                return
            if established:
                backoff = self._backoff_initial
            await asyncio.sleep(backoff)
            backoff = min(backoff * self._backoff_factor, self._backoff_max)

    async def _run_subscription(
        self,
        callback: OpApplier,
        on_ready: Callable[[], Awaitable[None]] | None,
        on_established: Callable[[], None],
        failure_payload: OpFailurePayload | None,
    ) -> None:
        # ``fresh=True``: a DEDICATED, non-pooled connection for the process-lifetime
        # subscription, so an epoch retire's ``drain_epoch`` can never force-close it.
        # A pooled lease is stamped with the epoch current at acquire time; the
        # subscription holds it for the whole epoch, so every reload's retire would
        # force-close it (blocking the full drain budget first) → the subscription
        # reconnects → its ``on_ready`` fires a resync reload_config → which retires and
        # force-closes the NEXT lease → an unbounded reload cascade. The bus URL is
        # Tier-1 refused (never changes across a profile apply), so this connection is
        # invariant across epochs and has no reason to be epoch-scoped; a publish's reply
        # collection holds its connection the same way (``_broadcast`` in ``publish.py``).
        # A bus outage the
        # connection retry outlasts raises a transport error here and drives the
        # reconnect+resync self-heal; a reconnect the retry hides (the pub/sub reconnects
        # and re-subscribes inside a read) sets ``reconnected`` and the loop runs the
        # same resync, since an op published while it was disconnected is never delivered.
        async with _pkg.client_ctx(RedisClient, self._settings.redis, fresh=True) as conn:
            r: Any = conn
            reconnected = False

            def _mark_reconnected() -> None:
                nonlocal reconnected
                reconnected = True

            pubsub = r.pubsub(on_reconnect=_mark_reconnected)
            await pubsub.subscribe(self._settings.channel)
            exit_kind = "transport"
            try:
                on_established()
                # Verify/claim the slot FIRST: a held claim keeps its name+generation
                # across the reconnect; a lost or absent claim mints a NEW life.
                await self._establish_identity(r)
                identity = self.identity
                presence_key = self._settings.presence_key(identity.name)
                self._presence = self._connection_presence()
                # Heartbeat FIRST (renew-gated), BEFORE on_ready: the resync budget exceeds
                # the claim TTL and the resync's rebuild holds this loop, so the claim is
                # renewed from a thread of its own throughout, except under a process
                # prelude, where the heartbeat is a task of this loop (:class:`_Heartbeat`).
                heartbeat = self._start_heartbeat(identity.name, presence_key)
                # Advertise ``resyncing`` before the resync, then ``ready`` after — both
                # renew-gated (skipped on a miss; the heartbeat's own miss raises
                # SlotLostError to route the re-mint). Presence is written BEFORE the
                # resync converges, so a mid-resync row is visible, not hidden. The channel
                # is already subscribed, so an op broadcast during the resync is buffered
                # and applied below; the publisher's gate therefore excludes the row for an
                # op its own resync re-reads, and expects it for an op that declares
                # ``awaited_mid_resync``.
                await self._resync(r, presence_key, self._presence, on_ready)
                logger.info("worker bus: subscription live as %s", identity.name)
                while True:
                    if reconnected:
                        reconnected = False
                        logger.warning(
                            "worker bus: subscription for %s reconnected; resyncing, as an op sent while "
                            "it was disconnected is not delivered",
                            identity.name,
                        )
                        await self._resync(r, presence_key, self._presence, on_ready)
                    if heartbeat.done.done():
                        # The heartbeat ended on its own — a presence refresh raised on
                        # its own connection while this held pub/sub connection stayed
                        # healthy (an asymmetric transport failure), OR a renew miss
                        # raised SlotLostError (the slot was lost). Surface it and force
                        # the whole subscription down so the outer loop reconnects
                        # (transport) or re-mints (slot lost). The exit's heartbeat stop
                        # still joins the finished thread; its exception is retrieved and
                        # logged here once.
                        raise self._heartbeat_failure(heartbeat, identity.name)
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=_POLL_TIMEOUT)
                    if msg is not None:
                        await self._handle_op(r, callback, msg, failure_payload=failure_payload)
            except asyncio.CancelledError:
                exit_kind = "deliberate"
                raise
            except SlotLostError:
                exit_kind = "slot_lost"
                raise
            except TRANSPORT_ERRORS:
                exit_kind = "transport"
                raise
            finally:
                await self._end_connection(exit_kind)
                await self._leave_channel(pubsub)

    def _connection_presence(self) -> _PresenceValue:
        """The in-memory presence a connection starts this life's row from.

        ``resyncing`` until the connection's resync converges, or ``recycling`` from the start
        when this life already began leaving.
        """
        identity = self.identity
        presence = _PresenceValue(
            kind=identity.kind,
            pid=identity.pid,
            generation=identity.generation,
            joined_at=self._joined_at if self._joined_at is not None else _utcnow_iso(),
            beat_at=_utcnow_iso(),
            state=WorkerState.resyncing,
            deployment_keys=published_deployment_keys(),
            env_readers=published_env_readers().digest,
            last_op=None,
        )
        if self._exit_latch.departing is not None:
            self._carry_leaving(presence)
        return presence

    async def _end_connection(self, exit_kind: str) -> None:
        """End this connection's hold on the life, as its ``exit_kind`` says.

        A transport or slot-lost end ends the connection, not the life: the next attempt
        re-verifies the claim (or re-mints) and starts a new heartbeat, so this one stops here
        and the keys carry the identity across on their TTL. A deliberate end leaves the life
        in place — the heartbeat keeps the claim and the row fresh until ``leave_census`` ends
        it after the stop's shutdown handlers — and no longer supervises it, so its end is logged when it
        ends.
        """
        if exit_kind != "deliberate":
            await self._stop_heartbeat()
            logger.info("worker bus: %s exit — presence/claim keys left to their TTL across the reconnect", exit_kind)
            self._presence = None
            return
        unsupervised = self._heartbeat
        if unsupervised is not None:
            unsupervised.watch_unsupervised(self.identity.name)

    async def _resync(
        self,
        r: Any,
        presence_key: str,
        presence: _PresenceValue,
        on_ready: Callable[[], Awaitable[None]] | None,
    ) -> None:
        """Write ``presence`` as ``resyncing``, run ``on_ready``, then write it as ``ready``.

        A life that began leaving keeps reading ``recycling`` throughout: its leaving, begun
        before this resync or during it, is never overwritten.
        """
        if presence.state is not WorkerState.recycling:
            presence.state = WorkerState.resyncing
        await self._write_presence(r, presence_key, presence)
        if on_ready is not None:
            await on_ready()
        if presence.state is not WorkerState.recycling:
            presence.state = WorkerState.ready
        await self._write_presence(r, presence_key, presence)

    async def _handle_op(
        self,
        r: Any,
        callback: OpApplier,
        msg: dict[str, Any],
        *,
        failure_payload: OpFailurePayload | None,
    ) -> None:
        admitted = self._admit_op_frame(msg)
        if admitted is None:
            return
        op_payload, responder, reply_to, _op_id = admitted
        await self._apply_op_and_reply(r, callback, op_payload, responder, reply_to, failure_payload=failure_payload)

    def _admit_op_frame(self, msg: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str | None, Any] | None:
        """Admit an op frame: decode, echo-skip by ``(name, generation)``, filter by targets, validate payload.

        Returns ``(op_payload, responder, reply_to, op_id)`` for a frame to apply, or
        ``None`` when it is not ours.
        """
        frame = _decode(msg["data"])
        if frame is None:
            return None
        identity = self.identity
        if frame.get("name") == identity.name and frame.get("generation") == identity.generation:
            # Echo-skip by (name, generation): the publisher applied this op locally
            # before broadcasting and synthesizes its own self entry — delivering it
            # back would double-apply. A frame from an earlier life under the same name
            # (a different generation) is NOT ours and is applied normally.
            return None
        targets = frame.get("targets")
        # Test presence, not truthiness: ``targets is None`` reaches every worker, a
        # list reaches exactly its members — so an empty list reaches nobody, matching
        # the publisher's empty expected set (no silent sibling apply).
        if targets is not None and identity.name not in targets:
            return None
        # The op payload rides nested under its reserved envelope key; a frame that is
        # missing it (or carries a non-object there) is malformed and discarded rather
        # than half-applied.
        op_payload = frame.get(_OP_PAYLOAD_KEY)
        if not isinstance(op_payload, dict):
            logger.warning("worker bus: discarding op frame with no object payload: %r", frame)
            return None
        reply_to = frame.get("reply_to")
        op_id = frame.get("op_id")
        responder = {"name": identity.name, "generation": identity.generation, "op_id": op_id}
        return op_payload, responder, reply_to, op_id

    async def _apply_op_and_reply(
        self,
        r: Any,
        callback: OpApplier,
        op_payload: dict[str, Any],
        responder: dict[str, Any],
        reply_to: str | None,
        *,
        failure_payload: OpFailurePayload | None,
    ) -> None:
        """Run the callback, send the terminal (applied/failed) frame after the received ack, and stamp last-op.

        A ``failed`` frame carries the payload ``failure_payload`` states for the op and its
        failure, when it states one.

        Fires the armed post-reply slot once the terminal reply was sent, whether it shipped or
        failed to ship and whether the op applied or failed: an action armed is a decision the
        handler already took. A cancellation fires nothing. The slot is disarmed on every exit.
        """
        identity = self.identity
        received_at = time.monotonic()
        if reply_to:
            await self._reply(r, reply_to, {**responder, "phase": "received"})
        outcome_value = OpOutcome.applied.value
        try:
            try:
                result = await callback(op_payload, deadline=received_at + self._settings.apply_timeout)
                terminal: dict[str, Any] = {**responder, "phase": "terminal", "outcome": OpOutcome.applied.value}
                if result is not None:
                    terminal["payload"] = result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("worker bus: op %r failed on %s", op_payload.get("op"), identity.name, exc_info=True)
                outcome_value = OpOutcome.failed.value
                terminal = {
                    **responder,
                    "phase": "terminal",
                    "outcome": OpOutcome.failed.value,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                stated = None if failure_payload is None else failure_payload(op_payload, exc)
                if stated is not None:
                    terminal["payload"] = stated

            try:
                if reply_to:
                    await self._reply(r, reply_to, terminal)

                # Stamp the applied op into presence AFTER the terminal reply ships. This is
                # a renew-gated presence write like any other: skipped on a renew miss, so an
                # ex-owner in the loss-lag never regresses the census under its stale name.
                op_name = op_payload.get("op")
                if isinstance(op_name, str):
                    await self._stamp_last_op(r, op_name, outcome_value)
            except Exception:
                # A reply or stamp that failed still ends the op: the armed action (a departure
                # the handler decided) runs before the error propagates.
                self._fire_post_reply()
                raise
            # Fire the armed post-reply slot (recycle's self-exit) only once the terminal reply
            # was sent, so the publisher records the op's outcome before this process departs.
            # A handler may ONLY arm the slot: scheduling the exit inside the callback would race
            # the reply and be reported ``timed_out``.
            self._fire_post_reply()
        finally:
            # Single-shot: disarm on EVERY exit — a fire, a reply-publish error, or a
            # cancellation — so no later op ever misfires a stale self-exit.
            self._post_reply_slot = None

    def _fire_post_reply(self) -> None:
        """Disarm the post-reply slot and run the action it held, if one was armed."""
        action = self._post_reply_slot
        self._post_reply_slot = None
        if action is not None:
            action()

    async def _stamp_last_op(self, r: Any, op_name: str, outcome: str) -> None:
        """Re-write presence with the last applied op (name + outcome + timestamp).

        A no-op when this bus holds no presence (not subscribed under a claimed slot).
        Renew-gated: the compare-token renew runs FIRST and the write is skipped on a
        miss — the re-mint itself rides the next heartbeat's ``SlotLostError``, never
        this message-loop write.
        """
        presence = self._presence
        if presence is None:
            return
        name = self.identity.name
        if not await self._renew_claim(r, name):
            return
        presence.last_op = LastOp(op=op_name, outcome=outcome, at=_utcnow_iso())
        presence.beat_at = _utcnow_iso()
        await self._set_presence(r, self._settings.presence_key(name), presence)

    async def _reply(self, r: Any, channel: str, frame: dict[str, Any]) -> None:
        await r.publish(channel, json.dumps(frame))

    async def _leave_channel(self, pubsub: Any) -> None:
        """Unsubscribe from the control channel and close the pub/sub; best-effort, logged on failure."""
        try:
            await pubsub.unsubscribe(self._settings.channel)
            await pubsub.aclose()
        except Exception:
            logger.warning("worker bus: pub/sub close failed when leaving the channel", exc_info=True)
