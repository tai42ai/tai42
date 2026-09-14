"""The worker bus — the app's one INTERNAL fleet fan-out primitive.

Every process (HTTP server or backend runtime) that shares a manifest joins one
Redis pub/sub control channel through :meth:`WorkerBus.subscribe` and keeps a TTL
presence key alive; a mutation reaches the whole fleet through the single awaited
:meth:`WorkerBus.publish`, which collects a per-worker outcome from every live
worker.

This is app-owned internal infrastructure, like the reload gate — NOT a plugin.
Nothing here is registrable, swappable, or user-selectable; there is exactly one
bus, and no manifest field chooses an implementation. A deployment with no Redis
configured runs on :meth:`WorkerBus.local`, the no-op variant.

Identity
--------
A worker is a named SLOT: ``{kind}-{n}`` (``serve-1``, ``backend-2``), the lowest
free ordinal, claimed atomically in bus Redis at subscribe time with ``SET NX`` on
the claim key and a per-name ``INCR`` generation counter. The claim carries a
per-process token (uuid4); a heartbeat renews it with a compare-token Lua script,
and the presence write follows ONLY on a successful renew. A renew miss (token
mismatch or absent key) is a LOST slot: the worker abandons the identity and
re-mints a NEW life (fresh claim + ``INCR``, possibly a different name) — a lost
slot is never resumed. Name and generation are minted once per life and immutable
for the life of the held claim; a reconnect that still holds its claim keeps them.

Namespacing
-----------
``TAI_BUS_NAMESPACE`` (default ``tai``) prefixes the control channel, every reply
channel, and every presence/slot/generation key. Redis pub/sub is server-global (NOT
scoped by db index), so two deployments/stacks sharing one Redis MUST diverge by
namespace or they would cross-talk; a shared-Redis deployment sets a unique
namespace per stack.

Transport shape
---------------
One control channel carries every op; each op names an ephemeral reply channel for
its confirmations and stamps its ``op_id`` (the reply-channel uuid). Every op frame
carries the publisher's ``(name, generation)``; both reply frames carry the
responder's ``(name, generation)`` and the echoed ``op_id``. ``_collect`` keys the
expected set on ``(name, generation)`` and DISCARDS a reply whose generation or
op_id does not match — a worker that lost its slot but has not yet discovered it (up
to one heartbeat of detection lag) can still reply under its stale life; the
generation gate drops exactly those. The census is a scan of the per-name presence
keys. Two wire messages come back from a subscriber per op: a ``received`` ack the
instant the op is delivered and exactly one terminal ``applied``/``failed`` once the
op has fully applied. ``timed_out`` and ``departed`` are never wire replies — the
publisher computes them from the presence census at the report cut.

That census is taken inside :meth:`WorkerBus.publish`, which for most callers is
AFTER their own local apply — so a publisher that must not lose a worker to a
presence TTL fading across a slow apply hands :meth:`WorkerBus.publish` its own
op-start census as ``expected_at_start``, and those names stay expected here.

Presence lifecycle
------------------
Presence is written under ``{ns}:bus:presence:{name}`` with a value carrying
``{kind, pid, generation, joined_at, beat_at, state, last_op?}`` and refreshed at
``ttl/3``. It is NOT force-deleted on a reconnect: the TTL carries the row across a
blip so a reader can tell "same worker, still here on TTL" from "new life". A
transport-error or lost-slot exit leaves the keys to their TTL; only a deliberate
stop deletes the presence row, and only after the compare-token claim release
succeeds (an absent/foreign claim means the slot already belongs to a new holder,
whose row must not be deleted). The row's ``state`` is ``resyncing`` before the boot
resync, ``ready`` after, and ``recycling`` before a graceful self-exit.

Fork safety
-----------
A forked child (an rq work-horse per job, a celery prefork pool child per task)
inherits the parent's :class:`WorkerBus` object across ``os.fork()``. The bus
registers an ``os.register_at_fork`` after-in-child hook that re-derives the child's
identity to an explicit NON-MEMBER: ``{parent-name}/fork-{pid}`` generation 0, which
claims nothing, registers no presence, and can never collide with a member — so a
fleet op the child publishes is applied by the parent instead of echo-skipped as the
parent's own broadcast.
"""

from __future__ import annotations

# ``client_ctx`` is bound on this package BEFORE the mixin submodules import, so it is
# patchable at the ``tai42_skeleton.app.bus`` alias: the submodules read it through this
# package object at call time, and a ``monkeypatch.setattr`` on the alias bites the
# pooled connection every mixin opens.
from tai42_kit.clients import client_ctx

from tai42_skeleton.app.bus.models import (
    FleetResult,
    LastOp,
    LocalApplyResult,
    OpOutcome,
    SlotLostError,
    UnknownFleetTargetsError,
    WorkerIdentity,
    WorkerKind,
    WorkerResult,
    WorkerRow,
    WorkerState,
    _PresenceValue,
    presence_fresh,
)
from tai42_skeleton.app.bus.worker_bus import WorkerBus

# Present the worker bus at its public location: the class is composed in a submodule
# but its canonical identity (repr, ``gc.get_referrers`` labels, logs) is this package
# path, matching the preserved public import path.
WorkerBus.__module__ = __name__

__all__ = [
    "FleetResult",
    "LastOp",
    "LocalApplyResult",
    "OpOutcome",
    "SlotLostError",
    "UnknownFleetTargetsError",
    "WorkerBus",
    "WorkerIdentity",
    "WorkerKind",
    "WorkerResult",
    "WorkerRow",
    "WorkerState",
    "_PresenceValue",
    "client_ctx",
    "presence_fresh",
]
