"""Recycle capability + refusal policy — the ONE source of truth.

This is the single exported source of truth for recycle refusal classification: the
refusal key lists live here so they can never silently desync from the deployment env
that pins them.

Shape detection is deterministic: the ``TAI_SUPERVISED`` marker set in lockstep with
the config mode by each supervised bundle (chart, compose, e2e harness). Absent
marker = ``bare`` (no supervisor) — recycle-class diffs are refused wholesale.

Refusal is two-tier:

* Tier 1 (EVERY shape, incl harness): the bus-reaching URLs. The orchestrator's
  census rides the bus itself, so a bus recycle is intrinsically unobservable — the
  scan opens the OLD bus while replacements register only on the NEW bus after
  resync. ``TAI_DEFAULT_REDIS_URL`` reaches the bus via ``BusSettings``' default-URL
  fallback, so it joins ``TAI_BUS_REDIS_URL`` here.
* Tier 2 (per shape): deployment-value pinning — a pod/container respawn re-injects
  the chart/compose value, so a profile-carried change silently reverts. These keys
  are orchestratable in principle (the bus is unchanged) but pinned, so they are
  refused upfront. ``harness`` carries no Tier-2 list.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from tai42_skeleton.app.bus import WorkerKind

if TYPE_CHECKING:
    from collections.abc import Mapping

SUPERVISION_MARKER_ENV = "TAI_SUPERVISED"

# Tier 1 — refused on every shape.
TIER1_REFUSED_KEYS: frozenset[str] = frozenset({"TAI_BUS_REDIS_URL", "TAI_DEFAULT_REDIS_URL"})

# Tier 2 (k8s) — the union of the chart's pinned pod-env helpers across BOTH
# deployments (``tai.commonEnv`` + ``tai.subMcpEnv`` + ``tai.backendEnv``), MINUS the
# X-band keys those helpers also carry (carried untouched across a profile apply,
# never recyclable) and MINUS the Tier-1 bus URL.
TIER2_K8S_REFUSED_KEYS: frozenset[str] = frozenset(
    {
        # commonEnv — redis auth + bus namespace + access-control toggle + feature
        # redis stores + memory-redis flow stores + the default-PG registry block.
        "REDIS_PASSWORD",
        "TAI_BUS_NAMESPACE",
        "ACCESS_CONTROL_ENABLE",
        "ACCESS_CONTROL_REDIS_URL",
        "INTERACTIONS_REDIS_URL",
        "TAI_TOOL_RUNS_REDIS_URL",
        "TAI_RATE_LIMIT_REDIS_URL",
        "HOOKS_REDIS_URL",
        "CONNECTOR_STORE_REDIS_URL",
        "FLOW_REDIS_URL",
        "MEMORY_REDIS_PASSWORD",
        "LLM_PROVIDER_CHECKPOINT_CONN_STRING",
        "LLM_PROVIDER_STORE_CONN_STRING",
        "TAI_DATABASE_DEFAULT_PG_HOST",
        "TAI_DATABASE_DEFAULT_PG_PORT",
        "TAI_DATABASE_DEFAULT_PG_DB",
        "TAI_DATABASE_DEFAULT_PG_USER",
        "TAI_DATABASE_DEFAULT_PG_PASSWORD",
        # subMcpEnv (serve only) — shared sub-MCP routing store.
        "SUB_MCP_REDIS_URL",
        # backendEnv (both deployments) — the task backend's connection env. arq and
        # celery are the chart-supervisable backend types, so only their broker keys
        # are pod-pinned here; rq's RQ_REDIS_URL is intentionally absent (rq is not
        # baked into any shipped image). A custom rq deployment pins its own broker
        # key in its pod spec and owns that key's refusal.
        "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
        "CELERY_REDBEAT_REDIS_URL",
        "ARQ_REDIS_URL",
        # Sandbox provider connection env. Both keys are recycle-class regardless of
        # which provider is active: ``SANDBOX_DOCKER_HOST`` is the sandbox-docker engine
        # connection, ``SANDBOX_LOCAL_ROOT`` binds the sandbox-local host workspace root.
        # The providers are mutually exclusive at RUNTIME (the scalar ``sandbox_module``),
        # so both coexist here. ``TAI_MCP_SANDBOX`` is deliberately absent (the provider
        # loads via the manifest module, not that env), mirroring ``TAI_MCP_BACKEND``.
        "SANDBOX_DOCKER_HOST",
        "SANDBOX_LOCAL_ROOT",
    }
)

# Tier 2 (compose) — the ``x-tai-app-env`` anchor's keys MINUS the X-classified
# deployment bare reads (shape marker + sentinel path, refused on the X axis and never
# recyclable), so ``TAI_SUPERVISED`` is excluded. Every service reuses the one anchor,
# so this key set IS the compose deployment-value pinning; the Tier-1 bus URLs the
# anchor also carries stay in the set (refused on their own axis as well).
TIER2_COMPOSE_REFUSED_KEYS: frozenset[str] = frozenset(
    {
        "TAI_CONFIG_MODE",
        "TAI_CONFIG_DIR_PATH",
        "TAI_MANIFEST_PATH",
        "TAI_BACKEND_MODULE",
        "ACCESS_CONTROL_ENABLE",
        "ACCESS_CONTROL_ALWAYS_PUBLIC_PATH_PREFIXES",
        "TAI_BUS_REDIS_URL",
        "TAI_DEFAULT_REDIS_URL",
        "SUB_MCP_REDIS_URL",
        "ARQ_REDIS_URL",
        "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
        "CELERY_REDBEAT_REDIS_URL",
        "TAI_TOOL_RUNS_REDIS_URL",
        "TAI_RATE_LIMIT_REDIS_URL",
        "INTERACTIONS_REDIS_URL",
        "HOOKS_REDIS_URL",
        "ACCESS_CONTROL_REDIS_URL",
        "CONNECTOR_STORE_REDIS_URL",
        "TAI_DATABASE_DEFAULT_PG_HOST",
        "TAI_DATABASE_DEFAULT_PG_PORT",
        "TAI_DATABASE_DEFAULT_PG_DB",
        "TAI_DATABASE_DEFAULT_PG_USER",
        "TAI_DATABASE_DEFAULT_PG_PASSWORD",
        "PROMETHEUS_MULTIPROC_DIR",
        "STORAGE_S3_ENDPOINT",
        "STORAGE_S3_BUCKET",
        "STORAGE_S3_ACCESS_KEY",
        "STORAGE_S3_SECRET_KEY",
        "STORAGE_S3_SECURE",
        "STORAGE_S3_REGION",
        # Sandbox provider connection env — both recycle-class regardless of which
        # provider is active (see the k8s list note above). ``TAI_MCP_SANDBOX`` stays
        # absent from the anchor and this list, mirroring ``TAI_BACKEND_MODULE``'s env.
        "SANDBOX_DOCKER_HOST",
        "SANDBOX_LOCAL_ROOT",
    }
)

# Deployment-infrastructure bare reads. X-classified: no profile may carry them — a
# carried value would spoof shape detection (self-exit on an unsupervised host) or
# relocate the readiness sentinel. The boundary validator folds this set into its
# X-band refusal, enforced at EVERY env writer.
X_CLASSIFIED_DEPLOYMENT_BARE_READS: frozenset[str] = frozenset({SUPERVISION_MARKER_ENV, "TAI_READY_SENTINEL_PATH"})

# The worker kinds the recycle orchestrator censuses as recycle targets.
CENSUS_TARGET_KINDS: tuple[WorkerKind, ...] = (WorkerKind.backend, WorkerKind.serve)


class Shape(StrEnum):
    """The deployment supervision shape, from the ``TAI_SUPERVISED`` marker."""

    k8s = "k8s"
    compose = "compose"
    harness = "harness"
    bare = "bare"


_MARKER_SHAPES: frozenset[str] = frozenset({Shape.k8s.value, Shape.compose.value, Shape.harness.value})


class CapabilityReport(BaseModel):
    """The recycle capability of this deployment, resolved at validate time. Consumed
    by the profile-apply validator to refuse a recycle-class diff upfront."""

    shape: Shape
    recycle_supported: bool
    refused_keys: list[str] = Field(default_factory=list)
    census_kinds: list[WorkerKind] = Field(default_factory=list)


def detect_shape() -> Shape:
    """Resolve the supervision shape from the ``TAI_SUPERVISED`` marker. Absent =
    ``bare``; any value other than the three supervised markers raises loudly (a typo
    must never silently degrade to bare and skip recycle refusal)."""
    marker = os.environ.get(SUPERVISION_MARKER_ENV, "").strip()
    if marker == "":
        return Shape.bare
    if marker not in _MARKER_SHAPES:
        raise ValueError(
            f"{SUPERVISION_MARKER_ENV}={marker!r} is not a recognized supervision marker "
            f"(expected one of {sorted(_MARKER_SHAPES)}, or unset for bare)"
        )
    return Shape(marker)


def refused_keys(shape: Shape) -> frozenset[str]:
    """The keys a recycle diff may not carry on ``shape``: Tier 1 always, plus the
    shape's Tier-2 pinned list (empty for harness and bare)."""
    if shape is Shape.k8s:
        return TIER1_REFUSED_KEYS | TIER2_K8S_REFUSED_KEYS
    if shape is Shape.compose:
        return TIER1_REFUSED_KEYS | TIER2_COMPOSE_REFUSED_KEYS
    return TIER1_REFUSED_KEYS


