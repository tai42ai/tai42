"""``ManagedSandbox`` — the shared sandbox lifecycle, written once.

A provider implements only its runtime I/O primitives; everything host-shaped
around them — the in-memory session ledger, TTL bookkeeping, generic
``reap()`` / ``destroy_session()``, orphan recovery, and the session-create
policy chokepoint — lives here so every provider enforces the operator policy
identically and no provider carries a policy copy of its own.

It lives in kit and not the skeleton because a provider may not import the
skeleton, and not the contract because the contract carries no logic.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from tai42_contract.sandbox import (
    Sandbox,
    SandboxDurability,
    SandboxPolicy,
    SandboxSessionInfo,
    SandboxSessionNotFoundError,
    SandboxSessionSpec,
    SandboxSpecRejectedError,
    isolation_strength,
    network_openness,
)

from tai42_kit.sandbox.session import ManagedSandboxSession

logger = logging.getLogger(__name__)

# Labels stamped on every session's runtime resources so a crash-restarted
# provider can rediscover its own orphans by querying them. The value of
# ``LABEL_SANDBOX`` is a constant marker; the other two carry the session's
# workspace identity and durability tier.
LABEL_SANDBOX = "tai42.sandbox"
LABEL_WORKSPACE = "tai42.sandbox.workspace"
LABEL_DURABILITY = "tai42.sandbox.durability"


@dataclass
class _LedgerRecord:
    """One live session's kit-owned bookkeeping, held beside the provider's
    session object. ``expires_at`` is the only mutable field (``touch`` extends
    it); everything else is fixed at create."""

    session: ManagedSandboxSession
    image: str
    workspace_key: str
    durability: SandboxDurability
    ttl_seconds: int
    labels: dict[str, str]
    created_at: datetime
    expires_at: datetime = field(compare=False)


class ManagedSandbox(Sandbox):
    """Sandbox provider base implementing the whole session lifecycle once.

    Owns the session ledger (keyed by id, with a ``workspace_key`` index), TTL
    bookkeeping, generic ``reap()`` / ``destroy_session()``, orphan recovery, and
    the KIT SESSION-CREATE POLICY CHOKEPOINT. A provider implements only
    :meth:`_create_session_resources`, :meth:`_destroy_session_resources`, and
    :meth:`_list_orphan_resources`, and ships a :class:`ManagedSandboxSession`
    subclass carrying its runtime I/O.

    The held :class:`SandboxPolicy` is installed by the skeleton at registration
    via :meth:`bind_policy` (the kit cannot read ``CoreSettings``); ``create_session``
    enforces it on EVERY create, before the provider primitive runs, for every
    consumer — there is no per-consumer policy copy.
    """

    def __init__(self) -> None:
        self._ledger: dict[str, _LedgerRecord] = {}
        self._by_workspace: dict[str, set[str]] = {}
        self._policy: SandboxPolicy | None = None

    # -- policy binding ------------------------------------------------------

    def bind_policy(self, policy: SandboxPolicy) -> None:
        """Install the resolved policy the create chokepoint enforces.

        Called by the skeleton at provider registration with the ONE policy it
        resolved from its settings. The kit consumes it and never resolves a
        policy of its own.
        """
        self._policy = policy

    # -- create chokepoint ---------------------------------------------------

    async def create_session(self, spec: SandboxSessionSpec) -> ManagedSandboxSession:
        """Create a session after enforcing the held policy — the kit
        session-create policy chokepoint.

        Enforced BEFORE the provider primitive runs: the ``network`` ceiling, the
        ``isolation`` floor (an unset value inherits the floor; a set value must
        be at-or-above it), the ``durable`` gate, and a ban on consumer labels in
        the reserved ``tai42.sandbox`` namespace. Each violation is a LOUD
        :class:`SandboxSpecRejectedError`, never a silent clamp or downgrade.
        Creating with no policy bound is a LOUD programming error.
        """
        policy = self._require_policy()
        effective_spec = self._enforce_policy(spec, policy)

        session = await self._create_session_resources(effective_spec)

        now = self._now()
        # The ledger holds the CONSUMER's labels, not the effective set: the reserved
        # markers ride the effective spec onto the runtime resource (so orphan recovery
        # finds it), but info().labels must round-trip exactly what create() was given.
        self._ledger[session.id] = _LedgerRecord(
            session=session,
            # Policy never rewrites image, so this is exactly the consumer request.
            image=effective_spec.image,
            workspace_key=effective_spec.workspace_key,
            durability=effective_spec.durability,
            ttl_seconds=effective_spec.ttl_seconds,
            labels=dict(spec.labels),
            created_at=now,
            expires_at=now + timedelta(seconds=effective_spec.ttl_seconds),
        )
        self._by_workspace.setdefault(effective_spec.workspace_key, set()).add(session.id)
        return session

    def _require_policy(self) -> SandboxPolicy:
        if self._policy is None:
            # A programming error, not a spec rejection: the skeleton always binds
            # a policy at registration, so an unbound one is a wiring fault the
            # consumer must never mistake for its own spec being refused.
            raise RuntimeError(
                "no sandbox policy is bound: create_session cannot enforce the operator policy "
                "(the skeleton binds one at provider registration via bind_policy)"
            )
        return self._policy

    def _enforce_policy(self, spec: SandboxSessionSpec, policy: SandboxPolicy) -> SandboxSessionSpec:
        """Return the spec resolved against ``policy`` — the effective isolation
        baked in and the standard labels stamped — or REJECT it loudly."""
        if network_openness(spec.network) > network_openness(policy.egress):
            raise SandboxSpecRejectedError(
                f"policy refused: network {spec.network!r} is looser than the egress ceiling {policy.egress!r}"
            )

        if spec.isolation is None:
            effective_isolation = policy.isolation
        else:
            if isolation_strength(spec.isolation) < isolation_strength(policy.isolation):
                raise SandboxSpecRejectedError(
                    f"policy refused: isolation {spec.isolation!r} is below the floor {policy.isolation!r}"
                )
            effective_isolation = spec.isolation

        if spec.durability == "persistent" and not policy.durable:
            raise SandboxSpecRejectedError(
                "policy refused: a persistent session was requested while durable workspaces are disabled"
            )

        reserved = sorted(key for key in spec.labels if key.startswith(LABEL_SANDBOX))
        if reserved:
            raise SandboxSpecRejectedError(
                f"policy refused: label keys {reserved} are in the reserved {LABEL_SANDBOX!r} namespace, "
                "which is kit-owned for orphan recovery and must not be supplied by a consumer"
            )

        return spec.model_copy(
            update={
                "isolation": effective_isolation,
                "labels": {**spec.labels, **self._standard_labels(spec)},
            }
        )

    def _standard_labels(self, spec: SandboxSessionSpec) -> dict[str, str]:
        """The infrastructure labels the provider stamps on the session's
        resources so a crash-restart can rediscover orphans. Applied to a spec
        already validated free of reserved-namespace keys, so no consumer label
        can collide with them."""
        return {
            LABEL_SANDBOX: "1",
            LABEL_WORKSPACE: spec.workspace_key,
            LABEL_DURABILITY: spec.durability,
        }

    # -- lookup --------------------------------------------------------------

    async def get_session(self, session_id: str) -> ManagedSandboxSession:
        return self._record(session_id).session

    async def list_sessions(self) -> list[SandboxSessionInfo]:
        return [self._build_info(record) for record in self._ledger.values()]

    def session_info(self, session_id: str) -> SandboxSessionInfo:
        """Build the observable state of a live session from its ledger record.

        Called by :class:`ManagedSandboxSession.info`; the ``workspace_path`` is
        read off the provider's session property so ``info().workspace_path``
        always equals ``session.workspace_path``.
        """
        return self._build_info(self._record(session_id))

    def _build_info(self, record: _LedgerRecord) -> SandboxSessionInfo:
        return SandboxSessionInfo(
            id=record.session.id,
            image=record.image,
            workspace_key=record.workspace_key,
            workspace_path=record.session.workspace_path,
            durability=record.durability,
            created_at=record.created_at,
            expires_at=record.expires_at,
            labels=dict(record.labels),
        )

    # -- TTL -----------------------------------------------------------------

    def extend_session(self, session_id: str) -> None:
        """Extend a session's ``expires_at`` by its ttl — the keep-alive turn.

        Called by :class:`ManagedSandboxSession.touch`; pure kit-owned
        bookkeeping, a provider adds nothing to TTL.
        """
        record = self._record(session_id)
        record.expires_at = self._now() + timedelta(seconds=record.ttl_seconds)

    async def reap(self) -> list[str]:
        """Destroy every session past its ``expires_at`` and return the destroyed
        ids.

        A reap tears down the session but PRESERVES a persistent workspace: the
        provider primitive is called with ``remove_workspace=False`` (an ephemeral
        workspace is container-local and dies with it regardless).
        """
        now = self._now()
        expired = [record for record in self._ledger.values() if record.expires_at <= now]
        for record in expired:
            await self._destroy_session_resources(record.session, remove_workspace=False)
            self._forget(record.session.id)
        return [record.session.id for record in expired]

    async def destroy_session(self, session_id: str) -> None:
        """Tear a session down, workspace included. Idempotent on an already-gone
        session.

        An explicit teardown removes the workspace too: the provider primitive is
        called with ``remove_workspace=True``, so even a persistent workspace is
        removed here (unlike :meth:`reap`, which preserves it).
        """
        record = self._ledger.get(session_id)
        if record is None:
            return
        await self._destroy_session_resources(record.session, remove_workspace=True)
        self._forget(session_id)

    def _forget(self, session_id: str) -> None:
        record = self._ledger.pop(session_id, None)
        if record is None:
            return
        peers = self._by_workspace.get(record.workspace_key)
        if peers is not None:
            peers.discard(session_id)
            if not peers:
                del self._by_workspace[record.workspace_key]

    def _record(self, session_id: str) -> _LedgerRecord:
        record = self._ledger.get(session_id)
        if record is None:
            raise SandboxSessionNotFoundError(session_id)
        return record

    # -- orphan recovery -----------------------------------------------------

    async def recover_orphans(self) -> list[str]:
        """Reconcile runtime-side resources carrying this base's labels that no
        live ledger entry claims — the residue of a crashed process.

        Called once at registration. The provider lists and disposes of each
        orphan (re-adopting or destroying it) via :meth:`_list_orphan_resources`;
        every one is logged LOUDLY here and handed back, so a recovery is
        auditable and never silent.
        """
        orphans = await self._list_orphan_resources()
        for descriptor in orphans:
            logger.warning("sandbox orphan reconciled at registration: %s", descriptor)
        return orphans

    # -- clock ---------------------------------------------------------------

    def _now(self) -> datetime:
        """The current instant TTL bookkeeping is computed against. A seam so a
        test can drive reap/touch without wall-clock waits."""
        return datetime.now(UTC)

    # -- provider primitives -------------------------------------------------

    @abstractmethod
    async def _create_session_resources(self, spec: SandboxSessionSpec) -> ManagedSandboxSession:
        """Create the runtime resources for ``spec`` and return the provider's
        :class:`ManagedSandboxSession`.

        ``spec`` arrives POLICY-RESOLVED: its ``isolation`` is the concrete
        effective level (never ``None``) and its ``labels`` carry the standard
        markers. The provider maps each field onto its runtime or REJECTS with
        :class:`SandboxSpecRejectedError` what it cannot enforce — it never
        silently downgrades.
        """

    @abstractmethod
    async def _destroy_session_resources(self, session: ManagedSandboxSession, *, remove_workspace: bool) -> None:
        """Tear down ``session``'s runtime resources.

        ``remove_workspace`` carries the persistent/ephemeral distinction: ``reap``
        passes ``False`` (a persistent workspace survives), an explicit
        ``destroy_session`` passes ``True`` (the workspace is removed too).
        """

    @abstractmethod
    async def _list_orphan_resources(self) -> list[str]:
        """List (and reconcile) runtime-side resources carrying this base's labels
        that no live session claims, returning a descriptor of each one handled.

        The provider re-adopts or destroys each orphan; the returned descriptors
        are logged by :meth:`recover_orphans` so the reconciliation is auditable.
        """
