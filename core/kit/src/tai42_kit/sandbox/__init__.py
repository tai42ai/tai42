"""The sandbox session lifecycle, implemented once for every provider.

A provider implements only its runtime I/O — creating session resources and
running ``exec`` / file transfers against them — by subclassing
:class:`~tai42_kit.sandbox.base.ManagedSandbox` and
:class:`~tai42_kit.sandbox.session.ManagedSandboxSession`. Everything host-shaped
around that (the session ledger, TTL bookkeeping, generic ``reap`` /
``destroy_session``, orphan recovery, and the SESSION-CREATE POLICY CHOKEPOINT
that enforces the operator-resolved :class:`SandboxPolicy` before any provider
primitive runs) lives here. A session is either ephemeral — its workspace dies
with it — or persistent, binding a durable workspace volume that survives the
reap; the tier is chosen per session spec and honored uniformly across providers.

It lives in kit and not the skeleton because a provider plugin may not import the
skeleton, and not the contract because the contract carries no logic. Kit is the
only package both a plugin and the skeleton may import. ``SandboxPolicy`` and its
ordering helpers are CONTRACT-defined (the contract cannot import the kit); the
kit imports and CONSUMES them at the chokepoint.
"""

from tai42_kit.sandbox.base import (
    LABEL_DURABILITY,
    LABEL_SANDBOX,
    LABEL_WORKSPACE,
    ManagedSandbox,
)
from tai42_kit.sandbox.conformance import (
    SandboxConformanceConfig,
    permissive_policy,
    run_sandbox_conformance,
)
from tai42_kit.sandbox.session import ManagedSandboxSession
from tai42_kit.sandbox.settings import SandboxDispatchSettings

__all__ = [
    "LABEL_DURABILITY",
    "LABEL_SANDBOX",
    "LABEL_WORKSPACE",
    "ManagedSandbox",
    "ManagedSandboxSession",
    "SandboxConformanceConfig",
    "SandboxDispatchSettings",
    "permissive_policy",
    "run_sandbox_conformance",
]
