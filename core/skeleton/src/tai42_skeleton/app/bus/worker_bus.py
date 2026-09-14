"""The worker-bus object: identity/claim state and construction, composing the
publish + subscribe surfaces."""

from __future__ import annotations

import logging
import os
import sys
import uuid
from collections.abc import Callable
from typing import Any

from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.app.bus.models import (
    _TRANSPORT_ERRORS,
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

logger = logging.getLogger(__name__)

# The seam-package object this submodule reads ``client_ctx`` through at call time, so a
# ``monkeypatch.setattr`` on the ``tai42_skeleton.app.bus`` alias bites the pooled
# connection opened here. Captured as this package instance's own object.
_pkg = sys.modules["tai42_skeleton.app.bus"]


class WorkerBus(WorkerBusPublishMixin, WorkerBusSubscribeMixin):
    """The one app-owned internal fan-out primitive over Redis pub/sub.

    Bound to this process's kind + pid at construction; its slot ``name`` and
    ``generation`` are minted on the first successful claim at subscribe time and
    then IMMUTABLE for the life of the held claim (a lost claim ends that life and
    re-mints a new one). :meth:`publish` reads the one identity to echo-skip the
    publisher's own broadcast and synthesize the self entry; the subscriber, presence
    writer, and echo-skip all read the SAME attribute, so the two-holder drift is
    structurally impossible. Constructed once per process by the lifecycle."""

    def __init__(
        self,
        settings: BusSettings,
        *,
        kind: WorkerKind,
        pid: int | None = None,
        local: bool = False,
        reconnect_backoff_initial: float = 0.5,
        reconnect_backoff_max: float = 30.0,
        reconnect_backoff_factor: float = 2.0,
    ) -> None:
        self._settings = settings
        self._kind = kind
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
        self._joined_at: str | None = None
        # The in-memory presence-state object, alive only while subscribed.
        self._presence: _PresenceValue | None = None
        # Set in a fork child of a member parent that had not yet claimed: it has no
        # name to derive a non-member identity from, and an in-hook raise is
        # unraisable, so any later bus use raises loudly at the use site instead.
        self._poisoned = False
        # Single-shot post-terminal-reply slot: an op handler arms it during its
        # callback, and the subscription loop fires it AFTER the op's terminal reply
        # ships (recycle's graceful self-exit). At most one op is in flight on the
        # single subscription task, so there is never more than one armed action.
        self._post_reply_slot: Callable[[], None] | None = None
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
        """Re-derive this bus's identity to an explicit NON-MEMBER in a forked child
        (an ``os.register_at_fork`` after-in-child hook registered at construction).

        A child inherits the parent's ``WorkerBus`` — identity included — across
        ``os.fork()``. Left shared, a fleet op the child publishes would carry the
        PARENT's identity and be echo-skipped by the parent's own subscription. A
        derived name ``{parent-name}/fork-{pid}`` at generation 0 makes the child's
        published op a foreign identity the parent applies instead of skipping, and
        can never collide with a member: the child claims nothing, registers no
        presence, and its inherited subscription task stays dormant.

        The name is derived ONLY from a parent that HAS an identity (a member parent
        post-claim, or a local bus). A member parent that has not yet claimed has no
        name to derive from — and a raise here is unraisable — so the identity is
        POISONED instead: any bus use in the child raises loudly at the use site."""
        parent = self._identity
        if parent is None:
            self._poisoned = True
            self._identity = None
            return
        self._identity = WorkerIdentity(
            name=f"{parent.name}/fork-{os.getpid()}",
            kind=parent.kind,
            pid=os.getpid(),
            generation=0,
            member=False,
        )

    @classmethod
    def local(cls, kind: WorkerKind = WorkerKind.serve) -> WorkerBus:
        """The no-op variant for a single-worker / file-mode / no-backend / no-bus
        process: :meth:`publish` returns a local-only result, :meth:`subscribe`
        parks, :meth:`census` returns just this process's one synthesized row. Legal
        only under the boot rules that permit a busless deployment. The ``{kind}-1``
        generation-1 identity is self-minted at construction."""
        return cls(BusSettings(), kind=kind, local=True)

    @property
    def heartbeat_ttl(self) -> float:
        """This bus's presence-key TTL, the freshness cadence :func:`presence_fresh`
        gates against — the ONE bound a stale check reads, so no consumer hardcodes a
        threshold of its own."""
        return self._settings.heartbeat_ttl

    @property
    def identity(self) -> WorkerIdentity:
        """This process's bus identity. Raises before the slot is claimed (a real bus
        pre-subscribe), or in a fork child of an unclaimed member parent (a poisoned
        identity) — never a silent ``None``/placeholder name on the wire."""
        if self._poisoned:
            raise RuntimeError(
                "worker bus: forked from a member parent that had not yet claimed a slot — "
                "no name to derive a non-member identity from"
            )
        identity = self._identity
        if identity is None:
            raise RuntimeError("worker bus: identity is not minted yet — the slot has not been claimed")
        return identity

    def arm_post_reply(self, action: Callable[[], None]) -> None:
        """Arm the single-shot slot the subscription loop fires AFTER the current op's
        terminal reply ships and only on a clean apply (recycle's post-reply self-exit).
        Called from an op handler during its callback; the last arming wins."""
        self._post_reply_slot = action

    async def mark_recycling(self) -> None:
        """Write ``state=recycling`` into presence before a graceful self-exit, so the
        census shows WHY this worker is departing rather than reading as merely quiet.

        Renew-gated like every presence write (the compare-token renew runs FIRST and
        the write is skipped on a miss) AND best-effort against transport errors: a
        cosmetic census write can never abort the recycle it precedes, so a blip on the
        connect/renew/set is logged and swallowed rather than propagated. A no-op when
        this bus holds no presence (a busless or unsubscribed bus)."""
        presence = self._presence
        if presence is None:
            return
        name = self.identity.name
        try:
            async with _pkg.client_ctx(RedisClient, self._settings.redis) as conn:
                r: Any = conn
                if not await self._renew_claim(r, name):
                    return
                presence.state = WorkerState.recycling
                presence.beat_at = _utcnow_iso()
                await self._set_presence(r, self._settings.presence_key(name), presence)
        except _TRANSPORT_ERRORS:
            logger.warning(
                "worker bus: recycling-state write for %s failed — census may briefly read it as quiet; "
                "proceeding with recycle",
                name,
                exc_info=True,
            )

    @staticmethod
    def _op_name(op: dict[str, Any]) -> str:
        name = op.get("op")
        if not isinstance(name, str) or not name:
            raise ValueError("publish: op dict must carry a non-empty 'op' name")
        return name

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
            last_op=None,
            # The lone busless worker is this live process — always fresh, so it clears
            # the same freshness predicate every other row is judged by (never stale).
            pttl_ms=int(self._settings.heartbeat_ttl * 1000),
        )
