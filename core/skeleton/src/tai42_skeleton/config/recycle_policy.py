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

from pydantic import BaseModel, Field

from tai42_skeleton.app.bus import WorkerKind

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
