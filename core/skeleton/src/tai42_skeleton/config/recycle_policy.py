"""Recycle capability + refusal policy.

The supervised deployment declares the keys it pins in ``TAI_SUPERVISED_PINNED_KEYS``;
this module holds the refusal mechanism and the platform's own Tier-1 keys.

Shape detection is deterministic: the ``TAI_SUPERVISED`` marker set in lockstep with
the config mode by each supervised deployment. Absent marker = ``bare`` (no
supervisor) — recycle-class diffs are refused wholesale.

Refusal is two-tier:

* Tier 1 (EVERY shape, incl harness): the bus-reaching URLs. The orchestrator's
  census rides the bus itself, so a bus recycle is intrinsically unobservable — the
  scan opens the OLD bus while replacements register only on the NEW bus after
  resync. ``TAI_DEFAULT_REDIS_URL`` reaches the bus via ``BusSettings``' default-URL
  fallback, so it joins ``TAI_BUS_REDIS_URL`` here.
* Tier 2 (``k8s`` / ``compose``): the deployment's declared pinned keys — a
  pod/container respawn re-injects the deployment value, so a profile-carried change
  silently reverts. They are refused upfront. ``harness`` and ``bare`` carry no Tier 2.
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, Field

from tai42_skeleton.app.bus import WorkerKind

if TYPE_CHECKING:
    from collections.abc import Mapping

SUPERVISION_MARKER_ENV = "TAI_SUPERVISED"

# Tier 1 — refused on every shape.
TIER1_REFUSED_KEYS: frozenset[str] = frozenset({"TAI_BUS_REDIS_URL", "TAI_DEFAULT_REDIS_URL"})

# The env var a supervised deployment declares its pinned keys in: a JSON list of env names.
PINNED_KEYS_ENV: Final = "TAI_SUPERVISED_PINNED_KEYS"

# Deployment-infrastructure bare reads. X-classified: no profile may carry them — a
# carried value would spoof shape detection (self-exit on an unsupervised host), shrink
# the deployment's pinned set, or relocate the readiness sentinel. The boundary validator
# folds this set into its X-band refusal, enforced at EVERY env writer.
X_CLASSIFIED_DEPLOYMENT_BARE_READS: frozenset[str] = frozenset(
    {SUPERVISION_MARKER_ENV, PINNED_KEYS_ENV, "TAI_READY_SENTINEL_PATH"}
)

# The worker kinds the recycle orchestrator censuses as recycle targets.
CENSUS_TARGET_KINDS: tuple[WorkerKind, ...] = (WorkerKind.backend, WorkerKind.serve)


class Shape(StrEnum):
    """The deployment supervision shape, from the ``TAI_SUPERVISED`` marker."""

    k8s = "k8s"
    compose = "compose"
    harness = "harness"
    bare = "bare"


_MARKER_SHAPES: frozenset[str] = frozenset({Shape.k8s.value, Shape.compose.value, Shape.harness.value})

# The shapes whose deployment declares a pinned set.
_PINNING_SHAPES: frozenset[Shape] = frozenset({Shape.k8s, Shape.compose})


class CapabilityReport(BaseModel):
    """The recycle capability of this deployment, resolved at validate time.

    Consumed by the profile-apply validator to refuse a recycle-class diff upfront.
    """

    shape: Shape
    recycle_supported: bool
    refused_keys: list[str] = Field(default_factory=list)
    census_kinds: list[WorkerKind] = Field(default_factory=list)


def detect_shape() -> Shape:
    """Resolve the supervision shape from the ``TAI_SUPERVISED`` marker.

    Absent = ``bare``; any value other than the three supervised markers raises loudly (a typo
    must never silently degrade to bare and skip recycle refusal).
    """
    marker = os.environ.get(SUPERVISION_MARKER_ENV, "").strip()
    if marker == "":
        return Shape.bare
    if marker not in _MARKER_SHAPES:
        raise ValueError(
            f"{SUPERVISION_MARKER_ENV}={marker!r} is not a recognized supervision marker "
            f"(expected one of {sorted(_MARKER_SHAPES)}, or unset for bare)"
        )
    return Shape(marker)


def pinned_keys() -> frozenset[str]:
    """The env keys the supervised deployment pins, from ``TAI_SUPERVISED_PINNED_KEYS``.

    The value must be a JSON list of non-empty strings (``[]`` is legal); anything else
    raises ``ValueError``. An unset variable raises ``RuntimeError``.
    """
    raw = os.environ.get(PINNED_KEYS_ENV)
    if raw is None:
        raise RuntimeError(f"{PINNED_KEYS_ENV} is not set")
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{PINNED_KEYS_ENV} must be a JSON list of env names, got {raw!r}") from exc
    if not isinstance(parsed, list) or not all(isinstance(key, str) and key for key in parsed):
        raise ValueError(f"{PINNED_KEYS_ENV} must be a JSON list of env names, got {raw!r}")
    return frozenset(parsed)


def _require_pinned_keys(shape: Shape) -> frozenset[str]:
    """The pinned set of a ``k8s``/``compose`` deployment; its absence raises naming the shape."""
    if PINNED_KEYS_ENV not in os.environ:
        raise RuntimeError(
            f"{SUPERVISION_MARKER_ENV}={shape.value} requires {PINNED_KEYS_ENV} "
            "(a JSON list of the env keys this deployment pins)"
        )
    return pinned_keys()


def refused_keys(shape: Shape) -> frozenset[str]:
    """The keys a recycle diff may not carry on ``shape``.

    Tier 1 always, plus the deployment's declared pinned set on ``k8s`` and ``compose``.
    """
    if shape in _PINNING_SHAPES:
        return TIER1_REFUSED_KEYS | _require_pinned_keys(shape)
    return TIER1_REFUSED_KEYS


def require_supervision_declared() -> None:
    """Refuse boot unless the supervision marker and the pinned set agree.

    A ``k8s``/``compose`` marker requires a well-formed ``TAI_SUPERVISED_PINNED_KEYS``; the
    pinned set on any other shape is refused; an unrecognized marker raises through
    :func:`detect_shape`.
    """
    shape = detect_shape()
    if shape in _PINNING_SHAPES:
        _require_pinned_keys(shape)
        return
    if PINNED_KEYS_ENV in os.environ:
        raise RuntimeError(f"{PINNED_KEYS_ENV} is set but {SUPERVISION_MARKER_ENV} is not k8s or compose")


def capability_report() -> CapabilityReport:
    """The recycle capability of this deployment.

    ``recycle_supported`` is false only on ``bare`` (no supervisor) — a recycle-class diff is then refused
    wholesale; on the supervised shapes ``refused_keys`` names the upfront-refused keys.
    """
    shape = detect_shape()
    return CapabilityReport(
        shape=shape,
        recycle_supported=shape is not Shape.bare,
        refused_keys=sorted(refused_keys(shape)),
        census_kinds=list(CENSUS_TARGET_KINDS),
    )


def _replace_diff_keys(stored: Mapping[str, str], proposed: Mapping[str, str]) -> set[str]:
    """The env key NAMES a whole-env replace changes: added, removed, or value-changed.

    Names only — the caller classifies them; a diff never carries a value off this seam.
    """
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
    are loud ``ValueError``s the operations layer maps to a 400. Names only.
    """
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
    """The per-step recycle budget — the same drain budget a retire uses.

    A recycled worker's replacement gets the shutdown-drain window to boot and rejoin the census.
    """
    from tai42_skeleton.routers.tool_runs_settings import tool_runs_settings

    return tool_runs_settings().shutdown_drain_seconds
