"""Shared sandbox helpers both durable-workspace engines use.

* :func:`workspace_key_for` — the agent-namespaced, charset-valid workspace key both
  engines derive from a ``thread_id``, so a ``claude_code`` thread and a
  ``langchain_deep_agent`` thread of the SAME id never share one volume.
* :func:`build_policied_spec` — the shared spec-builder that turns each engine's
  engine-specific fields into a plain :class:`SandboxSessionSpec`, resolving only the two
  consumer-side needs off the platform :class:`SandboxPolicy` (the network default and
  leaving isolation unset). It enforces NO policy — the KIT session-create chokepoint
  applies the network ceiling, the isolation floor, and the durable gate on every create.
"""

from __future__ import annotations

import uuid
from typing import Final

from pydantic import SecretStr
from tai42_contract.app import tai42_app
from tai42_contract.sandbox import (
    SandboxDurability,
    SandboxNetwork,
    SandboxPolicy,
    SandboxSessionSpec,
)

# A fixed namespace so ``workspace_key_for`` is a stable, collision-free ``uuid5`` across
# workers and restarts. The literal is arbitrary but MUST never change (it names live
# durable volumes).
M39_NS: Final[uuid.UUID] = uuid.UUID("6f3e5b2a-9c41-5d7e-8a10-2b4c6d8e0f11")


def workspace_key_for(agent_name: str, thread_id: str) -> str:
    """The durable workspace key for ``(agent_name, thread_id)``.

    ``str(uuid5(M39_NS, f"{agent_name}:{thread_id}"))`` — a 36-char value valid under the
    ``[A-Za-z0-9_-]{1,64}`` workspace-key charset, AGENT-NAMESPACED so the two engines never
    collide on one volume for the same ``thread_id``, and stable so a cross-worker resume
    reattaches the SAME volume."""
    return str(uuid.uuid5(M39_NS, f"{agent_name}:{thread_id}"))


def build_policied_spec(
    *,
    image: str,
    workspace_key: str,
    durability: SandboxDurability,
    env: dict[str, SecretStr],
    ttl_seconds: int,
    labels: dict[str, str],
    cpu: float | None = None,
    memory_mb: int | None = None,
    network_setting: SandboxNetwork | None,
) -> tuple[SandboxSessionSpec, SandboxPolicy]:
    """Build a plain :class:`SandboxSessionSpec` plus the resolved :class:`SandboxPolicy`.

    The spec's non-optional ``network`` field is set from the per-agent ``network_setting``
    when the operator narrowed it, else defaulted to the platform egress posture read via
    ``sandbox_policy().egress`` (open by default, so a coding agent gets egress). ``isolation``
    is LEFT UNSET (``None``) so it inherits the platform floor at the kit create chokepoint.
    The helper enforces NO ceiling/floor/durable gate — the KIT create chokepoint does (a
    ``network`` looser than the ceiling, an ``isolation`` below the floor, or a ``persistent``
    spec while durable is off is a LOUD error there, never a silent widen/clamp/downgrade).
    The returned policy carries ``scrub_transcript`` for the adapter to read."""
    policy = tai42_app.sandboxes.sandbox_policy()
    network: SandboxNetwork = network_setting if network_setting is not None else policy.egress
    spec = SandboxSessionSpec(
        image=image,
        workspace_key=workspace_key,
        durability=durability,
        env=env,
        network=network,
        # Left unset so the kit create chokepoint resolves the effective isolation from the
        # platform floor; the agent never hardcodes it.
        isolation=None,
        cpu=cpu,
        memory_mb=memory_mb,
        ttl_seconds=ttl_seconds,
        labels=labels,
    )
    return spec, policy