def capability_report() -> CapabilityReport:
    """The recycle capability of this deployment. ``recycle_supported`` is false only
    on ``bare`` (no supervisor) — a recycle-class diff is then refused wholesale;
    on the supervised shapes ``refused_keys`` names the upfront-refused keys."""
    shape = detect_shape()
    return CapabilityReport(
        shape=shape,
        recycle_supported=shape is not Shape.bare,
        refused_keys=sorted(refused_keys(shape)),
        census_kinds=list(CENSUS_TARGET_KINDS),
    )


def _replace_diff_keys(stored: Mapping[str, str], proposed: Mapping[str, str]) -> set[str]:
    """The env key NAMES a whole-env replace changes: added, removed, or value-changed.
    Names only — the caller classifies them; a diff never carries a value off this seam."""
    added = {key for key in proposed if key not in stored}
    removed = {key for key in stored if key not in proposed}
    changed = {key for key in proposed if key in stored and stored[key] != proposed[key]}
    return added | removed | changed


def _refuse_unrecyclable(diff_keys: set[str], recycle_diff_keys: list[str], report: CapabilityReport) -> None:
    """Refuse a profile apply whose diff cannot be carried on this deployment shape.

    A diff key the shape PINS (``refused_keys`` — a pod/container respawn re-injects it,
    or it reaches the worker bus itself) is refused upfront naming the key: a recycle can
    never make it stick. A recycle-class diff on a BARE (unsupervised) deployment is
    refused wholesale — no supervisor exists to respawn a worker under the new env. Both
    are loud ``ValueError``s the operations layer maps to a 400. Names only."""
    pinned = sorted(diff_keys & set(report.refused_keys))
    if pinned:
        raise ValueError(
            f"Refusing to apply this settings profile on a {report.shape.value!r} deployment: it changes "
            f"deployment-pinned key(s) a recycle cannot carry (a pod/container respawn re-injects them, or "
            f"they reach the worker bus itself): {', '.join(pinned)}. Change them in the deployment manifest."
        )
    if recycle_diff_keys and not report.recycle_supported:
        raise ValueError(
            "Refusing to apply this settings profile on a bare (unsupervised) deployment: it changes "
            f"recycle-class key(s) that require a worker recycle, and no supervisor is present to respawn "
            f"workers under the new env: {', '.join(recycle_diff_keys)}. Run under a supervised deployment."
        )


def _recycle_step_timeout() -> float:
    """The per-step recycle budget — the same drain budget a retire uses, so a recycled
    worker's replacement gets the shutdown-drain window to boot and rejoin the census."""
    from tai42_skeleton.routers.tool_runs_settings import tool_runs_settings

    return tool_runs_settings().shutdown_drain_seconds
