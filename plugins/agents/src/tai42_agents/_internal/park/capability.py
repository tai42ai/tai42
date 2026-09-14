"""Park capability: the provider-free park identity and the gates that decide whether a run
can be parked and rebuilt by a later, possibly different, worker.

Holds the :class:`ParkIdentity` shape both the LangGraph engines and ``claude_code`` record a
park through, the LangGraph-only :func:`build_park_identity` convenience, the directly
constructed park's structural gate :func:`assert_park_capable`, and the checkpoint-retention
horizon a durable checkpoint provider is kept to.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from tai42_contract.interactions import current_execution_identity
from tai42_kit.llm.settings import llm_provider_settings

from tai42_agents.settings import agents_park_redis_settings

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
        "completion_context",
        "completion_tool",
        "execution_fingerprint",
        "execution_identity",
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
        completion_context: Mapping[str, Any] | None = None,
        retention_bound: datetime | None = None,
        execution_identity: str | None = None,
        execution_fingerprint: str = "",
    ) -> None:
        self.agent_name = agent_name
        self.thread_id = thread_id
        self.rebuild_kwargs = rebuild_kwargs
        self.bind = bind
        # The registered tool a clean terminal drive fires with the final answer, so a
        # deferred response is delivered out of band. ``None`` = the driver's caller
        # receives the resumed result directly (the run face), no completion fire.
        self.completion_tool = completion_tool
        # The OPAQUE context the binder paired with that tool — the address the delivery tool
        # reads to route the answer. Carried verbatim (never interpreted here) and merged into
        # the completion fire, so this driver names no delivery tool's routing arguments.
        self.completion_context = completion_context
        # The latest wall-time every store backing this park is guaranteed to still hold it;
        # ``None`` = keep-forever. Gated against each ask deadline at persist time.
        self.retention_bound = retention_bound
        # The execution identity this run is authorized as — its ``(key, fingerprint)`` — so an
        # OUT-OF-BAND completion fired for the park later (the abandonment fire) runs under the same
        # identity a normal resume would, never fail-open. ``None`` key = no identity was bound (or
        # the host has no identity system); the fire then runs unbound.
        self.execution_identity = execution_identity
        self.execution_fingerprint = execution_fingerprint


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
    completion_context: Mapping[str, Any] | None = None,
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
    # Capture the identity this run is authorized as, so a later out-of-band completion fire runs
    # under it. On a resume this helper is re-entered under the continuation's bound identity, so a
    # re-park re-captures the same one — carried forward exactly like the completion tool.
    execution_identity, execution_fingerprint = current_execution_identity()
    return ParkIdentity(
        agent_name=agent_name,
        thread_id=thread_id,
        rebuild_kwargs=pinned,
        bind=bind,
        completion_tool=completion_tool,
        completion_context=completion_context,
        retention_bound=retention_bound,
        execution_identity=execution_identity,
        execution_fingerprint=execution_fingerprint,
    )


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
